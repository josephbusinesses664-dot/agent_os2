"""Agent Runtime.

Instantiates an agent definition for one task: composes its system prompt
(role + project context + memory + relevant skills + tools + constraints +
output format), routes a model, runs the model/tool loop, records usage and
produces a structured result with reflection. No fake autonomy: everything an
agent claims must trace to tools that actually ran.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from agentos.agents.identity import identity_prompt_block
from agentos.domain.models import (
    AgentDef,
    ModelResponse,
    Project,
    Task,
    UsageRecord,
)
from agentos.models.router import ModelRouter
from agentos.tools.executor import ToolExecutor

logger = logging.getLogger("agentos.runtime")

MAX_TOOL_ROUNDS = 12
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_STRIP_XML_RE = re.compile(
    r"(<invoke\s+name=[\"\'][^\"\']+[\"\']\s*(?:/>|>.*?</invoke>)|"
    r"<tool_call>.*?</tool_call>|<parameter\s+name=[\"\'][^\"\']+[\"\']>.*?</parameter>)",
    re.DOTALL)
_STRIP_NUDGE_RE = re.compile(
    r"\[?(?:STOP using tools|Your tool calls were identical)[^\]]*\]?", re.DOTALL)


def clean_content(text: str) -> str:
    """Strip raw tool-call XML and loop-nudge echoes from agent text so
    Mattermost/tasks only ever see the agent's actual words."""
    text = _STRIP_XML_RE.sub("", text or "")
    text = _STRIP_NUDGE_RE.sub("", text)
    lines = [l.rstrip() for l in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


@dataclass
class RuntimeContext:
    """Everything a tool handler may touch, scoped to one agent run."""

    agent: AgentDef
    task: Optional[Task]
    project: Optional[Project]
    workspace: Any  # Path
    services: Any  # Services bundle
    executor: ToolExecutor
    router: ModelRouter
    prompts: Any  # PromptLibrary
    skill_registry: Any
    agent_registry: Any
    event_bus: Any
    budget_manager: Any
    approved_tools: set[str] = field(default_factory=set)
    active_skills: list = field(default_factory=list)
    spawn_depth: int = 0
    current_span: Optional[str] = None
    browser: Any = None  # BrowserSession, created lazily, closed at run end


@dataclass
class AgentRunResult:
    content: str = ""
    artifacts: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    model: Optional[str] = None
    cost: float = 0.0
    usage: list[UsageRecord] = field(default_factory=list)
    error: Optional[str] = None
    fail_class: Optional[str] = None  # transient | configuration | provider | permission | logic | human_required
    verified: bool = False
    verification_note: str = ""
    reflection: dict[str, Any] = field(default_factory=dict)


def classify_error(error: str) -> str:
    low = error.lower()
    if any(k in low for k in ("timeout", "timed out", "connection", "reset", "502", "503", "temporarily")):
        return "transient"
    if any(k in low for k in ("permission", "denied", "unauthorized", "not authorized")):
        return "permission"
    if any(k in low for k in ("not configured", "no api key", "no search api")):
        return "configuration"
    if any(k in low for k in ("provider", "model")):
        return "provider"
    return "logic"


_INVOKE_RE = re.compile(
    r"<invoke\s+name=[\"\']([^\"\']+)[\"\']\s*(?:/>|>(.*?)</invoke>)", re.DOTALL)
_PARAM_RE = re.compile(
    r"<parameter\s+name=[\"\']([^\"\']+)[\"\']>([^<]*)</parameter>", re.DOTALL)


def parse_tool_calls(response: ModelResponse) -> list[dict]:
    import json as _json

    calls = []
    for native in response.tool_calls:
        args = native.get("args")
        if args is None and native.get("arguments"):
            try:
                args = _json.loads(native["arguments"])
            except Exception:  # noqa: BLE001
                args = {"raw": native["arguments"]}
        calls.append({"tool": native.get("name") or native.get("tool") or "",
                      "args": args if isinstance(args, dict) else {}})
    for name, body in _INVOKE_RE.findall(response.content or ""):
        args = {}
        for pname, pval in _PARAM_RE.findall(body):
            args[pname] = pval.strip()
        calls.append({"tool": name.strip(), "args": args})
    for block in TOOL_CALL_RE.findall(response.content or ""):
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and "tool" in parsed:
                calls.append(parsed)
        except json.JSONDecodeError:
            continue
    return calls


class AgentRuntime:
    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.agent = ctx.agent

    # -- prompt composition -------------------------------------------------
    async def _build_prompt(self, task: Task) -> tuple[str, str]:
        ctx = self.ctx
        project = ctx.project
        project_context = "No project context."
        if project:
            project_context = (
                f"Project: {project.name}\n"
                f"Objective: {project.objective}\n"
                f"Status: {project.status.value} (stage: {project.stage or 'none'})\n"
                f"Requirements: {'; '.join(project.requirements[-5:]) or 'none recorded'}\n"
                f"Budget spent: ${project.cost:.3f}"
            )
        memory_ctx = "No relevant memory."
        try:
            entries = await ctx.services.memory.recall("project", project.project_id if project else "org",
                                                       query=task.description, limit=6)
            if entries:
                memory_ctx = "\n".join(f"- [{e.kind}] {e.content}" for e in entries)
        except Exception:  # noqa: BLE001
            pass

        skills_ctx = "None loaded."
        try:
            skills = await ctx.skill_registry.load_for_agent(self.agent.id, task.description, limit=5)
            if skills:
                # capabilities become real: register executable tools + hooks
                if ctx.services.capabilities is not None:
                    for skill in skills:
                        try:
                            await ctx.services.capabilities.load_skill(skill)
                        except Exception:  # noqa: BLE001
                            pass
                ctx.active_skills = skills
                skills_ctx = "\n\n".join(
                    f"### {s.name} ({s.id})\n{s.body[:3000]}" for s in skills
                )
        except Exception:  # noqa: BLE001
            pass

        tools = await ctx.services.tools.list()
        allowed_tools = [t for t in tools if self.agent.allows(t.permission_key or t.name)]
        # surface tools relevant to this task, plus capability tools from skills
        discovered = await ctx.services.tools.discover(task.description, agent=self.agent, limit=12)
        by_name = {t.name: t for t in allowed_tools}
        for t in discovered:
            by_name.setdefault(t.name, t)
        for skill in ctx.active_skills:
            for cap in skill.tools:
                by_name.setdefault(cap.name, None)
        tools_ctx = "\n".join(
            f"- {t.name}: {t.description}" for t in by_name.values() if t
        ) or "No tools permitted."

        inbox_ctx = "No pending messages."
        try:
            # per-project scope: an agent working project A must never see
            # handoffs from a sibling project B (cross-run state confusion)
            messages = await ctx.services.messages.inbox(self.agent.id,
                                                         unread_only=True, limit=40)
            relevant = [m for m in messages
                        if m.message_type.value in (
                            "task_request", "handoff", "status_update",
                            "challenge", "decision", "question")
                        and m.project_id in (None, task.project_id)][-8:]
            if relevant:
                lines = []
                for m in relevant:
                    summary = json.dumps(m.payload)[:300]
                    lines.append(f"[{m.message_type.value}] from {m.sender}: {summary}")
                inbox_ctx = "\n".join(lines)
        except Exception:  # noqa: BLE001
            pass

        context = {
            "agent_id": self.agent.id,
            "role": self.agent.role,
            "name": self.agent.name,
            "description": self.agent.description,
            "identity_block": identity_prompt_block(self.agent.identity),
            "responsibilities": f"Parent: {self.agent.parent_agent or 'none'}. "
                                f"Escalate unresolved issues to your parent agent.",
            "project_context": project_context,
            "task": task.description or task.title,
            "skills": skills_ctx,
            "tools": tools_ctx,
            "extra_constraints": (
                f"- Model policy tier: {self.agent.model_policy.get('tier', 't2')}.\n"
                f"- Risk level: {self.agent.risk_level}.\n"
                f"- You may delegate to your allowed sub-agents with the "
                f"agent.delegate tool (depth-limited); do not simulate other agents.\n"
                f"- Verify your work: check artifacts exist and report evidence.\n"
                f"- Incoming structured messages from your parent:\n{inbox_ctx}"
            ),
            "output_format": (
                "Write your final answer as a concise report with concrete "
                "artifacts (file paths under the project workspace when "
                "applicable). Never invent test/deploy evidence."
            ),
        }
        system = ctx.prompts.render(self.agent.id, context)
        user = (f"Task: {task.title}\n\n{task.description or ''}\n\n"
                f"Task id: {task.task_id}\nProject id: {task.project_id}")
        return system, user

    # -- model loop ---------------------------------------------------------
    async def run(self, task: Task, *, force_model: Optional[str] = None,
                  approved_tools: Optional[set[str]] = None) -> AgentRunResult:
        """Execute one task with this agent through the observe → plan → act →
        verify → recover loop. Never raises for ordinary failures; provider
        outages classify as transient."""
        ctx = self.ctx
        if approved_tools:
            ctx.approved_tools |= approved_tools
        result = AgentRunResult()
        tracer = getattr(ctx.services, "tracer", None)
        span = None
        span_cm = None
        if tracer is not None:
            span_cm = tracer.span(
                kind="agent", name=f"{self.agent.id}.run", trace_id=task.task_id,
                agent_id=self.agent.id, task_id=task.task_id,
                project_id=task.project_id, payload={"task": task.title})
            span = await span_cm.__aenter__()
            ctx.current_span = span.span_id
        started = time.perf_counter()
        try:
            result = await self._run_loop(task, result, force_model=force_model)
        finally:
            if span is not None and span_cm is not None:
                span.latency_ms = int((time.perf_counter() - started) * 1000)
                span.status = "ok" if not result.error else "error"
                span.error = result.error
                span.model = result.model
                span.cost = result.cost
                span.result_summary = (result.content or "")[:200]
                await span_cm.__aexit__(None, None, None)
                ctx.current_span = None
            # browser isolation: no session outlives its agent run
            if getattr(ctx, "browser", None) is not None:
                try:
                    await ctx.browser.close()
                except Exception:  # noqa: BLE001
                    pass
                ctx.browser = None
            await self._post_run(task, result)
        return result

    async def _run_loop(self, task: Task, result: AgentRunResult,
                        force_model: Optional[str]) -> AgentRunResult:
        ctx = self.ctx
        system, user = await self._build_prompt(task)

        model_id, reason = await ctx.router.route(self.agent, task, project_id=task.project_id,
                                                  force_model=force_model)
        if model_id is None:
            result.error = f"model routing rejected: {reason}"
            result.fail_class = "configuration"
            return result
        result.model = model_id

        messages: list[dict] = [{"role": "user", "content": user}]
        try:
            response = await self._call_with_failover(model_id, system, messages, task)
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            result.fail_class = classify_error(str(exc))
            return result

        executed_signatures: set[str] = set()
        duplicate_nudges = 0
        self._schema_name_map = {}
        for round_index in range(MAX_TOOL_ROUNDS):
            if response.error:
                result.error = response.error
                result.fail_class = classify_error(response.error)
                break
            await self._track_usage(response, task, result)
            calls = parse_tool_calls(response)
            for call in calls:
                call["tool"] = self._schema_name_map.get(call["tool"], call["tool"])
            # never re-execute an identical tool call (prevents model tool loops)
            fresh_calls = []
            for call in calls:
                args = call.get("args", {}) if isinstance(call.get("args"), dict) else {}
                # signature ignores volatile content fields so a re-emitted call
                # with regenerated text is still recognized as a duplicate
                sig_args = {k: v for k, v in args.items() if k != "content"}
                signature = call.get("tool", "") + json.dumps(sig_args, sort_keys=True)
                if signature in executed_signatures:
                    continue
                executed_signatures.add(signature)
                fresh_calls.append(call)
            if not fresh_calls:
                if calls and duplicate_nudges < 3:
                    duplicate_nudges += 1
                    if duplicate_nudges == 3:
                        # final warning: no more tools, produce the answer now
                        messages.append({
                            "role": "user",
                            "content": ("[STOP using tools. The tools already returned "
                                        "their results above. Write your complete final "
                                        "answer now as plain text — full detail, no tool "
                                        "calls.]"),
                        })
                    else:
                        messages.append({
                            "role": "user",
                            "content": ("[Your tool calls were identical to ones already "
                                        "executed this turn — their results are above. "
                                        "Continue the work or give your final answer "
                                        "without tool calls.]"),
                        })
                    try:
                        response = await self._call_with_failover(
                            model_id, system, messages, task)
                    except Exception as exc:  # noqa: BLE001
                        result.error = str(exc)
                        result.fail_class = classify_error(str(exc))
                        return result
                    continue
                result.content = clean_content(response.content or "")
                break
            result.tool_calls.extend(fresh_calls)
            await ctx.event_bus.publish("agent.tool_calls", {"agent": self.agent.id,
                                                             "task": task.task_id,
                                                             "calls": [c.get("tool") for c in fresh_calls]},
                                        agent_id=self.agent.id, task_id=task.task_id,
                                        project_id=task.project_id)
            for call in fresh_calls:
                tool_name = call.get("tool", "")
                args = call.get("args", {}) if isinstance(call.get("args"), dict) else {}
                tool_result = await ctx.executor.execute(ctx, self.agent, tool_name, args)
                result.tool_results.append({**tool_result, "tool": tool_name})
                if tool_result.get("ok") and tool_name in ("filesystem.write", "file.patch",
                                                            "api.call", "shell"):
                    for path in self._extract_artifacts(tool_result):
                        if path not in result.artifacts:
                            result.artifacts.append(path)
                messages.append({
                    "role": "user",
                    "content": f"[tool result for {tool_name}]\n{json.dumps(tool_result)[:4000]}",
                })
                if tool_result.get("pending_approval"):
                    result.error = f"awaiting approval {tool_result['pending_approval']}"
                    result.fail_class = "human_required"
                    return result
            try:
                response = await self._call_with_failover(model_id, system, messages, task)
            except Exception as exc:  # noqa: BLE001
                result.error = str(exc)
                result.fail_class = classify_error(str(exc))
                return result
        else:
            # exhausted the round budget: keep whatever was produced rather
            # than discarding real work (a stage that wrote files and ran out
            # of exploration rounds still counts as done if it has content)
            last_text = clean_content(response.content or "") if response is not None else ""
            if last_text:
                result.content = result.content or last_text
                result.error = None
            else:
                result.error = f"exceeded {MAX_TOOL_ROUNDS} tool rounds"
                result.fail_class = "logic"

        # -- minimum-work guard: never let an opening line pass as the work --
        if not result.error and len((result.content or "").strip()) < 400:
            final_text = await self._finish_with_work(
                model_id, system, messages, task,
                ("[Your answer so far is just an opening — the actual work is "
                 "missing. Do the full task now: use tools to gather what you "
                 "need, then give the complete final deliverable in your answer "
                 "— analysis, findings, conclusions, everything, in full detail.]"))
            if len(final_text) >= 400:
                result.content = final_text
            else:
                result.content = result.content or final_text
                result.error = "stage produced no substantive output"
                result.fail_class = "logic"

        # -- verify step: evidence over claims ------------------------------
        if not result.error:
            await self._verify_result(task, result)
        result.reflection = self._build_reflection(result, task)
        return result

    async def _finish_with_work(self, model_id: str, system: str,
                                messages: list[dict], task: Task,
                                nudge: str) -> str:
        """After a stub answer: demand the real work, allowing up to three
        more tool-capable turns, and return the final content."""
        import json as _json

        messages.append({"role": "user", "content": nudge})
        executed = set()
        for _ in range(3):
            try:
                # no tools offered: the model MUST answer in text now
                response = await self._call_with_failover(model_id, system, messages,
                                                          task, allow_tools=False)
            except Exception as exc:  # noqa: BLE001
                return f"_(finish attempt failed: {exc})_"
            calls = parse_tool_calls(response)
            for call in calls:
                call["tool"] = self._schema_name_map.get(call["tool"], call["tool"])
            fresh = []
            for call in calls:
                sig = call.get("tool", "") + _json.dumps(call.get("args", {}), sort_keys=True)
                if sig in executed:
                    continue
                executed.add(sig)
                fresh.append(call)
            if not fresh:
                return clean_content(response.content or "")
            for call in fresh:
                tool_name = call.get("tool", "")
                args = call.get("args", {}) if isinstance(call.get("args"), dict) else {}
                tool_result = await self.ctx.executor.execute(self.ctx, self.agent,
                                                              tool_name, args)
                result.tool_results.append({**tool_result, "tool": tool_name})
                messages.append({
                    "role": "user",
                    "content": f"[tool result for {tool_name}]\n{_json.dumps(tool_result)[:4000]}",
                })
        return clean_content(response.content or "")

    async def _verify_result(self, task: Task, result: AgentRunResult) -> None:
        """Deterministic verification: claimed artifacts must exist on disk,
        failed tool calls must be accounted for. No evidence → not verified."""
        notes: list[str] = []
        if result.artifacts:
            missing = []
            for artifact in result.artifacts:
                candidate = self.ctx.workspace / artifact
                if not candidate.exists():
                    missing.append(artifact)
            if missing:
                result.verification_note = f"artifacts missing on disk: {missing}"
                notes.append("artifact existence FAILED")
            else:
                notes.append("artifacts verified on disk")
        failed_tools = [r for r in result.tool_results if not r.get("ok")]
        if failed_tools:
            notes.append(f"{len(failed_tools)} tool call(s) failed")
        if not notes:
            notes.append("no artifacts claimed, no tool failures")
        result.verified = not any("FAILED" in n for n in notes)
        result.verification_note = "; ".join(notes)

    async def _post_run(self, task: Task, result: AgentRunResult) -> None:
        """Auto memory + parent notification + performance recording."""
        ctx = self.ctx
        try:
            # durable memory: explicit facts + the episode itself
            facts = await ctx.services.memory.extract_facts(task, result, self.agent.id)
            await ctx.services.memory.record_episode(task, result, self.agent.id)
            if facts:
                await ctx.event_bus.publish(
                    "memory.facts_extracted",
                    {"agent": self.agent.id, "count": len(facts)},
                    agent_id=self.agent.id, task_id=task.task_id,
                    project_id=task.project_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            # structured handoff to parent: TASK_RESULT or FAILURE
            parent = self.agent.parent_agent
            if parent:
                if result.error and result.fail_class != "human_required":
                    await ctx.services.messages.send(
                        "failure", self.agent.id, parent,
                        {"task_id": task.task_id, "error": str(result.error)[:500],
                         "fail_class": result.fail_class, "artifacts": result.artifacts},
                        project_id=task.project_id, task_id=task.task_id,
                        priority="high", requires_response=True)
                else:
                    await ctx.services.messages.send(
                        "task_result", self.agent.id, parent,
                        {"task_id": task.task_id, "summary": (result.content or "")[:1000],
                         "artifacts": result.artifacts, "cost": result.cost,
                         "verified": result.verified,
                         "verification_note": result.verification_note},
                        project_id=task.project_id, task_id=task.task_id,
                        priority="normal")
        except Exception:  # noqa: BLE001
            pass
        try:
            if ctx.services.performance is not None:
                await ctx.services.performance.record_run(
                    self.agent.id, outcome=result, task=task, latency_ms=0)
        except Exception:  # noqa: BLE001
            pass

    async def _call_with_failover(self, model_id: str, system: str,
                                  messages: list[dict], task: Task,
                                  allow_tools: bool = True) -> ModelResponse:
        ctx = self.ctx
        request = await self._make_request(model_id, system, messages, task)
        if not allow_tools:
            request.tools = []  # force a plain-text answer
        current = model_id
        used: set[str] = set()
        for attempt in range(3):
            used.add(current)
            model_def = await ctx.router.registry.get(current)
            provider = None
            if model_def:
                provider = ctx.services.providers.get(model_def.provider)
            if provider is None:
                fallback, reason = await ctx.router.failover(current, "no provider for model")
                if fallback and fallback not in used:
                    current = fallback
                    await ctx.event_bus.publish("model.failover",
                                                {"from": model_id, "to": current, "reason": reason},
                                                project_id=task.project_id, task_id=task.task_id,
                                                severity="warning")
                    request.model_id = current
                    continue
                return ModelResponse(request_id=request.request_id, model_id=current,
                                     error=f"no provider for {current}")
            request.model_id = current
            tracer = getattr(ctx.services, "tracer", None)
            span = None
            span_cm = None
            if tracer is not None:
                span_cm = tracer.span(
                    kind="model", name=f"model:{current}", trace_id=task.task_id,
                    parent_span=ctx.current_span, agent_id=self.agent.id,
                    task_id=task.task_id, project_id=task.project_id,
                    model=current)
                span = await span_cm.__aenter__()
            response = await provider.complete(request)
            if span is not None and span_cm is not None:
                span.tokens_in = response.prompt_tokens
                span.tokens_out = response.completion_tokens
                span.cost = response.estimated_cost
                span.status = "error" if response.error else "ok"
                span.error = response.error
                span.result_summary = (response.content or "")[:200]
                await span_cm.__aexit__(None, None, None)
            if not response.error:
                # success closes any open circuit for this model
                ctx.router.record_success(current)
                return response
            fallback, reason = await ctx.router.failover(current, response.error)
            if fallback and fallback not in used:
                current = fallback
                await ctx.event_bus.publish("model.failover",
                                            {"from": model_id, "to": current, "reason": reason},
                                            project_id=task.project_id, task_id=task.task_id,
                                            severity="warning")
            else:
                return response
        return response

    async def _make_request(self, model_id: str, system: str,
                            messages: list[dict], task: Task) -> Any:
        from agentos.domain.models import ModelRequest

        import re as _re

        max_tokens = 4096
        m = _re.search(r"MAX_TOKENS\s*:\s*(\d+)", task.description or "")
        if m:
            max_tokens = int(m.group(1))
        return ModelRequest(model_id=model_id, system=system, messages=messages,
                            project_id=task.project_id, agent_id=self.agent.id,
                            task_id=task.task_id, max_tokens=max_tokens,
                            tools=await self._tool_schemas())

    async def _tool_schemas(self) -> list[dict]:
        """OpenAI-format schemas for every tool this agent may actually call.

        Real providers (DeepSeek) only use tools they receive here; without
        schemas the model can only guess tool names in text."""
        import json

        registry = getattr(self.ctx.services, "tool_registry", None)
        if registry is None:
            return []
        self._schema_name_map = {}
        schemas = []
        for tool in await registry.list(enabled_only=True):
            permission_key = tool.permission_key or tool.name
            allowed = self.agent.permissions.get(
                permission_key, "deny" if permission_key in (
                    "shell", "web.scrape", "api.call", "mcp.call", "deploy",
                    "github", "postgres.query", "docker", "agent.delegate",
                    "browser.open", "browser.snapshot", "browser.click",
                    "browser.type", "browser.screenshot", "browser.evaluate",
                    "browser.close") else "allow")
            if allowed == "deny":
                continue
            # DeepSeek rejects tool names containing dots — sanitize and map back
            schema_name = tool.name.replace(".", "_")
            self._schema_name_map[schema_name] = tool.name
            params = tool.config.get("parameters")
            if not isinstance(params, dict):
                params = {}
            schemas.append({
                "type": "function",
                "function": {
                    "name": schema_name,
                    "description": tool.description,
                    "parameters": {"type": "object", "properties": params,
                                   "required": list(params.keys())},
                },
            })
        return schemas

    async def _track_usage(self, response: ModelResponse, task: Task, result: AgentRunResult) -> None:
        model_def = await self.ctx.router.registry.get(response.model_id)
        provider = model_def.provider if model_def else "unknown"
        record = UsageRecord(
            provider=provider,
            model=response.model_id,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            estimated_cost=response.estimated_cost,
            project_id=task.project_id,
            agent_id=self.agent.id,
            task_id=task.task_id,
            request_id=response.request_id,
        )
        result.usage.append(record)
        result.cost += response.estimated_cost

    def _extract_artifacts(self, tool_result: dict) -> list[str]:
        paths: list[str] = []
        raw = json.dumps(tool_result)
        for m in re.finditer(r"\"?path\"?\s*:\s*\"([^\"]+)\"", raw):
            p = m.group(1)
            if p.startswith(("workspace", "artifacts", "./", "/")) or "." in p:
                paths.append(p)
        return paths[:20]

    def _build_reflection(self, result: AgentRunResult, task: Task) -> dict[str, Any]:
        return {
            "asked": task.title,
            "done": result.content[:500] if result.content else "",
            "worked": [c.get("tool") for c in result.tool_calls],
            "failed": result.error or None,
            "remains": "verification by a senior agent" if result.error else "none",
            "next_agent_note": f"task {task.task_id} run by {self.agent.id} "
                               f"using {result.model}; cost ${result.cost:.4f}",
        }