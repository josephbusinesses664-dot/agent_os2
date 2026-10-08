"""Bootstrap — composition root and lifecycle.

Wires every subsystem together: services, orchestrator engine, Mattermost
connection + listener, event → Mattermost sink, and the worker loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any, Optional

from agentos.config import Settings, get_settings
from agentos.integrations.mattermost.client import MattermostClient
from agentos.integrations.mattermost.listener import MattermostListener
from agentos.integrations.mattermost.service import MattermostService
from agentos.orchestration.engine import OrchestratorEngine
from agentos.services import Services

logger = logging.getLogger("agentos")


async def build_app(settings: Optional[Settings] = None) -> Services:
    settings = settings or get_settings()
    svc = Services(settings)
    await svc.init_db()
    await svc.seed()

    # engine
    svc.engine = OrchestratorEngine(svc)
    svc.engine.get_graph()

    # mattermost
    if settings.mattermost_url and settings.mattermost_token:
        client = MattermostClient(settings.mattermost_url, settings.mattermost_token)
        service = MattermostService(settings, client,
                                 on_command=await on_mattermost_command(svc))
        svc.mattermost = service
        service.svc = svc
        if await service.connect():
            await service.ensure_workspace()
            svc.events.subscribe(service.route_event)
            # mirror agent-to-agent inbox traffic into branch channels
            svc.messages.mirror = _make_message_mirror(svc)
            # listen for human messages
            listener = MattermostListener(service)
            service.listener = listener
        else:
            logger.warning("mattermost offline — continuing without it")
    else:
        logger.info("mattermost not configured (set MATTERMOST_URL/MATTERMOST_TOKEN)")

    return svc


def _make_message_mirror(svc: Services):
    """Return an async hook that mirrors each agent-to-agent message into the
    sender's branch channel. Best-effort: never raises into the message bus."""

    async def mirror(msg: Any) -> None:
        if not (svc.mattermost and svc.mattermost.available):
            return
        from agentos.agents.hierarchy import ORG_AGENTS
        from agentos.agents.personas import branch_channel_for
        from agentos.integrations.mattermost.service import format_identity

        sender = ORG_AGENTS.get(msg.sender)
        recipient = ORG_AGENTS.get(msg.recipient)
        from_label = format_identity(sender) if sender else f"[{msg.sender}]"
        to_label = format_identity(recipient) if recipient else f"[{msg.recipient}]"
        payload = msg.payload or {}
        mtype = str(getattr(msg.message_type, "value", msg.message_type))
        parts = []
        for key in ("title", "description", "concern", "reason", "decision",
                    "rationale", "note", "question", "text", "summary", "result",
                    "recommended_action", "claim", "scope"):
            value = payload.get(key)
            if value:
                parts.append(str(value))
        if mtype == "handoff" and payload.get("artifacts"):
            parts.append("artifacts: " + ", ".join(payload["artifacts"]))
        if mtype == "challenge" and payload.get("evidence"):
            parts.append("evidence: " + "; ".join(str(e) for e in payload["evidence"]))
        if mtype == "challenge" and payload.get("severity"):
            parts.append("severity: " + str(payload["severity"]))
        body = "\n".join(parts)
        line = f"{from_label} → {to_label} — **{mtype.upper()}**:\n{body}"
        from agentos.agents.personas import token_for_agent, username_for
        token = token_for_agent(msg.sender)
        if token:
            await svc.mattermost.post_as_user(
                username_for(msg.sender), token, line, branch_channel_for(msg.sender))
        else:
            await svc.mattermost.post_to(branch_channel_for(msg.sender), line)

    return mirror


async def run_control_plane(svc: Services, stop_event: Optional[asyncio.Event] = None) -> None:
    """Run API + worker + mattermost listener until stopped."""
    stop_event = stop_event or asyncio.Event()
    tasks = []

    # API server
    from agentos.api.main import create_app
    import uvicorn

    app = create_app(svc)
    config = uvicorn.Config(app, host=svc.settings.api_host, port=svc.settings.api_port,
                            log_level="warning")
    server = uvicorn.Server(config)
    api_task = asyncio.create_task(server.serve())
    tasks.append(api_task)

    # worker
    worker_task = asyncio.create_task(svc.engine.worker_loop(stop_event=stop_event))
    tasks.append(worker_task)

    # mattermost listener
    if svc.mattermost is not None and getattr(svc.mattermost, "listener", None):
        tasks.append(asyncio.create_task(svc.mattermost.listener.run(stop_event=stop_event)))

    async def _shutdown() -> None:
        stop_event.set()
        server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    await svc.close()


