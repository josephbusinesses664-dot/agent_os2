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


@pytest.mark.asyncio
async def test_admin_ui_renders_security_timeline_workspace_views(svc):
    """Admin UI ships the new Governance/Observability views and their data
    endpoints respond (master spec §36: security, mission, workspaces)."""
    from agentos.api.main import create_app

    app = create_app(svc)
    with TestClient(app) as client:
        html = client.get("/static/index.html").text
        for token in ["view-security", "view-timeline", "view-workspaces",
                      "loadSecurity", "loadTimeline", "loadWorkspaces",
                      "Mission Timeline"]:
            assert token in html, f"admin UI missing {token}"

        policies = client.get("/api/security/policies").json()
        assert len(policies) >= 4
        kinds = {p["kind"] for p in policies}
        assert kinds >= {"deny_tool", "require_approval"}

        em = client.get("/api/security/emergency")
        assert em.status_code == 200 and em.json()["engaged"] is False
        # engage → all agents are stopped → disengage
        engaged = client.post("/api/security/emergency",
                              json={"operator": "admin-ui", "reason": "test"})
        assert engaged.json()["engaged"] is True
        denied = client.get("/api/security/emergency")
        assert denied.json()["engaged"] is True
        released = client.delete("/api/security/emergency?operator=admin-ui")
        assert released.json()["engaged"] is False

        assert client.get("/api/workspaces").json() == []
        # create a workspace against a real project (plain-dir sandbox is fine)
        run = await svc.engine.execute_goal("Admin UI workspace target",
                                            user_id="test", workflow_id="discovery")
        project = client.get("/api/projects").json()[0]
        assert project["project_id"] == run["project_id"]
        created = client.post("/api/workspaces",
                              json={"project_id": project["project_id"]})
        assert created.status_code == 200
        ws = created.json()
        assert ws["status"] in ("isolated", "working")
        assert client.get("/api/workspaces").json()[0]["workspace_id"] == ws["workspace_id"]
        # diff endpoint is reachable; integrating a sandbox without changes is a no-op
        assert client.get(f"/api/workspaces/{ws['workspace_id']}/status").status_code == 200
        integ = client.post(f"/api/workspaces/{ws['workspace_id']}/integrate",
                            json={"message": "admin test"})
        assert integ.status_code in (200, 409)
        assert client.get(f"/api/workspaces/{ws['workspace_id']}/status").status_code in (200, 404)