"""Benchmark runner.

Executes a regression dataset through the real engine path (task → agent →
model → tools → result), evaluates every run with the deterministic
evaluator (+ optional LLM judge), persists records, updates performance
stats, and produces a comparison summary by agent and by model.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agentos.domain.models import (
    EvaluationRecord,
    EvaluationRunSummary,
    TaskStatus,
    new_id,
)

from .datasets import RegressionDataset, load_dataset
from .deterministic import DeterministicEvaluator
from .judge import LLMJudgeEvaluator


class BenchmarkRunner:
    def __init__(self, svc: Any, *, use_judge: bool = True,
                 judge_tier: str = "t3") -> None:
        self.svc = svc
        self.deterministic = DeterministicEvaluator()
        self.judge = LLMJudgeEvaluator(svc.router, judge_tier=judge_tier) if use_judge else None

    async def run_dataset(self, dataset: RegressionDataset | str,
                          *, agent_override: Optional[str] = None,
                          model_override: Optional[str] = None,
                          project_name: str = "eval-benchmark") -> EvaluationRunSummary:
        if isinstance(dataset, str):
            loaded = load_dataset(dataset)
            if loaded is None:
                raise KeyError(f"dataset {dataset} not found")
            dataset = loaded
        project = await self.svc.projects.create(
            f"{project_name}-{dataset.name}", objective=f"Benchmark: {dataset.name}",
            created_by="evaluation")
        summary = EvaluationRunSummary(dataset=dataset.name, total=len(dataset.items))
        start = time.perf_counter()
        for item in dataset.items:
            record = await self._run_item(project, item, agent_override, model_override)
            summary.total += 0  # already set
            await self.svc.entity_store.save("evaluation", record)
            if record.passed:
                summary.passed += 1
            summary.avg_score += record.score
            summary.total_cost += record.cost
            summary.avg_latency_ms += record.latency_ms
            if record.fail_class:
                summary.by_fail_class[record.fail_class] = \
                    summary.by_fail_class.get(record.fail_class, 0) + 1
            if record.agent_id:
                bucket = summary.by_agent.setdefault(record.agent_id, {"runs": 0, "passed": 0, "score": 0.0})
                bucket["runs"] += 1
                bucket["passed"] += int(record.passed)
                bucket["score"] += record.score
            if record.model_id:
                bucket = summary.by_model.setdefault(record.model_id, {"runs": 0, "passed": 0, "score": 0.0})
                bucket["runs"] += 1
                bucket["passed"] += int(record.passed)
                bucket["score"] += record.score
            if record.skill_id:
                bucket = summary.by_skill.setdefault(record.skill_id, {"runs": 0, "passed": 0, "score": 0.0})
                bucket["runs"] += 1
                bucket["passed"] += int(record.passed)
                bucket["score"] += record.score
            await self.svc.performance.record_evaluation(record)
        n = max(len(dataset.items), 1)
        summary.avg_score = round(summary.avg_score / n, 2)
        summary.avg_latency_ms = int(summary.avg_latency_ms / n)
        summary.total_cost = round(summary.total_cost, 4)
        for bucket in summary.by_agent.values():
            bucket["score"] = round(bucket["score"] / max(bucket["runs"], 1), 2)
        for bucket in summary.by_model.values():
            bucket["score"] = round(bucket["score"] / max(bucket["runs"], 1), 2)
        for bucket in summary.by_skill.values():
            bucket["score"] = round(bucket["score"] / max(bucket["runs"], 1), 2)
        summary.ts = datetime.now(timezone.utc)
        await self.svc.entity_store.save("eval_runs", summary)
        await self.svc.events.publish("evaluation.run_completed", {
            "dataset": dataset.name, "passed": summary.passed,
            "total": summary.total, "avg_score": summary.avg_score,
            "total_cost": summary.total_cost}, source="evaluation")
        return summary

    async def _run_item(self, project: Any, item: Any,
                        agent_override: Optional[str], model_override: Optional[str]) -> EvaluationRecord:
        agent_id = agent_override or item.agent or "test-engineer"
        task = await self.svc.tasks.create(
            project.project_id, title=item.title, description=item.description,
            assigned_agent=agent_id, priority=item.priority, created_by="evaluation")
        start = time.perf_counter()
        try:
            outcome = await self.svc.engine.run_single_task(task.task_id)
        except Exception as exc:  # noqa: BLE001
            from agentos.agents.runtime import AgentRunResult

            outcome = AgentRunResult(error=f"run crashed: {exc}")
            await self.svc.tasks.set_status(task.task_id, TaskStatus.FAILED,
                                            error=str(exc))
        latency = int((time.perf_counter() - start) * 1000)
        record = await self.deterministic.evaluate(task, outcome)
        record.latency_ms = latency
        if record.passed and item.expected_markers:
            missing = [m for m in item.expected_markers
                       if m.lower() not in (outcome.content or "").lower()]
            if missing:
                record.score = max(record.score - 1.0, 0.0)
                record.passed = record.score >= 3.0
                record.reasons.append(f"missing expected markers: {missing}")
                record.verdict = "pass" if record.passed else "fail"
        if self.judge is not None:
            judge_record = await self.judge.evaluate(task, outcome)
            if judge_record.evaluator == self.judge.name:
                # blend judge with deterministic for the final record
                record.score = round((record.score + judge_record.score) / 2, 2)
                record.passed = record.passed and judge_record.passed
                record.verdict = "pass" if record.passed else "fail"
                record.evaluator = f"deterministic+{self.judge.name}"
                record.cost += judge_record.cost
                record.reasons.extend(f"[judge] {r}" for r in judge_record.reasons[:2])
        if outcome.model:
            record.model_id = outcome.model
        record.agent_id = agent_id
        record.skill_id = item.skill
        record.fail_class = outcome.fail_class
        record.task_id = task.task_id
        record.project_id = project.project_id
        return record