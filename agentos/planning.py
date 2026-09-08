"""Dynamic planning (autonomous workflow intelligence).

The executive does not blindly run every stage of a fixed workflow. The
DynamicPlanner decides the required stages from the goal:

* an explicit `PLANNED_STAGES: [...]` marker in the goal (model-written plan)
  is honored verbatim,
* otherwise a keyword-scored stage selector picks only the stages the goal
  actually needs (base understanding + report always; specialists matched by
  topic),
* a goal that asks for the full pipeline (major project / launch / full
  product) expands to the complete 0→100-style stage set.

The result is a WorkflowDef assembled from reusable stage templates
(workflows/stage_templates.yaml) — same engine, same approval gates, same
artifacts convention. The full 0→100 workflow remains available by name.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

from agentos.domain.models import WorkflowDef, WorkflowStage

_MARKER_RE = re.compile(r"PLANNED_STAGES\s*:\s*\[([^\]]*)\]", re.IGNORECASE)
# artifact path inside a stage template's PLANNED_TOOL_CALLS filesystem.write
_ARTIFACT_RE = re.compile(r'"path"\s*:\s*"([^"]+)"', re.IGNORECASE)


def _template_artifact(description: str) -> str:
    """Extract the planned artifact path (e.g. artifacts/research.md) from a
    stage template's description. The engine guarantees this file lands on
    disk after the stage, even when the model never issues the write."""
    m = _ARTIFACT_RE.search(description or "")
    return m.group(1).strip() if m else ""

# keyword → stage ids. Order matters: more specific first.
_KEYWORD_STAGES: list[tuple[tuple[str, ...], list[str]]] = [
    (("full", "saas", "product", "launch", "major", "complete", "agency"),
     ["research", "community", "requirements", "design", "implement-frontend",
      "implement-backend", "testing", "security", "review", "deploy"]),
    (("landing", "website", "frontend", "ui", "page", "web app", "react", "next"),
     ["design", "implement-frontend", "testing", "review"]),
    (("api", "backend", "server", "database", "service", "integration"),
     ["implement-backend", "testing", "review"]),
    (("research", "market", "validate", "demand", "competitor", "community"),
     ["research", "community"]),
    (("security", "audit", "secrets", "pentest"),
     ["security", "review"]),
    (("bug", "debug", "fix", "broken", "error"),
     ["implement-frontend", "implement-backend", "testing"]),
    (("content", "copy", "marketing", "seo", "campaign"),
     ["design", "implement-frontend", "review"]),
]


def _parse_marker(goal: str) -> Optional[list[str]]:
    m = _MARKER_RE.search(goal)
    if not m:
        return None
    stages = [s.strip().lower() for s in m.group(1).split(",") if s.strip()]
    return stages or None


def _keyword_stages(goal: str) -> list[str]:
    low = goal.lower()
    for keywords, stages in _KEYWORD_STAGES:
        if any(k in low for k in keywords):
            return list(stages)
    # generic small task: the minimal useful pipeline
    return ["requirements", "implement-frontend", "testing", "review"]


class DynamicPlanner:
    def __init__(self, templates_root: Optional[Path] = None) -> None:
        self.templates_root = templates_root or (
            Path(__file__).resolve().parents[1] / "workflows")
        self._templates: Optional[dict] = None

    # -- template loading ----------------------------------------------------
    def _load_templates(self) -> dict:
        if self._templates is None:
            path = self.templates_root / "stage_templates.yaml"
            self._templates = yaml.safe_load(path.read_text()) or {}
        return self._templates

    def available_stages(self) -> list[str]:
        return sorted(self._load_templates().keys())

    # -- planning ------------------------------------------------------------
    def plan_stages(self, goal: str, *, expand_full: bool = False) -> list[str]:
        """Decide which stages this goal needs, in order.

        The executive frames every run (understanding → selected stages →
        report); only the *middle* is goal-dependent — simple requests never
        blindly run every stage."""
        explicit = _parse_marker(goal)
        if explicit:
            middle = explicit
        elif expand_full:
            middle = [s for s in self.available_stages()
                      if s not in ("understanding", "report")]
        else:
            middle = _keyword_stages(goal)
        stages = list(dict.fromkeys(["understanding"] + middle))
        if "report" not in stages:
            stages.append("report")
        return stages

    def build_workflow(self, goal: str, stages: Optional[list[str]] = None,
                       workflow_id: str = "dynamic") -> WorkflowDef:
        """Assemble a WorkflowDef from templates for the chosen stages."""
        chosen = stages or self.plan_stages(goal)
        templates = self._load_templates()
        unknown = [s for s in chosen if s not in templates]
        if unknown:
            raise KeyError(f"unknown stage template(s): {unknown}")
        built: list[WorkflowStage] = []
        for stage_id in chosen:
            tpl = templates[stage_id]
            built.append(WorkflowStage(
                stage_id=stage_id,
                name=tpl.get("name", stage_id),
                agent_role=tpl["agent_role"],
                description=tpl.get("description", ""),
                requires_approval=bool(tpl.get("requires_approval", False)),
                artifact_prefix=_template_artifact(tpl.get("description", "")),
            ))
        for i, stage in enumerate(built):
            if i + 1 < len(built):
                stage.next = built[i + 1].stage_id
        return WorkflowDef(
            workflow_id=f"{workflow_id}_{len(built)}",
            name=f"Dynamic plan: {goal[:60]}",
            description=f"Stages selected for: {goal[:200]}",
            entry_stage=built[0].stage_id if built else "understanding",
            stages=built,
            version="1.0.0",
            source_path="dynamic",
        )

    def plan_summary(self, goal: str) -> dict:
        stages = self.plan_stages(goal)
        return {
            "goal": goal[:200],
            "stages": stages,
            "count": len(stages),
            "agents": list(dict.fromkeys(
                self._load_templates()[s]["agent_role"] for s in stages)),
        }