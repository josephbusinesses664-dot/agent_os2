"""Control-plane API extras: opt-in Bearer auth gate + mission timeline."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_api_auth_gate_enforces_token(tmp_path):
    from agentos.config import Settings
    from agentos.orchestration.engine import OrchestratorEngine
    from agentos.services import Services

    settings = Settings(database_url=None, redis_url=None,
                        workspace_dir=str(tmp_path / "ws"),
                        api_auth_token="sekret-token")
    svc = Services(settings)
    await svc.seed()
    svc.engine = OrchestratorEngine(svc)
    from agentos.api.main import create_app

    app = create_app(svc)
    try:
        with TestClient(app) as client:
            # protected routes refuse without the token
            assert client.get("/api/status").status_code == 401
            assert client.get("/api/agents").status_code == 401
            assert client.get("/api/status", headers={
                "Authorization": "Bearer wrong"}).status_code == 401
            # the right token passes
            ok = client.get("/api/status",
                            headers={"Authorization": "Bearer sekret-token"})
            assert ok.status_code == 200
            # health stays open for load balancers, admin UI stays open
            assert client.get("/api/health").status_code == 200
            assert client.get("/").status_code == 200
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_mission_timeline_reconstructs_run(svc):
    from agentos.api.main import create_app

    app = create_app(svc)
    with TestClient(app) as client:
        run = await svc.engine.execute_goal("Timeline test mission",
                                            user_id="test", workflow_id="discovery")
        project_id = run["project_id"]
        response = client.get(f"/api/projects/{project_id}/timeline")
        assert response.status_code == 200
        data = response.json()
        assert data["project_id"] == project_id
        assert data["status"] == "completed"
        assert data["count"] >= 5, "a full run must leave a rich timeline"
        kinds = {item["kind"] for item in data["timeline"]}
        assert "task" in kinds and "event" in kinds
        # chronological order
        stamps = [item["ts"] for item in data["timeline"]]
        assert stamps == sorted(stamps)

        # unknown project → 404
        assert client.get("/api/projects/nope/timeline").status_code == 404