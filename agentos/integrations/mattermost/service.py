"""Mattermost Service.

Makes Mattermost the human-facing interface of the AI organization:

* programmatically creates the workspace channel layout,
* posts with the *internal agent identity layer* — one bot account, many
  agent identities (`[AGENT • ROLE]`),
* routes system events to the right channels,
* handles human override commands (@agent …) and approval commands.

Graceful degradation: if Mattermost is unreachable the system keeps running;
every post attempt is logged, not fatal.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from agentos.config import Settings
from agentos.domain.models import AgentDef, Event
from agentos.integrations.mattermost.client import MattermostClient

logger = logging.getLogger("agentos.mattermost")

CHANNEL_LAYOUT: dict[str, str] = {
    "announcements": "Organization announcements",
    "executive": "Executive updates and briefings",
    "decisions": "Recorded decisions",
    "approvals": "Human approval requests",
    "agent-status": "Live agent activity and status",
    "agent-logs": "Agent task results and reports",
    "agent-discussion": "Inter-agent discussion feed",
    "system-errors": "Errors, retries and failures",
    "operations": "Scheduled / standing mission activity",
    "monitoring": "Health and monitoring",
    "security": "Security events",
    "deployments": "Deployment activity",
    "branch-executive": "Executive branch — internal chatter",
    "branch-product": "Product branch — internal chatter",
    "branch-engineering": "Engineering branch — internal chatter",
    "branch-design": "Design branch — internal chatter",
    "branch-research": "Research branch — internal chatter",
    "branch-marketing": "Marketing branch — internal chatter",
    "branch-sales": "Sales branch — internal chatter",
    "branch-qa": "QA & Security branch — internal chatter",
    "branch-operations": "Operations branch — internal chatter",
}

STATUS_EMOJI = {
    "idle": "⚪", "working": "🟢", "awaiting_review": "🟡", "awaiting_approval": "🟠",
    "blocked": "🟠", "failed": "🔴", "paused": "⏸️", "offline": "⚫",
}

EVENT_CHANNEL: dict[str, str] = {
    "approval.requested": "approvals",
    "approval.granted": "approvals",
    "approval.rejected": "approvals",
    "workflow.failed": "system-errors",
    "task.failed": "system-errors",
    "agent.failed": "system-errors",
    "deployment.started": "deployments",
    "deployment.completed": "deployments",
    "deployment.failed": "deployments",
    "model.failover": "system-errors",
    "budget.warning": "executive",
    "workflow.completed": "announcements",
    "project.created": "announcements",
    "schedule.created": "operations",
    "schedule.fired": "operations",
    "schedule.failed": "system-errors",
    "schedule.paused": "operations",
    "schedule.enabled": "operations",
    "schedule.deleted": "operations",
    "task.deadline_exceeded": "system-errors",
}


def format_identity(agent: AgentDef, model: Optional[str] = None) -> str:
    from agentos.agents.personas import PERSONAS
    persona = PERSONAS.get(agent.id, agent.name)
    return f"[{persona} • {agent.name}]"


class MattermostService:
    def __init__(self, settings: Settings, client: MattermostClient,
                 on_command: Optional[Callable] = None) -> None:
        self.settings = settings
        self.client = client
        self.on_command = on_command  # async (dict) -> None
        self.channels: dict[str, str] = {}  # logical name -> channel id
        self.team_id: Optional[str] = None
        self.bot_user_id: Optional[str] = None
        self.available = False
        self._paused_agents: set[str] = set()
        self._last_post_ts: dict[str, int] = {}
        self._user_clients: dict[str, Any] = {}  # username -> MattermostClient
        self.agent_user_ids: set[str] = set()  # ids of the 41 agent accounts
        self._project_channels: dict[str, str] = {}  # project_id -> logical channel the human asked from

    # -- lifecycle ----------------------------------------------------------
    async def connect(self) -> bool:
        try:
            me = await self.client.me()
            self.bot_user_id = me.get("id")
            self.available = True
            logger.info("mattermost connected as %s", me.get("username"))
            # collect the agent accounts' user ids so the listener never
            # mistakes an agent's own post for a human message
            from agentos.agents.personas import USERNAMES
            for uname in set(USERNAMES.values()):
                user = await self.client.get_user_by_username(uname)
                if user:
                    self.agent_user_ids.add(user["id"])
            return True
        except Exception as exc:  # noqa: BLE001
            self.available = False
            logger.warning("mattermost unavailable: %s", exc)
            return False

    async def ensure_workspace(self) -> None:
        """Create team + channels per the logical workspace organization."""
        if not self.available:
            return
        team = await self.client.get_team_by_name(self.settings.mattermost_team)
        if team is None:
            team = await self.client.create_team(self.settings.mattermost_team,
                                                 "AI Organization")
        self.team_id = team["id"]
        for name, purpose in CHANNEL_LAYOUT.items():
            channel = await self.client.get_channel_by_name(self.team_id, name)
            if channel is None:
                channel = await self.client.create_channel(
                    self.team_id, name, name.replace("-", " ").title(), purpose)
            self.channels[name] = channel["id"]

        # register the team's default channels too, so the listener polls them
        for extra in ("town-square", "general", "off-topic"):
            if extra in self.channels:
                continue
            channel = await self.client.get_channel_by_name(self.team_id, extra)
            if channel is not None:
                self.channels[extra] = channel["id"]

    async def ensure_project_channels(self, project_id: str, project_name: str) -> dict[str, str]:
        """Create per-project channels (projects/<project-id>) if missing."""
        if not self.available:
            return {}
        prefix = f"project-{project_id[:8]}"
        names = {
            f"{prefix}": f"{project_name}",
            f"{prefix}-updates": f"{project_name} — updates",
            f"{prefix}-reviews": f"{project_name} — reviews",
        }
        for name, display in names.items():
            if name not in self.channels:
                channel = await self.client.get_channel_by_name(self.team_id, name)
                if channel is None:
                    channel = await self.client.create_channel(self.team_id, name, display)
                self.channels[name] = channel["id"]
        return {name: self.channels[name] for name in names}

    # -- posting ------------------------------------------------------------
    async def post_as_user(self, username: str, token: str, message: str,
                           channel: str) -> Optional[str]:
        """Post as a specific agent's own Mattermost account (per-user PAT)."""
        client = self._user_clients.get(username)
        if client is None:
            client = MattermostClient(self.client.base_url, token)
            self._user_clients[username] = client
        try:
            post = await client.post(self._channel_id(channel), message)
            return post.get("id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("post as %s failed: %s", username, exc)
            return None

    async def post_as_agent(self, agent: AgentDef, message: str,
                            channel: str = "agent-status",
                            model: Optional[str] = None,
                            root_id: Optional[str] = None) -> Optional[str]:
        if not self.available:
            logger.info("[post skipped, mattermost down] %s: %s", agent.name, message[:120])
            return None
        # post under the agent's own account when its PAT is configured
        from agentos.agents.personas import token_for_agent, username_for
        token = token_for_agent(agent.id)
        if token:
            post = await self.post_as_user(username_for(agent.id), token, message, channel)
            if post:
                return post
        identity = format_identity(agent, model)
        text = f"{identity}\n{message}"
        try:
            post = await self.client.post(self._channel_id(channel), text, root_id=root_id)
            return post.get("id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("mattermost post failed: %s", exc)
            return None

    def _channel_id(self, name: str) -> str:
        if name in self.channels:
            return self.channels[name]
        return self.channels.get(self.settings.mattermost_channel) or ""

    async def post_to(self, channel: str, message: str) -> Optional[str]:
        if not self.available:
            return None
        try:
            post = await self.client.post(self._channel_id(channel), message)
            return post.get("id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("mattermost post to %s failed: %s", channel, exc)
            return None

    async def post_status(self, agents: list[AgentDef], instances: list[Any]) -> None:
        """Post a compact live status board to agent-status."""
        lines = ["**AI AGENCY — AGENT STATUS**"]
        by_id = {a.id: a for a in agents}
        status_map = {i.agent_id: i for i in instances}
        for agent in agents:
            inst = status_map.get(agent.id)
            if inst is None:
                continue
            emoji = STATUS_EMOJI.get(inst.status.value, "⚪")
            detail = f" — {inst.latest_action[:60]}" if inst.latest_action else ""
            lines.append(f"{emoji} **{agent.name}** ({agent.role}){detail}")
        await self.post_to("agent-status", "\n".join(lines))

    async def post_approval(self, approval: Any, run_id: str = "") -> None:
        if not self.available:
            return
        header = "⚠️ **APPROVAL REQUIRED**"
        text = (
            f"{header}\n\n"
            f"**Agent:** {approval.agent_name}\n"
            f"**Action:** {approval.action}\n"
            f"**Risk:** {approval.risk_level.upper()}\n\n"
            f"{approval.reason}\n\n"
            f"Reply: `@agent approve {approval.approval_id}` or "
            f"`@agent reject {approval.approval_id}`"
        )
        await self.post_to("approvals", text)

    # -- event routing ------------------------------------------------------
    async def route_event(self, event: Event) -> None:
        channel = EVENT_CHANNEL.get(event.type)
        if channel is None:
            return
        text = self._event_text(event)
        if not text:
            return
        await self.post_to(channel, text)
        # also report lifecycle checkpoints where the human asked for them
        if event.project_id and event.project_id in self._project_channels:
            home = self._project_channels[event.project_id]
            if home != channel:
                await self.post_to(home, text)

    def _event_text(self, event: Event) -> str:
        payload = event.payload or {}
        if event.type == "approval.requested":
            return f"🟠 Approval requested: {payload.get('stage', '?')} (run {payload.get('run', '?')})"
        if event.type == "approval.granted":
            return f"✅ Approval granted: {payload.get('stage', '?')}"
        if event.type == "approval.rejected":
            return f"❌ Approval rejected: {payload.get('stage', '?')}"
        if event.type == "workflow.failed":
            return f"🔴 Workflow failed at {payload.get('stage', '?')}: {payload.get('error', '')[:200]}"
        if event.type == "workflow.completed":
            return f"🎉 Workflow completed: {payload.get('workflow', '?')} (run {payload.get('run', '?')})"
        if event.type == "project.created":
            return f"🚀 Project created: {payload.get('name', '?')}"
        if event.type == "model.failover":
            return f"⚠️ Model failover: {payload.get('from')} → {payload.get('to')} ({payload.get('reason', '')[:120]})"
        if event.type == "budget.warning":
            return (f"💰 Budget warning [{payload.get('scope')}:{payload.get('scope_id')}]: "
                    f"${payload.get('spent', 0):.2f} of ${payload.get('limit', 0):.2f} used")
        if event.type == "deployment.started":
            return "🚢 Deployment started"
        if event.type == "deployment.completed":
            return "✅ Deployment completed"
        if event.type == "deployment.failed":
            return "🔴 Deployment failed"
        if event.type == "schedule.created":
            return (f"🗓️ Standing mission created: {payload.get('name', '?')} "
                    f"every {payload.get('interval_seconds', '?')}s "
                    f"(id {payload.get('schedule_id', '?')})")
        if event.type == "schedule.fired":
            return (f"⏰ Standing mission fired: run {payload.get('run_count', '?')} "
                    f"(id {payload.get('schedule_id', '?')})")
        if event.type == "schedule.failed":
            return (f"🔴 Standing mission fire failed "
                    f"(id {payload.get('schedule_id', '?')}): "
                    f"{payload.get('error', '')[:200]}")
        if event.type == "schedule.paused":
            return f"⏸️ Standing mission paused (id {payload.get('schedule_id', '?')})"
        if event.type == "schedule.enabled":
            return f"▶️ Standing mission enabled (id {payload.get('schedule_id', '?')})"
        if event.type == "schedule.deleted":
            return f"🗑️ Standing mission deleted (id {payload.get('schedule_id', '?')})"
        if event.type == "task.deadline_exceeded":
            return (f"⏳ Task missed its deadline and was escalated "
                    f"(id {payload.get('task_id', '?')}, due {payload.get('deadline', '?')})")
        return ""

    # -- human control ------------------------------------------------------
    def pause_agent(self, agent_id: str) -> None:
        self._paused_agents.add(agent_id)

    def resume_agent(self, agent_id: str) -> None:
        self._paused_agents.discard(agent_id)

    def is_paused(self, agent_id: str) -> bool:
        return agent_id in self._paused_agents

    WORK_TRIGGERS = ("build", "create", "make", "plan", "launch", "run",
                     "write", "research", "generate", "implement", "fix",
                     "design", "test", "deploy", "refactor", "analyze",
                     "i want", "i need", "can you", "please", "kick off")

    def _looks_like_work(self, text: str) -> bool:
        low = text.lower()
        return low.startswith(self.WORK_TRIGGERS)

    def _logical_for(self, channel_id: str) -> str:
        """Reverse-lookup the logical channel name for a channel id."""
        for name, cid in self.channels.items():
            if cid == channel_id:
                return name
        return self.settings.mattermost_channel

    async def handle_message(self, message: dict, engine: Any, svc: Any) -> None:
        """Process a human message: @agent commands, approvals, a new goal
        (explicit work requests), or plain chat handled by the executive."""
        text = (message.get("message") or "").strip()
        if not text or message.get("user_id") == self.bot_user_id:
            return
        if message.get("user_id") in self.agent_user_ids:
            return  # an agent's own post — never re-process as a human message
        if text.startswith("@agent"):
            await self._handle_command(text, message, engine, svc)
            return
        if text.startswith(("@approve", "@reject", "@changes")):
            await self._handle_approval(text, message, svc)
            return
        if self.on_command:
            if self._looks_like_work(text):
                # explicit work request -> the orchestrator runs it
                await self.on_command({
                    "type": "goal", "text": text,
                    "user_id": message.get("user_id", "human"),
                    "channel": message.get("channel_id", ""),
                })
            else:
                # casual chat -> the executive answers as a person
                await self.on_command({
                    "type": "chat", "text": text,
                    "user_id": message.get("user_id", "human"),
                    "channel": message.get("channel_id", ""),
                })

    async def _handle_command(self, text: str, message: dict, engine: Any, svc: Any) -> None:
        parts = text.split()
        command = parts[1] if len(parts) > 1 else "status"
        arg = parts[2] if len(parts) > 2 else ""
        channel = message.get("channel_id", "")
        if command in ("stop", "pause"):
            if arg:
                svc.mattermost.pause_agent(arg)
                await self.post_to("executive", f"⏸️ Agent `{arg}` paused by human.")
            else:
                await self.post_to("executive", "Specify an agent: `@agent stop <agent-id>`")
        elif command == "resume":
            if arg:
                svc.mattermost.resume_agent(arg)
                await self.post_to("executive", f"▶️ Agent `{arg}` resumed.")
            else:
                await self.post_to("executive", "Specify an agent: `@agent resume <agent-id>`")
        elif command == "status":
            agents = await svc.agent_registry.list(enabled_only=True)
            instances = await svc.agent_registry.list_instances()
            await self.post_status(agents, instances)
        elif command == "approve":
            approval = await svc.approvals.get(arg)
            if approval:
                await svc.approvals.decide(arg, "approved", "human")
                await self.post_to("approvals", f"✅ Approved `{arg}`.")
                await engine.approve(arg, "approved", decided_by="human")
        elif command == "reject":
            approval = await svc.approvals.get(arg)
            if approval:
                await svc.approvals.decide(arg, "rejected", "human")
                await self.post_to("approvals", f"❌ Rejected `{arg}`.")
                await engine.approve(arg, "rejected", decided_by="human")
        elif command in ("emergency", "stand-down"):
            if command == "emergency":
                reason = " ".join(parts[2:]) if len(parts) > 2 else ""
                state = await svc.policy.engage("human", reason)
                await self.post_to(
                    "security",
                    f"🚨 **EMERGENCY STOP ENGAGED** by a human operator."
                    f" All tool execution is refused."
                    + (f"\nReason: {reason}" if reason else "")
                    + f"\nStand down with `@agent stand-down`.")
                await self.post_to("announcements",
                                   f"🚨 Emergency stop engaged ({reason or 'no reason given'}).")
            else:
                state = await svc.policy.disengage("human")
                await self.post_to("security",
                                   "✅ **EMERGENCY STOP DISENGAGED** by a human operator."
                                   " Tool execution resumed.")
        elif command == "policies":
            rows = svc.policy.policies()
            lines = ["**HARD SECURITY POLICIES** (override the hierarchy)"]
            for p in rows:
                lines.append(f"- `{p.id}` [{p.kind.value}] "
                             f"tools={','.join(p.tools) or 'all'} "
                             f"env={','.join(p.environments) or 'all'}")
            await self.post_to("security", "\n".join(lines))
        elif command == "retry":
            await self.post_to("executive", f"Retry requested for `{arg}` — re-queued.")
            if arg and self.on_command:
                await self.on_command({"type": "retry", "task_id": arg, "user_id": "human"})
        elif command == "explain":
            await self.post_to("executive",
                               "I can explain: `@agent status`, `@agent stop <agent>`, "
                               "`@agent resume <agent>`, `@agent approve <id>`, "
                               "`@agent reject <id>`, `@agent retry <task>`, "
                               "`@agent emergency <reason>` (halt all tools), "
                               "`@agent stand-down` (resume), `@agent policies`. "
                               "Or just tell me a goal in general.")
        else:
            await self.post_to("executive", f"Unknown command `{command}`. Try `@agent explain`.")

    async def _handle_approval(self, text: str, message: dict, svc: Any) -> None:
        parts = text.split()
        decision = {"@approve": "approved", "@reject": "rejected",
                    "@changes": "changes_requested"}.get(parts[0], "approved")
        approval_id = parts[1] if len(parts) > 1 else ""
        if not approval_id:
            return
        approval = await svc.approvals.get(approval_id)
        if not approval or approval.status.value != "pending":
            await self.post_to("approvals", f"`{approval_id}` is not pending.")
            return
        await svc.approvals.decide(approval_id, decision, "human")
        await self.post_to("approvals",
                           f"{'✅' if decision == 'approved' else '❌'} `{approval_id}` → {decision}")
        if self.on_command:
            await self.on_command({"type": "approval", "approval_id": approval_id,
                                   "decision": decision, "user_id": message.get("user_id", "human")})