async def on_mattermost_command(svc: Services) -> Any:
    """Handler wiring human Mattermost messages into the control plane."""

    async def handler(message: dict) -> None:
        msg_type = message.get("type")
        try:
            if msg_type == "chat":
                await _executive_respond(svc, message)
            elif msg_type == "goal":
                home = svc.mattermost._logical_for(message.get("channel", ""))
                result = await svc.engine.execute_goal(message.get("text", ""),
                                                       user_id=message.get("user_id", "human"),
                                                       home_channel=home)
                if svc.mattermost:
                    line = (f"🚀 Project `{result['project_id']}` created — "
                            f"workflow run `{result['run_id']}` status: {result['status']}")
                    await svc.mattermost.post_to("executive", line)
                    # checkpoint where the human asked for it
                    home = svc.mattermost._logical_for(message.get("channel", ""))
                    svc.mattermost._project_channels[result["project_id"]] = home
                    if home != "executive":
                        await svc.mattermost.post_to(home, line)
            elif msg_type == "approval":
                decision = message.get("decision", "approved")
                await svc.engine.approve(message.get("approval_id", ""), decision,
                                         decided_by=message.get("user_id", "human"))
            elif msg_type == "retry":
                task_id = message.get("task_id", "")
                if task_id:
                    task = await svc.tasks.get(task_id)
                    if task and task.status.value in ("failed", "cancelled"):
                        await svc.tasks.set_status(task_id, task.status)  # touch
                        await svc.queue.enqueue({"task_id": task_id})
        except Exception as exc:  # noqa: BLE001
            logger.exception("command handling failed")
            if svc.mattermost:
                await svc.mattermost.post_to("system-errors", f"Command failed: {exc}")

    return handler


async def _executive_respond(svc: Services, message: dict) -> None:
    """The executive decides: is this message real work (GOAL) or chat?

    Chat gets a personal reply; GOAL runs the full agency (dynamic planning,
    director gate, workers) with checkpoints mirrored to the asking channel."""
    text = (message.get("text") or "").strip()
    if not text or svc.mattermost is None or not svc.mattermost.available:
        return
    from agentos.agents.hierarchy import ORG_AGENTS
    from agentos.agents.identity import identity_prompt_block
    from agentos.agents.personas import token_for_agent, username_for
    from agentos.domain.models import ModelRequest

    executive = ORG_AGENTS.get("executive")
    if executive is None:
        return
    model_id, _reason = await svc.router.route(
        executive, None, description=text, project_id=None)
    model_def = await svc.model_registry.get(model_id) if model_id else None
    provider = svc.providers.get(model_def.provider) if model_def else None
    channel = svc.mattermost._logical_for(message.get("channel", ""))
    if provider is None:
        await svc.mattermost.post_to(
            channel, "_(no model available right now — try again in a moment)_")
        return

    # decision pass: real work or just conversation?
    decision = ""
    try:
        decide_req = ModelRequest(
            model_id=model_id,
            system=("You are Alex, the Executive Director of an AI agency. The boss "
                    "just sent you a message in Mattermost. Decide whether it asks you "
                    "to actually DO work (research, build, plan, create, write, launch "
                    "something) or is just conversation. Reply with exactly one word: "
                    "GOAL or CHAT."),
            messages=[{"role": "user", "content": text}],
            agent_id="executive", temperature=0.1, max_tokens=10)
        resp = await provider.complete(decide_req)
        decision = (resp.content or "").strip().upper()
    except Exception as exc:  # noqa: BLE001
        logger.warning("executive decision call failed: %s", exc)

    if decision.startswith("GOAL"):
        result = await svc.engine.execute_goal(text,
                                               user_id=message.get("user_id", "human"),
                                               home_channel=channel)
        line = (f"🚀 Project `{result['project_id']}` created — "
                f"workflow run `{result['run_id']}` status: {result['status']}")
        await svc.mattermost.post_to("executive", line)
        svc.mattermost._project_channels[result["project_id"]] = channel
        if channel != "executive":
            await svc.mattermost.post_to(channel, line)
        return

    system = identity_prompt_block(getattr(executive, "identity", None))
    system += ("\n\nYou are Alex, the Executive Director, talking to the boss in "
               "Mattermost. Reply directly, briefly and naturally. If you do decide "
               "the request is real work, say so and actually run it.")
    req = ModelRequest(model_id=model_id, system=system,
                       messages=[{"role": "user", "content": text}],
                       agent_id="executive")
    try:
        resp = await provider.complete(req)
        reply = (resp.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        reply = f"_(sorry — the model call failed: {exc})_"
    if not reply:
        return
    token = token_for_agent("executive")
    if token:
        await svc.mattermost.post_as_user(username_for("executive"), token, reply, channel)
    else:
        await svc.mattermost.post_to(channel, reply)