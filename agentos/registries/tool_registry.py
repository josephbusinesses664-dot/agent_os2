"""Tool Registry.

Holds tool definitions (permission key, risk level, category) and the handler
callables. Custom tools can be registered at runtime without touching core.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from agentos.db.store import EntityStore
from agentos.domain.models import ToolDef
from agentos.tools.builtin import HANDLERS, load_builtin_defs

ToolHandler = Callable[[object, dict], Awaitable[dict]]

# Models hallucinate tool names; these aliases map the names agents actually
# emit (often the dot-sanitized schema name, e.g. project_state) onto the real
# tool. Resolution happens in get()/handler() so every pathway benefits.
ALIASES: dict[str, str] = {
    # dot-sanitized schema names (DeepSeek rejects dots)
    "project_state": "project.state",
    "repo_tree": "repo.tree",
    "repo_search": "repo.search",
    "filesystem_read": "filesystem.read",
    "filesystem_write": "filesystem.write",
    "file_read": "filesystem.read",
    "file_write": "filesystem.write",
    "memory_recall": "memory.recall",
    "memory_save": "memory.save",
    "web_search": "web.search",
    "web_scrape": "web.scrape",
    "mcp_call": "mcp.call",
    "mcp_select": "mcp.select",
    "api_call": "api.call",
    "deploy_github": "deploy.github",
    "render_manage": "render.manage",
    "postgres_query": "postgres.query",
    "db_query": "db.query",
    "agent_delegate": "agent.delegate",
    "agent_challenge": "agent.challenge",
    "agent_resolve": "agent.resolve",
    "browser_open": "browser.open",
    "browser_snapshot": "browser.snapshot",
    "browser_click": "browser.click",
    "browser_type": "browser.type",
    "browser_evaluate": "browser.evaluate",
    "browser_screenshot": "browser.screenshot",
    "browser_close": "browser.close",
    "tool_discover": "tool.discover",
    "tool_health": "tool.health",
    "mattermost_post": "mattermost.post",
    "hn_search": "hn.search",
    "reddit_search": "reddit.search",
    # architecture-style guesses from live runs
    "arch.tree": "repo.tree",
    "arch.explore": "repo.tree",
    "arch": "repo.tree",
    "git_status": "git.status",
    "git": "git.status",
    # (git.log / git.branch / git.diff are commands OF git.status, not tools;
    # the executor's closest-match hint steers models there)
}


def resolve_alias(name: str) -> str:
    return ALIASES.get(name, name)


class ToolRegistry:
    def __init__(self, store: EntityStore) -> None:
        self.store = store
        self._collection = "tools"
        self._handlers: dict[str, ToolHandler] = dict(HANDLERS)
        self._health_checks: dict[str, ToolHandler] = {}

    async def seed_defaults(self) -> int:
        existing = await self.list()
        if existing:
            return len(existing)
        for tool in load_builtin_defs().values():
            await self.store.save(self._collection, tool)
        return len(load_builtin_defs())

    async def list(self, enabled_only: bool = True) -> list[ToolDef]:
        tools = await self.store.list(self._collection, ToolDef)
        tools.sort(key=lambda t: t.name)
        return [t for t in tools if t.enabled or not enabled_only]

    async def get(self, name: str) -> Optional[ToolDef]:
        return await self.store.get(self._collection, resolve_alias(name), ToolDef)

    async def register(self, tool: ToolDef, handler: Optional[ToolHandler] = None,
                       health_check: Optional[ToolHandler] = None) -> ToolDef:
        await self.store.save(self._collection, tool)
        if handler:
            self._handlers[tool.name] = handler
        if health_check:
            self._health_checks[tool.name] = health_check
        return tool

    def handler(self, name: str) -> Optional[ToolHandler]:
        return self._handlers.get(resolve_alias(name))

    async def set_enabled(self, name: str, enabled: bool) -> ToolDef:
        tool = await self.get(name)
        if not tool:
            raise KeyError(f"tool {name} not found")
        tool.enabled = enabled
        await self.store.save(self._collection, tool)
        return tool

    # -- capability-based discovery -----------------------------------------
    async def discover(self, query: str = "", agent: Any = None,
                       limit: int = 10) -> list[ToolDef]:
        """Rank tools by relevance to a capability need (description + category
        + name tokens), filtered by the agent's permission policy. Keeps the
        tool list an agent sees proportional to the task, not the catalog."""
        import re as _re

        tools = await self.list(enabled_only=True)
        if agent is not None:
            tools = [t for t in tools if agent.allows(t.permission_key or t.name)]
        if not query:
            return tools[:limit]
        terms = {t for t in _re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2}
        scored: list[tuple[float, ToolDef]] = []
        for tool in tools:
            haystack = _re.findall(r"[a-z0-9]+",
                                   f"{tool.name} {tool.description} {tool.category}".lower())
            score = sum(1 for t in terms if t in haystack)
            if score:
                scored.append((score, tool))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [t for _, t in scored[:limit]] or tools[:limit]

    # -- health checks ------------------------------------------------------
    def register_health(self, name: str, check: ToolHandler) -> None:
        self._health_checks[name] = check

    async def health(self, name: str = "") -> dict:
        """Probe a tool (or all enabled tools): handler present, enabled,
        optional live check. Returns structured health reports."""
        if name:
            tool = await self.get(name)
            if not tool:
                return {"tool": name, "status": "unknown", "detail": "not registered"}
            return await self._health_one(tool)
        reports = []
        for tool in await self.list(enabled_only=True):
            reports.append(await self._health_one(tool))
        ok = sum(1 for r in reports if r["status"] == "ok")
        return {"status": "ok" if ok == len(reports) else "degraded",
                "healthy": ok, "total": len(reports), "tools": reports}

    async def _health_one(self, tool: ToolDef) -> dict:
        check = self._health_checks.get(tool.name)
        if tool.risk_level == "high" and not check:
            return {"tool": tool.name, "status": "ok", "detail": "high-risk (approval-gated), no live probe"}
        if check is None:
            return {"tool": tool.name, "status": "ok", "detail": "registered"}
        try:
            result = await check(None, {})
            return {"tool": tool.name,
                    "status": "ok" if result.get("ok") else "error",
                    "detail": str(result.get("error", "live probe passed"))[:200]}
        except Exception as exc:  # noqa: BLE001
            return {"tool": tool.name, "status": "error", "detail": str(exc)[:200]}

    async def categories(self) -> list[str]:
        tools = await self.list()
        return sorted({t.category for t in tools})

    def aliases(self) -> dict[str, str]:
        return dict(ALIASES)