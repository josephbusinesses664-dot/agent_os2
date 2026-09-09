"""Standing-mission visibility tests: schedule events routed to Mattermost
channels, the ChurchApp benchmark dataset, and schedule status on /api/status.
"""

import pytest

from agentos.domain.models import Event
from agentos.evaluation.datasets import load_dataset, list_datasets
from agentos.integrations.mattermost.service import MattermostService


class _FakeClient:
    """Minimal Mattermost client stand-in (posts recorded in order)."""

    def __init__(self):
        self.posts = []          # list of (channel, message)
        self.channels = {}
        self.team_id = "team1"
        self.bot_id = "bot1"

    async def me(self):
        return {"id": self.bot_id, "username": "agent-os"}

    async def health(self):
        return True

    async def get_team_by_name(self, name):
        return {"id": self.team_id, "name": name}

    async def create_team(self, name, display_name):
        return {"id": self.team_id, "name": name}

    async def get_channel_by_name(self, team_id, name):
        return self.channels.get(name)

    async def create_channel(self, team_id, name, display_name, purpose=""):
        self.channels[name] = {"id": f"ch-{name}"}
        return self.channels[name]

    async def post(self, channel_id, message, root_id=None):
        self.posts.append((channel_id, message))
        return {"id": f"p{len(self.posts)}"}

    async def posts_after(self, channel_id, since):
        return []

    async def get_user_by_username(self, username):
        return None


class _FakeSettings:
    mattermost_team = "agents"
    mattermost_bot_token = "x"
    mattermost_bot_name = "agent-os"
    mattermost_channel = "town-square"


def _service() -> MattermostService:
    svc = MattermostService(_FakeSettings(), _FakeClient())
    assert _run(svc.connect())
    _run(svc.ensure_workspace())
    return svc


async def _route(svc, event_type: str, payload: dict | None = None):
    await svc.route_event(Event(type=event_type, payload=payload or {},
                                source="scheduler"))


def test_schedule_events_route_to_operations_channel():
    svc = _service()
    _run(_route(svc, "schedule.created",
                {"name": "Weekly research", "interval_seconds": 604800,
                 "schedule_id": "sched1"}))
    _run(_route(svc, "schedule.fired",
                {"schedule_id": "sched1", "run_count": 3}))
    channels = [c for c, _ in svc.client.posts]
    assert channels == [svc.channels["operations"], svc.channels["operations"]]


def test_schedule_failed_routes_to_system_errors():
    svc = _service()
    _run(_route(svc, "schedule.failed",
                {"schedule_id": "sched1", "error": "model timeout"}))
    assert svc.client.posts[0][0] == svc.channels["system-errors"]
    assert "model timeout" in svc.client.posts[0][1]


def test_deadline_exceeded_escalates_to_system_errors():
    svc = _service()
    _run(_route(svc, "task.deadline_exceeded",
                {"task_id": "task9", "deadline": "2026-09-07T12:00:00"}))
    assert svc.client.posts[0][0] == svc.channels["system-errors"]
    assert "task9" in svc.client.posts[0][1]


def test_unknown_event_ignored():
    svc = _service()
    _run(_route(svc, "something.unknown", {}))
    assert svc.client.posts == []


def test_churchapp_dataset_loads_with_real_agents():
    ds = load_dataset("churchapp")
    assert ds is not None
    assert len(ds.items) == 9
    # every stage targets an agent that exists in the built-in hierarchy
    from agentos.agents.hierarchy import ORG_AGENTS
    ids = set(ORG_AGENTS)
    for item in ds.items:
        assert item.agent in ids, f"{item.agent} not in hierarchy"
        assert item.expected_markers, f"{item.title} lacks evidence markers"
        assert item.required_artifacts >= 1


def test_churchapp_dataset_has_full_mission_coverage():
    ds = load_dataset("churchapp")
    titles = " ".join(i.title for i in ds.items).lower()
    for stage in ("market", "requirement", "architecture", "backend",
                  "frontend", "test", "security", "deployment", "document"):
        assert stage in titles, f"missing stage: {stage}"


def test_churchapp_in_dataset_list():
    names = list_datasets()
    assert "churchapp" in names


def test_status_includes_schedules(svc):
    _run(svc.scheduler.create("nightly", "recurring scan",
                              interval_seconds=3600, max_runs=2))
    st = _status(svc)
    assert st["schedules"] >= 1
    assert st["schedules_enabled"] >= 1


# -- tiny asyncio helpers (kept local to avoid conftest assumptions) --------

def _run(coro):
    import asyncio
    try:
        return asyncio.get_event_loop().run_until_complete(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _status(svc):
    from agentos.api.main import create_app
    app = create_app(svc)
    from starlette.testclient import TestClient
    with TestClient(app) as client:
        return client.get("/api/status").json()