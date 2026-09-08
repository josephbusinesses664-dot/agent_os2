"""Control-plane API tests for schedules (recurring/standing missions)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_schedule_crud_roundtrip(svc):
    from agentos.api.main import create_app
    app = create_app(svc)
    with TestClient(app) as client:
        # create
        r = client.post("/api/schedules", json={
            "name": "Weekly Market Watch", "goal": "Research the market weekly",
            "interval_seconds": 604800, "max_runs": 4,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schedule_id"].startswith("sched_")
        assert body["enabled"] is True
        sid = body["schedule_id"]

        # list
        rows = client.get("/api/schedules").json()
        assert any(x["schedule_id"] == sid for x in rows)

        # pause
        patched = client.patch(f"/api/schedules/{sid}",
                               json={"enabled": False}).json()
        assert patched["enabled"] is False

        # change interval
        patched = client.patch(f"/api/schedules/{sid}",
                               json={"interval_seconds": 3600}).json()
        assert patched["interval_seconds"] == 3600

        # delete
        assert client.delete(f"/api/schedules/{sid}").json() == {"deleted": sid}
        assert client.delete(f"/api/schedules/{sid}").status_code == 404


@pytest.mark.asyncio
async def test_schedule_run_now_returns_run(svc):
    from agentos.api.main import create_app
    app = create_app(svc)
    with TestClient(app) as client:
        r = client.post("/api/schedules", json={
            "name": "One-shot", "goal": "Produce a one-line brief",
            "interval_seconds": 60, "workflow_id": "build_feature",
            "max_runs": 1,
        })
        sid = r.json()["schedule_id"]
        fired = client.post(f"/api/schedules/{sid}/run").json()
        assert fired.get("run_id"), fired
