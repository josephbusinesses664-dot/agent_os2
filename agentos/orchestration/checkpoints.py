"""Durable LangGraph checkpointer.

The default MemorySaver loses every checkpoint on restart — a workflow
paused at an approval gate, or interrupted mid-run, could not resume. This
checkpointer persists each checkpoint (the full LangGraph state) through the
same EntityStore every other organizational object uses, keyed by
thread_id + checkpoint_ns + checkpoint_id, with a per-thread `latest`
pointer. In-memory repository in tests/offline, Postgres in production —
identical behavior, durable in the latter.

Long-horizon missions survive restarts: a run that was interrupted keeps its
checkpoints and can be resumed from where it stopped.
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

_COLLECTION = "checkpoints"
_LATEST = "checkpoint_latest"


class DurableCheckpointer(BaseCheckpointSaver):
    """Entity-store-backed checkpointer (async, used by the ainvoke path)."""

    def __init__(self, store: Any) -> None:
        super().__init__()
        self.store = store

    @staticmethod
    def _key(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{thread_id}:{checkpoint_ns}:{checkpoint_id}"

    # -- write --------------------------------------------------------------
    async def aput(self, config: Any, checkpoint: Any, metadata: Any,
                   new_versions: Any) -> dict:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config.get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        await self.store.put_doc(
            _COLLECTION,
            self._key(thread_id, checkpoint_ns, checkpoint_id),
            {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns,
             "checkpoint_id": checkpoint_id, "checkpoint": checkpoint,
             "metadata": metadata, "new_versions": new_versions})
        await self.store.put_doc(
            _LATEST, thread_id,
            {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns,
             "checkpoint_id": checkpoint_id})
        return {"configurable": {**config.get("configurable", {}),
                                 "checkpoint_id": checkpoint_id,
                                 "checkpoint_ns": checkpoint_ns}}

    async def aput_writes(self, config: Any, writes: Any, task_id: str,
                          task_path: str = "") -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config.get("checkpoint_ns", "")
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id", "pending")
        await self.store.put_doc(
            _COLLECTION,
            self._key(thread_id, checkpoint_ns, checkpoint_id) + f":writes:{task_id}",
            {"writes": [[w[0], w[1]] for w in writes]})

    # -- read ---------------------------------------------------------------
    async def aget_tuple(self, config: Any) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config.get("checkpoint_ns", "")
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
        if not checkpoint_id:
            latest = await self.store.get_doc(_LATEST, thread_id)
            if not latest:
                return None
            checkpoint_ns = latest.get("checkpoint_ns", "")
            checkpoint_id = latest["checkpoint_id"]
        doc = await self.store.get_doc(
            _COLLECTION, self._key(thread_id, checkpoint_ns, checkpoint_id))
        if not doc:
            return None
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id,
                                     "checkpoint_ns": checkpoint_ns,
                                     "checkpoint_id": checkpoint_id}},
            checkpoint=doc["checkpoint"],
            metadata=doc["metadata"],
            parent_config=None,
            pending_writes=[],
        )

    # -- listing (observability) ---------------------------------------------
    async def list_threads(self, limit: int = 50) -> list[dict]:
        docs = await self.store.list_docs(_LATEST)
        docs.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
        return docs[:limit]

    async def checkpoint_count(self) -> int:
        return len(await self.store.list_docs(_COLLECTION))