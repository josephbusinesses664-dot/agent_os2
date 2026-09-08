"""Task Service.

Every meaningful task has an id, project, parent, assigned agent, status,
dependencies, budget, artifacts and review status. A dependency graph drives
automatic unblocking: when a task completes, tasks waiting on it become
runnable.
"""

from __future__ import annotations

from typing import Any, Optional

from agentos.db.store import EntityStore
from agentos.domain.models import Task, TaskStatus, new_id


class TaskService:
    def __init__(self, store: EntityStore, event_bus: Optional[Any] = None) -> None:
        self.store = store
        self._collection = "tasks"
        self._events = event_bus

    async def create(self, project_id: str, title: str, description: str = "", *,
                     assigned_agent: Optional[str] = None, parent_task: Optional[str] = None,
                     dependencies: Optional[list[str]] = None, priority: str = "normal",
                     budget: Optional[float] = None, created_by: str = "executive") -> Task:
        task = Task(task_id=new_id("task"), project_id=project_id, title=title,
                    description=description, assigned_agent=assigned_agent,
                    parent_task=parent_task, dependencies=dependencies or [],
                    priority=priority, budget=budget, created_by=created_by)
        await self.store.save(self._collection, task)
        return task

    async def get(self, task_id: str) -> Optional[Task]:
        return await self.store.get(self._collection, task_id, Task)

    async def require(self, task_id: str) -> Task:
        task = await self.get(task_id)
        if not task:
            raise KeyError(f"task {task_id} not found")
        return task

    async def save(self, task: Task) -> None:
        task.touch()
        await self.store.save(self._collection, task)

    async def by_project(self, project_id: str) -> list[Task]:
        tasks = await self.store.list(self._collection, Task,
                                      predicate={"project_id": project_id})
        tasks.sort(key=lambda t: t.created_at)
        return tasks

    async def list(self, status: Optional[TaskStatus] = None,
                   project_id: Optional[str] = None, limit: int = 500) -> list[Task]:
        predicate: dict = {}
        if status:
            predicate["status"] = status.value
        if project_id:
            predicate["project_id"] = project_id
        tasks = await self.store.list(self._collection, Task, predicate=predicate)
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]

    async def set_status(self, task_id: str, status: TaskStatus, *,
                         error: Optional[str] = None, result: Optional[str] = None) -> Task:
        task = await self.require(task_id)
        prev = task.status
        task.status = status
        if error is not None:
            task.error = error
        if result is not None:
            task.result = result
        await self.save(task)
        if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            await self._unblock_dependents(task_id, status)
        if self._events is not None and status != prev:
            try:
                await self._events.publish(
                    "task.status_changed",
                    {"task": task.title, "from": prev.value, "to": status.value,
                     "error": error, "agent": task.assigned_agent},
                    task_id=task_id, project_id=task.project_id,
                    agent_id=task.assigned_agent,
                    severity="error" if status in (TaskStatus.FAILED,) else "info")
            except Exception:  # noqa: BLE001
                pass
        return task

    async def _unblock_dependents(self, task_id: str, status: TaskStatus) -> None:
        if status != TaskStatus.COMPLETED:
            return
        tasks = await self.store.list(self._collection, Task)
        for doc in tasks:
            task = Task.model_validate(doc)
            if task.status == TaskStatus.BLOCKED and task_id in task.dependencies:
                remaining = [d for d in task.dependencies
                             if d != task_id and not await self._is_done(d)]
                if not remaining:
                    task.status = TaskStatus.PENDING
                    await self.save(task)

    async def _is_done(self, task_id: str) -> bool:
        task = await self.get(task_id)
        return bool(task and task.status == TaskStatus.COMPLETED)

    async def runnable_tasks(self, project_id: str) -> list[Task]:
        """Tasks that are pending/queued with all dependencies complete."""
        tasks = await self.by_project(project_id)
        out = []
        for task in tasks:
            if task.status not in (TaskStatus.PENDING, TaskStatus.QUEUED):
                continue
            if all([await self._is_done(d) for d in task.dependencies]):
                out.append(task)
        return out

    async def delegate(self, task_id: str, agent_id: str) -> Task:
        task = await self.require(task_id)
        task.assigned_agent = agent_id
        task.status = TaskStatus.QUEUED
        await self.save(task)
        return task

    async def record_artifact(self, task_id: str, artifact: str) -> Task:
        task = await self.require(task_id)
        if artifact not in task.artifacts:
            task.artifacts.append(artifact)
        await self.save(task)
        return task