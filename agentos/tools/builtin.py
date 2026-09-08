"""Built-in tools available to agents.

Every tool has a name, description, permission key, risk level and a handler.
Handlers are async functions receiving (ctx, args) where ctx exposes the
runtime context: agent, project, task, workspace, stores, services.

New tools are pluggable: register a ToolDef + handler in the ToolRegistry.
"""

from __future__ import annotations

import ast
import json
import operator
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from agentos.domain.models import ToolDef

ToolHandler = Callable[[Any, dict], Awaitable[dict]]


def _safe_math(expr: str) -> str:
    """Evaluate a small safe subset of Python arithmetic."""
    tree = ast.parse(expr, mode="eval")
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
               ast.USub, ast.UAdd)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError(f"unsupported expression element: {type(node).__name__}")
    return str(eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {}))


def _path_inside(workspace: Path, rel: str) -> Path:
    candidate = (workspace / rel).resolve()
    if not str(candidate).startswith(str(workspace.resolve())):
        raise PermissionError("path escapes workspace")
    return candidate


# Client sites Prem protects: NEVER touch these repos or Render services
# without his explicit permission (Bellam & Kaaram, QueSnack).
PROTECTED_REPO_MARKERS = ("quesnack", "bellam")
PROTECTED_RENDER_SERVICE_IDS = ("srv-dabq05u7bikc73dtl0f0", "srv-dabq06e7bikc73dtl290")


def _protected_repo(repo: str) -> bool:
    return any(m in repo.lower() for m in PROTECTED_REPO_MARKERS)


def _protected_render_target(name: str, service_id: str = "") -> bool:
    if service_id and service_id in PROTECTED_RENDER_SERVICE_IDS:
        return True
    return any(m in (name or "").lower() for m in PROTECTED_REPO_MARKERS)


async def _h_deploy_github(ctx: Any, args: dict) -> dict:
    """Push the project workspace files to a GitHub repo via the Contents API.

    Needs GITHUB_TOKEN and DEPLOY_REPO env vars (repo = owner/name). A Render
    static site connected to that repo redeploys automatically on push."""
    import base64
    import os

    import httpx

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = str(args.get("repo") or os.environ.get("DEPLOY_REPO", "")).strip()
    if not token:
        return {"ok": False, "error": "deploy.github needs GITHUB_TOKEN configured"}
    if not repo:
        # create a repo per project: agentos-<project id>
        pid = (ctx.project.project_id if ctx.project else "agentos").replace("_", "-")
        repo = f"josephbusinesses664-dot/agentos-{pid}"
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                r = await client.post(
                    "https://api.github.com/user/repos",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/vnd.github+json"},
                    json={"name": repo.split("/")[-1], "private": False,
                          "auto_init": False})
                if r.status_code not in (200, 201, 422):
                    return {"ok": False, "error": f"repo creation failed: {r.status_code}"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"repo creation failed: {exc}"}
    if _protected_repo(repo):
        return {"ok": False, "error": "PROTECTED client repo — human permission required "
                                      "(Bellam & Kaaram / QueSnack)"}
    files = {}
    for path in ctx.workspace.rglob("*"):
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        if path.suffix.lower() in (".html", ".css", ".js", ".md", ".svg", ".png", ".jpg"):
            rel = path.relative_to(ctx.workspace).as_posix()
            files[rel] = base64.b64encode(path.read_bytes()).decode()
    if not files:
        return {"ok": False, "error": "no deployable files in the workspace"}
    results = []
    async with httpx.AsyncClient(timeout=60) as client:
        for rel, b64 in files.items():
            try:
                r = await client.put(
                    f"https://api.github.com/repos/{repo}/contents/{rel}",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/vnd.github+json"},
                    json={"message": f"agent-os deploy: {rel}",
                          "content": b64, "branch": args.get("branch", "main")})
                results.append({"path": rel, "status": r.status_code})
            except Exception as exc:  # noqa: BLE001
                results.append({"path": rel, "status": 0, "error": str(exc)[:120]})
    ok = all(r.get("status") in (200, 201) for r in results)
    return {"ok": ok, "repo": repo, "count": len(results), "deployed": results}


async def _h_render_manage(ctx: Any, args: dict) -> dict:
    """Manage Render static sites: list | create | delete.

    Create: spins up a static site from a GitHub repo (autoDeploy on push).
    Delete: removes the service. GUARD: Bellam & Kaaram and QueSnack services
    are protected — any touch returns an error without acting."""
    import os

    import httpx

    key = os.environ.get("RENDER_API_KEY", "")
    owner = os.environ.get("RENDER_OWNER", "")
    if not key:
        return {"ok": False, "error": "render.manage needs RENDER_API_KEY configured"}
    action = str(args.get("action") or "list")
    name = str(args.get("name") or "").strip()
    service_id = str(args.get("service_id") or "").strip()
    if _protected_render_target(name, service_id):
        return {"ok": False, "error": "PROTECTED client service — human permission "
                                      "required (Bellam & Kaaram / QueSnack)"}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        if action == "list":
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get("https://api.render.com/v1/services",
                                     headers=headers,
                                     params={"ownerId": owner, "limit": "20",
                                             **({"name": name} if name else {})})
                data = r.json()
                services = [{"id": s.get("service", {}).get("id", s.get("id")),
                             "name": s.get("service", {}).get("name", s.get("name")),
                             "type": s.get("service", {}).get("type", s.get("type"))}
                            for s in data]
                return {"ok": r.status_code < 300, "services": services,
                        "count": len(services)}
        if action == "create":
            repo = str(args.get("repo") or "").strip()
            if not name or not repo:
                return {"ok": False, "error": "render.manage create needs name + repo"}
            payload = {
                "type": "static_site",
                "ownerId": owner,
                "name": name,
                "repo": repo,
                "branch": args.get("branch", "main"),
                "autoDeploy": "yes",
                "serviceDetails": {"pullRequestPreviewsEnabled": "no",
                                   "publishDirectory": "."},
            }
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post("https://api.render.com/v1/services",
                                      headers=headers, json=payload)
                data = r.json()
                return {"ok": r.status_code < 300, "status": r.status_code,
                        "service": data if r.status_code < 300 else None,
                        "error": data.get("message", "") if r.status_code >= 300 else ""}
        if action == "delete":
            if not service_id:
                return {"ok": False, "error": "render.manage delete needs service_id"}
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.delete(
                    f"https://api.render.com/v1/services/{service_id}", headers=headers)
                return {"ok": r.status_code < 300, "status": r.status_code,
                        "deleted": service_id}
        return {"ok": False, "error": f"unknown action {action} (list|create|delete)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"render.manage failed: {exc}"}


async def _h_repo_tree(ctx: Any, args: dict) -> dict:
    """List the project workspace tree (dirs + files, sandbox-confined)."""
    project = ctx.project
    base = project.workspace_dir if project is not None else ctx.services.settings.workspace_dir
    path = str(args.get("path") or ".")
    if path.startswith("/"):
        path = path.lstrip("/")
    import os
    target = os.path.join(base, path) if path != "." else base
    if not os.path.abspath(target).startswith(os.path.abspath(base)):
        return {"ok": False, "error": "path escapes the workspace"}
    if not os.path.exists(target):
        return {"ok": True, "tree": f"(no such path: {path})"}
    lines = []
    for root, dirs, files in os.walk(target):
        dirs.sort()
        for d in dirs:
            if d in (".git", "__pycache__", ".venv"):
                continue
            rel = os.path.relpath(os.path.join(root, d), base)
            lines.append(f"{rel}/")
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(root, f), base)
            lines.append(rel)
        if len(lines) > 400:
            lines = lines[:400] + ["…(truncated)"]
            break
    return {"ok": True, "tree": "\n".join(lines), "count": len(lines)}


async def _h_filesystem_read(ctx: Any, args: dict) -> dict:
    rel = str(args.get("path") or "").strip()
    if not rel:
        return {"ok": False, "error": "filesystem.read requires a 'path' argument"}
    path = _path_inside(ctx.workspace, rel)
    if not path.exists():
        return {"ok": False, "error": f"{path} does not exist"}
    if path.is_dir():
        return {"ok": True, "entries": sorted(p.name for p in path.iterdir())}
    content = path.read_text(errors="replace")
    return {"ok": True, "content": content[:20_000], "truncated": len(content) > 20_000}


async def _h_filesystem_write(ctx: Any, args: dict) -> dict:
    rel = str(args.get("path") or "").strip()
    content = args.get("content")
    if not rel:
        return {"ok": False, "error": "filesystem.write requires a 'path' argument"}
    if content is None:
        return {"ok": False, "error": "filesystem.write requires 'content'"}
    path = _path_inside(ctx.workspace, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return {"ok": True, "path": str(path), "bytes": path.stat().st_size}


async def _h_shell(ctx: Any, args: dict) -> dict:
    import asyncio

    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return {"ok": False, "error": "shell requires a 'command' argument"}
    read_only = not re.search(r"(;|&&|\|\||>|rm |mv |mkdir|curl -X|git push|docker compose up)", cmd)
    if not read_only and not ctx.agent.allows("shell.write"):
        return {"ok": False, "error": "write shell commands denied by permission policy"}
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(ctx.workspace),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": "command timed out after 60s"}
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": out.decode(errors="replace")[-20_000:],
        "stderr": err.decode(errors="replace")[-5000:],
    }


async def _h_web_search(ctx: Any, args: dict) -> dict:
    """DuckDuckGo HTML search (keyless) — real web results."""
    import re
    import urllib.parse

    import httpx

    query = str(args.get("query") or args.get("q") or args.get("search") or "")
    if not query:
        return {"ok": False, "error": "web.search requires a 'query' argument"}
    limit = min(int(args.get("limit", 6)), 15)
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        html = resp.text
        results = []
        for m in re.finditer(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
                r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', html, re.S):
            results.append({
                "title": re.sub(r"<[^>]+>", "", m.group(2)).strip(),
                "url": m.group(1),
                "snippet": re.sub(r"<[^>]+>", "", m.group(3)).strip()[:400],
            })
            if len(results) >= limit:
                break
        if not results:
            # rate-limit fallback: the lite endpoint
            url2 = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
            async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"}) as client:
                resp2 = await client.get(url2)
                resp2.raise_for_status()
            for m2 in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>'
                                  r'.*?<td[^>]*class="result-snippet"[^>]*>(.*?)</td>',
                                  resp2.text, re.S):
                results.append({
                    "title": re.sub(r"<[^>]+>", "", m2.group(2)).strip(),
                    "url": m2.group(1),
                    "snippet": re.sub(r"<[^>]+>", "", m2.group(3)).strip()[:400],
                })
                if len(results) >= limit:
                    break
        if not results:
            return {"ok": False, "error": "no results (DDG may be rate-limiting; retry shortly)"}
        return {"ok": True, "query": query, "results": results, "count": len(results)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"web_search failed: {exc}"}


async def _h_calculator(ctx: Any, args: dict) -> dict:
    try:
        return {"ok": True, "result": _safe_math(args["expression"])}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def _h_memory_recall(ctx: Any, args: dict) -> dict:
    """Permission-scoped recall: agents may only read memories they are
    authorized for (agent/task/project/org rules); denials return empty
    results and are audit-logged. Existence is never disclosed."""
    scope = args.get("scope", "project")
    owner_id = args.get("owner_id") or (ctx.project.project_id if ctx.project else "org")
    scoped = getattr(ctx.services, "scoped_memory", None)
    if scoped is not None and ctx.agent is not None:
        entries = await scoped.scoped_recall(
            ctx.agent, scope, owner_id,
            query=args.get("query"), limit=args.get("limit", 10),
            task=ctx.task,
            project_id=ctx.project.project_id if ctx.project else "")
    else:  # no scoping context (system callers keep legacy behavior)
        entries = await ctx.services.memory.recall(scope, owner_id,
                                                   query=args.get("query"),
                                                   limit=args.get("limit", 10))
    return {"ok": True, "entries": [e.model_dump() for e in entries]}


async def _h_memory_save(ctx: Any, args: dict) -> dict:
    provenance = ""
    if ctx.agent is not None:
        provenance += f"agent:{ctx.agent.id}"
    if ctx.task is not None:
        provenance += f" task:{ctx.task.task_id}"
    scope = args.get("scope", "project")
    owner_id = args.get("owner_id") or (ctx.project.project_id if ctx.project else "org")
    scoped = getattr(ctx.services, "scoped_memory", None)
    entry = None
    if scoped is not None and ctx.agent is not None:
        entry = await scoped.scoped_save(
            ctx.agent, scope, owner_id, args["content"],
            task=ctx.task,
            project_id=ctx.project.project_id if ctx.project else "",
            kind=args.get("kind", "fact"),
            importance=args.get("importance", 3),
            source=args.get("source", "agent"),
            provenance=provenance.strip())
        if entry is None:
            return {"ok": False,
                    "error": f"not authorized to save {scope} memories for {owner_id}"}
    else:
        entry = await ctx.services.memory.save(
            scope, owner_id, args["content"],
            kind=args.get("kind", "fact"),
            importance=args.get("importance", 3),
            source=args.get("source", "agent"),
            provenance=provenance.strip())
    return {"ok": True, "memory_id": entry.memory_id}


async def _h_project_state(ctx: Any, args: dict) -> dict:
    project = await ctx.services.projects.get(ctx.project.project_id) if ctx.project else None
    tasks = await ctx.services.tasks.by_project(ctx.project.project_id) if ctx.project else []
    return {
        "ok": True,
        "project": project.model_dump() if project else None,
        "tasks": [
            {"task_id": t.task_id, "title": t.title, "status": t.status.value,
             "assigned_agent": t.assigned_agent}
            for t in tasks
        ],
    }


async def _h_mattermost_post(ctx: Any, args: dict) -> dict:
    if ctx.services.mattermost is None:
        return {"ok": False, "error": "mattermost not configured"}
    channel = args.get("channel") or ""
    if not channel and getattr(ctx, "agent", None):
        from agentos.agents.personas import branch_channel_for
        channel = branch_channel_for(ctx.agent.id)
    if not channel:
        channel = "agent-status"
    message = args.get("message", "")
    await ctx.services.mattermost.post_as_agent(ctx.agent, message, channel=channel)
    return {"ok": True, "channel": channel}


async def _h_api_call(ctx: Any, args: dict) -> dict:
    if ctx.services.api_catalog is None:
        return {"ok": False, "error": "api catalog not configured"}
    api = str(args.get("api") or "")
    if not api:
        return {"ok": False, "error": "api.call requires an 'api' name argument"}
    return await ctx.services.api_catalog.call_api(api, args.get("path", ""), args.get("params", {}))


async def _h_mcp_call(ctx: Any, args: dict) -> dict:
    if ctx.services.mcp is None:
        return {"ok": False, "error": "mcp registry not configured"}
    # client-site guard applies to EVERY pathway, MCP included: Bellam &
    # Kaaram and QueSnack are untouchable without Prem's explicit permission
    import json as _json
    blob = _json.dumps(args.get("args", {})).lower() + " " + str(args.get("tool", "")).lower()
    if any(m in blob for m in PROTECTED_REPO_MARKERS):
        return {"ok": False, "error": "PROTECTED client target — human permission "
                                      "required (Bellam & Kaaram / QueSnack)"}
    return await ctx.services.mcp.call_tool(
        args["server"], args["tool"], args.get("args", {}),
        agent_id=ctx.agent.id if ctx.agent is not None else "",
        task_id=ctx.task.task_id if ctx.task is not None else None,
        agent_permissions=dict(ctx.agent.permissions) if ctx.agent is not None else None)


async def _h_mcp_select(ctx: Any, args: dict) -> dict:
    """Progressive MCP tool discovery: rank governable candidate tools for a
    capability need, filtered by trust/permission/health for THIS agent."""
    if ctx.services.mcp is None:
        return {"ok": False, "error": "mcp registry not configured"}
    need = args.get("need", "") or args.get("query", "")
    if not need:
        return {"ok": False, "error": "need (capability description) is required"}
    tools = await ctx.services.mcp.select_tools(
        need, ctx.agent.id if ctx.agent is not None else "")
    return {"ok": True, "need": need, "tools": tools, "count": len(tools)}


async def _h_repo_search(ctx: Any, args: dict) -> dict:
    """Search file contents under the workspace (simple recursive scan)."""
    pattern = str(args.get("pattern") or args.get("query") or "").strip().lower()
    if not pattern:
        return {"ok": False, "error": "repo.search requires a 'pattern' argument "
                                      "(the substring to search for)"}
    max_results = min(int(args.get("max_results", 20)), 100)
    matches: list[dict] = []
    if not ctx.workspace.exists():
        return {"ok": True, "pattern": pattern, "matches": [], "count": 0}
    for path in ctx.workspace.rglob("*"):
        if path.is_dir() or not _is_text_file(path):
            continue
        try:
            text = path.read_text(errors="replace")[:100_000]
        except OSError:
            continue
        if pattern in text.lower():
            matches.append({"path": str(path.relative_to(ctx.workspace)),
                            "matches": text.lower().count(pattern)})
            if len(matches) >= max_results:
                break
    return {"ok": True, "pattern": pattern, "matches": matches,
            "count": len(matches)}


def _is_text_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" not in fh.read(4096)
    except OSError:
        return False


async def _h_repo_tree(ctx: Any, args: dict) -> dict:
    depth = min(int(args.get("depth", 3)), 6)
    root = ctx.workspace
    lines: list[str] = []

    def walk(directory: Path, level: int) -> None:
        if level > depth:
            return
        for child in sorted(directory.iterdir()):
            if child.name.startswith("."):
                continue
            lines.append("  " * level + ("📁 " if child.is_dir() else "📄 ") + child.name)
            if child.is_dir():
                walk(child, level + 1)

    walk(root, 0)
    return {"ok": True, "tree": lines[:300]}


async def _h_db_query(ctx: Any, args: dict) -> dict:
    """Run a read-only SQL query against a SQLite database in the workspace."""
    import sqlite3

    db_rel = str(args.get("db") or "").strip()
    if not db_rel:
        return {"ok": False, "error": "db.query requires a 'db' argument"}
    db_path = _path_inside(ctx.workspace, db_rel)
    if not db_path.exists():
        return {"ok": False, "error": f"database {db_path} does not exist"}
    query = args["query"].strip()
    if query.lower().lstrip().startswith(("insert", "update", "delete", "drop", "create", "alter")):
        return {"ok": False, "error": "only read-only queries allowed"}
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(query)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(min(int(args.get("limit", 50)), 200))
            return {"ok": True, "columns": columns, "rows": rows,
                    "row_count": len(rows)}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"query failed: {exc}"}


async def _h_file_patch(ctx: Any, args: dict) -> dict:
    """Apply a unified diff to a file inside the workspace (targets validated
    inside the workspace; hunks must match context exactly)."""
    rel = str(args.get("path") or "").strip()
    if not rel:
        return {"ok": False, "error": "file.patch requires a 'path' argument"}
    target = _path_inside(ctx.workspace, rel)
    if not target.exists():
        return {"ok": False, "error": f"{target} does not exist"}
    if target.is_dir():
        return {"ok": False, "error": f"{target} is a directory, not a file"}
    patch = str(args.get("patch") or "")
    if not patch:
        return {"ok": False, "error": "file.patch requires a 'patch' argument"}
    old_lines = target.read_text(errors="replace").splitlines(keepends=True)
    try:
        new_lines = _apply_unified_diff(old_lines, patch)
    except ValueError as exc:
        return {"ok": False, "error": f"patch failed: {exc}"}
    target.write_text("".join(new_lines))
    return {"ok": True, "path": str(target), "bytes": sum(len(l) for l in new_lines)}


def _apply_unified_diff(original: list[str], patch: str) -> list[str]:
    """Apply a unified diff positionally; raise ValueError on any mismatch.

    Walks the original file forward, consuming context/removed lines exactly
    and inserting added lines — a strict three-way apply that never silently
    corrupts a file.
    """
    new_lines: list[str] = []
    idx = 0
    for hunk in _parse_hunks(patch.splitlines(keepends=True)):
        start = hunk["start"]
        if start < idx or start > len(original):
            raise ValueError("hunk start out of range/order")
        new_lines.extend(original[idx:start])
        idx = start
        for op, content in hunk["ops"]:
            if op in (" ", "-"):
                if idx >= len(original):
                    raise ValueError("patch wants more lines than the file has")
                if original[idx].rstrip("\n") != content.rstrip("\n"):
                    raise ValueError(f"line mismatch at {idx + 1}: "
                                     f"expected {original[idx].rstrip()!r}, got {content.rstrip()!r}")
                if op == " ":
                    new_lines.append(original[idx])
                idx += 1
            else:  # "+"
                new_lines.append(content)
    new_lines.extend(original[idx:])
    return new_lines


def _parse_hunks(diff_lines: list[str]) -> list[dict]:
    """Parse `@@ -a,b +c,d @@` hunks into {start, ops: [(op, content), ...]}."""
    hunks: list[dict] = []
    current: Optional[dict] = None
    for line in diff_lines:
        if line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            start = int(m.group(1)) - 1 if m else 0
            if current:
                hunks.append(current)
            current = {"start": max(start, 0), "ops": []}
        elif current is not None and line[:1] in (" ", "-", "+"):
            current["ops"].append((line[:1], line[1:]))
    if current:
        hunks.append(current)
    return hunks


async def _h_json_query(ctx: Any, args: dict) -> dict:
    """Query a JSON file in the workspace with a dotted path."""
    rel = str(args.get("path") or "").strip()
    if not rel:
        return {"ok": False, "error": "json.query requires a 'path' argument"}
    target = _path_inside(ctx.workspace, rel)
    if not target.exists():
        return {"ok": False, "error": f"{target} does not exist"}
    if target.is_dir():
        return {"ok": False, "error": f"{target} is a directory, not a file"}
    try:
        data = json.loads(target.read_text(errors="replace"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid JSON: {exc}"}
    path = [p for p in args.get("path_expr", "").split(".") if p]
    node: Any = data
    for part in path:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return {"ok": False, "error": f"path segment {part} not found"}
    return {"ok": True, "result": node}


async def _h_hn_search(ctx: Any, args: dict) -> dict:
    """Hacker News search via the Algolia HN API (keyless)."""
    import urllib.parse

    import httpx

    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "hn.search requires a 'query' argument"}
    limit = min(int(args.get("limit", 10)), 30)
    url = ("https://hn.algolia.com/api/v1/search?query=" + urllib.parse.quote(query)
           + f"&hitsPerPage={limit}")
    try:
        async with httpx.AsyncClient(timeout=25,
                                     headers={"User-Agent": "agent-os/0.1"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        results = []
        for h in data.get("hits", []):
            title = h.get("title") or (h.get("story_title") or "")
            if not title:
                continue
            results.append({
                "title": title,
                "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                "author": h.get("author", ""),
                "points": h.get("points", 0),
                "comments": h.get("num_comments", 0),
                "created": h.get("created_at", ""),
            })
        return {"ok": True, "query": query, "results": results, "count": len(results)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"hn_search failed: {exc}"}


async def _h_reddit_search(ctx: Any, args: dict) -> dict:
    """Reddit search via the old.reddit JSON endpoint (keyless; browser UA)."""
    import time
    import urllib.parse

    import httpx

    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "reddit.search requires a 'query' argument"}
    limit = min(int(args.get("limit", 10)), 30)
    sub = str(args.get("subreddit") or "").strip()
    base = f"https://old.reddit.com/r/{sub}/search.json" if sub else "https://old.reddit.com/search.json"
    url = base + "?q=" + urllib.parse.quote(query) + f"&limit={limit}&sort=relevance"
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        results = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            results.append({
                "title": d.get("title", ""),
                "text": (d.get("selftext") or "")[:600],
                "url": "https://reddit.com" + d.get("permalink", ""),
                "subreddit": d.get("subreddit", ""),
                "score": d.get("score", 0),
                "comments": d.get("num_comments", 0),
                "created_utc": time.strftime("%Y-%m-%d", time.gmtime(d.get("created_utc", 0))),
            })
        return {"ok": True, "query": query, "results": results, "count": len(results)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"reddit_search failed: {exc}"}


async def _h_web_scrape(ctx: Any, args: dict) -> dict:
    """Fetch a URL and return its visible text (network tool, permission-gated).

    Reddit pages are JS-rendered (a plain GET returns the literal word
    "Reddit"), so reddit.com URLs are fetched through the old.reddit JSON
    endpoint instead — real post titles and selftext.
    """
    import httpx

    url = str(args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "web.scrape requires a 'url' argument"}
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "url must be http(s)"}
    try:
        if "reddit.com" in url:
            json_url = url.replace("www.reddit.com", "old.reddit.com") \
                .replace("reddit.com", "old.reddit.com") \
                .rstrip("/") + ".json"
            async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                                 "Chrome/125.0 Safari/537.36"}) as client:
                resp = await client.get(json_url)
                resp.raise_for_status()
                data = resp.json()
            children = data.get("data", {}).get("children", [])
            parts = []
            for child in children[:20]:
                d = child.get("data", {})
                title = d.get("title")
                body = (d.get("selftext") or "").strip()
                if title:
                    parts.append(title)
                if body:
                    parts.append(body)
            text = "\n\n".join(parts)
            if not text:
                return {"ok": False, "error": "reddit JSON returned no content"}
            return {"ok": True, "url": url, "status": resp.status_code,
                    "text": text[:12_000], "truncated": len(text) > 12_000}
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers={"User-Agent": "agent-os/0.1"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        text = re.sub(r"<script.*?</script>|<style.*?</style>", "", resp.text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return {"ok": True, "url": url, "status": resp.status_code,
                "text": text[:12_000], "truncated": len(text) > 12_000}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"scrape failed: {exc}"}


async def _h_git(ctx: Any, args: dict) -> dict:
    """Read-only git introspection inside the workspace."""
    import asyncio

    cmd = args.get("command", "status")
    allowed = ("status", "log", "diff", "branch", "remote", "show")
    if cmd not in allowed:
        return {"ok": False, "error": f"git.{cmd} not allowed (read-only: {', '.join(allowed)})"}
    argv = ["git", cmd]
    if cmd in ("log", "diff"):
        argv.append("--no-pager")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(ctx.workspace),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": "git command timed out"}
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "stdout": out.decode(errors="replace")[-12_000:],
            "stderr": err.decode(errors="replace")[-3000:]}


async def _h_tool_discover(ctx: Any, args: dict) -> dict:
    """Discover tools relevant to a capability need (keeps tool lists small)."""
    tools = await ctx.services.tools.discover(
        args.get("query", ""), agent=ctx.agent, limit=args.get("limit", 10))
    return {"ok": True, "tools": [
        {"name": t.name, "description": t.description, "risk": t.risk_level}
        for t in tools
    ]}


async def _h_tool_health(ctx: Any, args: dict) -> dict:
    report = await ctx.services.tools.health(args.get("tool", ""))
    # fold in circuit-breaker state so operators can see which tools are
    # failing fast after repeated failures
    circuits = getattr(ctx.services, "executor", None)
    if circuits is not None and hasattr(circuits, "_circuit_status"):
        report["circuits"] = circuits._circuit_status()
    return {"ok": True, "report": report}


async def _h_browser_open(ctx: Any, args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "browser.open requires a 'url' argument"}
    return await _browser_session(ctx).open(url)


async def _h_browser_snapshot(ctx: Any, args: dict) -> dict:
    return await _browser_session(ctx).snapshot()


async def _h_browser_click(ctx: Any, args: dict) -> dict:
    selector = str(args.get("selector") or "").strip()
    if not selector:
        return {"ok": False, "error": "browser.click requires a 'selector' argument"}
    return await _browser_session(ctx).click(selector)


async def _h_browser_type(ctx: Any, args: dict) -> dict:
    selector = str(args.get("selector") or "").strip()
    text = str(args.get("text") or "").strip()
    if not selector:
        return {"ok": False, "error": "browser.type requires a 'selector' argument"}
    if not text:
        return {"ok": False, "error": "browser.type requires a 'text' argument"}
    return await _browser_session(ctx).type_text(selector, text)


async def _h_browser_evaluate(ctx: Any, args: dict) -> dict:
    expression = str(args.get("expression") or "").strip()
    if not expression:
        return {"ok": False, "error": "browser.evaluate requires an 'expression' argument"}
    return await _browser_session(ctx).evaluate(expression)


async def _h_browser_screenshot(ctx: Any, args: dict) -> dict:
    path = str(args.get("path") or "").strip() or "screenshot.png"
    return await _browser_session(ctx).screenshot(path)


async def _h_browser_close(ctx: Any, args: dict) -> dict:
    if getattr(ctx, "browser", None) is None:
        return {"ok": True, "closed": True}
    return await ctx.browser.close()


def _browser_session(ctx: Any):
    """Lazily create the per-run browser session (isolated per agent run)."""
    if getattr(ctx, "browser", None) is None:
        from agentos.integrations.browser import BrowserSession

        ctx.browser = BrowserSession(ctx.workspace,
                                     timeout_ms=int(
                                         getattr(ctx.services.settings, "browser_timeout_ms", 15000)))
    return ctx.browser


async def _h_github(ctx: Any, args: dict) -> dict:
    """Read-only GitHub adapter: search_repos | get_repo | list_issues."""
    token = getattr(ctx.services.settings, "github_token", None)
    if not token:
        return {"ok": False, "error": "github adapter not configured (set GITHUB_TOKEN)"}
    import httpx

    action = args.get("action", "search_repos")
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        async with httpx.AsyncClient(timeout=25, headers=headers) as client:
            if action == "search_repos":
                resp = await client.get("https://api.github.com/search/repositories",
                                        params={"q": args.get("query", ""), "per_page": args.get("limit", 10)})
                resp.raise_for_status()
                body = resp.json()
                return {"ok": True, "action": action,
                        "repos": [{"name": r["full_name"], "stars": r.get("stargazers_count", 0),
                                   "description": (r.get("description") or "")[:120]}
                                  for r in body.get("items", [])]}
            if action == "get_repo":
                resp = await client.get(f"https://api.github.com/repos/{args['repo']}")
                resp.raise_for_status()
                r = resp.json()
                return {"ok": True, "action": action,
                        "repo": {"name": r["full_name"], "stars": r.get("stargazers_count", 0),
                                 "language": r.get("language"), "description": (r.get("description") or "")[:200]}}
            if action == "list_issues":
                resp = await client.get(f"https://api.github.com/repos/{args['repo']}/issues",
                                        params={"state": args.get("state", "open"), "per_page": args.get("limit", 10)})
                resp.raise_for_status()
                issues = resp.json()
                return {"ok": True, "action": action,
                        "issues": [{"number": i["number"], "title": i["title"],
                                    "state": i["state"]} for i in issues]}
            return {"ok": False, "error": f"unknown github action {action}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"github adapter failed: {exc}"}


async def _h_postgres_query(ctx: Any, args: dict) -> dict:
    """Read-only PostgreSQL query adapter (production DBs — approval-gated)."""
    # read-only gate first (security boundary), config check second
    query = args.get("query", "").strip()
    lowered = query.lower().lstrip()
    if not lowered.startswith("select"):
        return {"ok": False, "error": "postgres adapter is read-only: only SELECT allowed"}
    url = getattr(ctx.services.settings, "postgres_query_url", None)
    if not url:
        return {"ok": False, "error": "postgres adapter not configured (set POSTGRES_QUERY_URL)"}
    import asyncpg

    try:
        conn = await asyncpg.connect(url, timeout=10)
        try:
            rows = await conn.fetch(query)
            columns = list(rows[0].keys()) if rows else []
            data = [dict(r) for r in rows[: min(int(args.get("limit", 50)), 200)]]
            return {"ok": True, "columns": columns, "rows": data,
                    "row_count": len(data)}
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"postgres adapter failed: {exc}"}


async def _h_docker(ctx: Any, args: dict) -> dict:
    """Read-only Docker adapter: ps | inspect | logs (never mutates)."""
    import asyncio

    action = args.get("action", "ps")
    if action not in ("ps", "inspect", "logs"):
        return {"ok": False, "error": f"docker.{action} not allowed (read-only: ps, inspect, logs)"}
    cmd = ["docker", action]
    if action == "ps":
        cmd += ["--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"]
    if action == "inspect":
        cmd.append(args.get("container", ""))
    if action == "logs":
        cmd += ["--tail", str(args.get("lines", 50)), args.get("container", "")]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": "docker command timed out"}
    if proc.returncode != 0:
        return {"ok": False, "error": "docker not available: "
                                       + err.decode(errors="replace")[:300]}
    text = out.decode(errors="replace")
    if action == "ps":
        containers = []
        for line in text.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                containers.append({"id": parts[0][:12], "name": parts[1],
                                   "image": parts[2], "status": parts[3]})
        return {"ok": True, "containers": containers, "count": len(containers)}
    return {"ok": True, "action": action, "output": text[-8000:]}


async def _h_agent_delegate(ctx: Any, args: dict) -> dict:
    """Delegate a subtask to a child agent. Bounded by engine spawn limits
    (depth, parallelism, budget, duplicate detection).

    Two modes:
    - explicit: `agent` names the child (hierarchy/permission gated)
    - purposeful: `agent: "auto"` asks the DelegationEngine to select the
      best suited AND authorized candidate (explainable scoring)
    """
    child = args.get("agent", "")
    description = args.get("description", "")
    if not child or not description:
        return {"ok": False, "error": "agent and description are required"}
    if ctx.task is None:
        return {"ok": False, "error": "delegation requires a parent task"}
    if ctx.agent is None:
        return {"ok": False, "error": "delegation requires an agent context"}

    selection_note = ""
    if child == "auto":
        # purposeful selection: bounded by hierarchy + permissions; the
        # engine only widens the pool when the caller holds agent.delegate
        if ctx.agent.permissions.get("agent.delegate", "deny") != "allow" \
                and not ctx.agent.allowed_children:
            return {"ok": False, "error": f"{ctx.agent.id} may not delegate"}
        from agentos.orchestration.delegation import DelegationEngine
        engine = DelegationEngine(ctx.agent_registry,
                                  performance=getattr(ctx.services, "performance", None),
                                  instances=ctx.agent_registry)
        decision = await engine.select(ctx.agent.id, description)
        if not decision.selected:
            return {"ok": False, "error": decision.reason,
                    "candidates": [c.agent_id for c in decision.candidates]}
        child = decision.selected
        selection_note = decision.explanation()
    else:
        # only the child's parent (or an explicitly permitted agent) may
        # delegate. The permission must be EXPLICIT — the default-allow
        # policy does not apply to delegation.
        child_def = await ctx.agent_registry.get(child)
        if child_def is None:
            return {"ok": False, "error": f"unknown agent {child}"}
        permitted = (child_def.parent_agent == ctx.agent.id
                     or ctx.agent.permissions.get("agent.delegate", "deny") == "allow")
        if not permitted:
            return {"ok": False, "error": f"{ctx.agent.id} may not delegate to {child}"}

    try:
        child_result = await ctx.services.engine.spawn_subagent(
            ctx.task, child, description, depth=ctx.spawn_depth + 1)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"delegation failed: {exc}"}
    return {"ok": True, "child": child, "result": child_result.content[:4000],
            "error": child_result.error, "artifacts": child_result.artifacts,
            "cost": child_result.cost,
            **({"selection": selection_note} if selection_note else {})}


async def _h_agent_challenge(ctx: Any, args: dict) -> dict:
    """Raise a structured disagreement with another agent (identity-driven:
    claim + evidence + severity + recommended action). Bounded: the
    recipient or its parent resolves; no infinite debate."""
    recipient = args.get("agent", "")
    concern = args.get("concern", "")
    if not recipient or not concern:
        return {"ok": False, "error": "agent and concern are required"}
    severity = args.get("severity", "medium")
    if severity not in ("low", "medium", "high", "blocking"):
        return {"ok": False, "error": "severity must be low|medium|high|blocking"}
    recipient_def = await ctx.agent_registry.get(recipient)
    if recipient_def is None:
        return {"ok": False, "error": f"unknown agent {recipient}"}
    msg = await ctx.services.messages.send_challenge(
        ctx.agent.id, recipient, concern,
        evidence=[str(e) for e in args.get("evidence", [])],
        severity=severity,
        recommended_action=str(args.get("recommended_action", "")),
        claim=str(args.get("claim", "")),
        scope=str(args.get("scope", "")),
        task_id=ctx.task.task_id if ctx.task else None,
        project_id=ctx.project.project_id if ctx.project else None)
    await ctx.event_bus.publish("agent.challenge",
                                {"from": ctx.agent.id, "to": recipient,
                                 "severity": severity, "concern": concern[:200],
                                 "message_id": msg.message_id},
                                agent_id=ctx.agent.id,
                                task_id=ctx.task.task_id if ctx.task else None,
                                project_id=ctx.project.project_id if ctx.project else None,
                                severity="warning" if severity in ("high", "blocking") else "info")
    return {"ok": True, "challenge_id": msg.message_id, "recipient": recipient,
            "note": "the recipient (or its parent) resolves; expect a DECISION message"}


async def _h_agent_resolve(ctx: Any, args: dict) -> dict:
    """Resolve a challenge directed at this agent: accept | reject | escalate.
    Rejection requires a rationale; escalating passes the decision upward."""
    challenge_id = args.get("challenge_id", "")
    verdict = args.get("verdict", "")
    rationale = str(args.get("rationale", ""))
    if not challenge_id or verdict not in ("accept", "reject", "escalate"):
        return {"ok": False, "error": "challenge_id and verdict (accept|reject|escalate) are required"}
    if verdict == "reject" and not rationale:
        return {"ok": False, "error": "rejecting a challenge requires a rationale"}
    try:
        decision = await ctx.services.messages.resolve_challenge(
            challenge_id, ctx.agent.id, verdict, rationale=rationale)
    except (KeyError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    await ctx.event_bus.publish("agent.challenge_resolved",
                                {"challenge_id": challenge_id, "verdict": verdict,
                                 "resolved_by": ctx.agent.id, "rationale": rationale[:200]},
                                agent_id=ctx.agent.id,
                                task_id=ctx.task.task_id if ctx.task else None,
                                project_id=ctx.project.project_id if ctx.project else None)
    return {"ok": True, "verdict": verdict, "decision_message": decision.message_id}


BUILTIN_TOOLS: list[ToolDef] = [
    ToolDef(name="filesystem.read", description="Read a file or list a directory inside the project workspace.",
            permission_key="filesystem.read", risk_level="low",
            config={"parameters": {"path": {"type": "string", "description": "File path or directory inside the project workspace"}}, "required": ["path"]}),
    ToolDef(name="repo.tree", description="List the project workspace file tree (dirs + files).",
            permission_key="filesystem.read", risk_level="low",
            config={"parameters": {"path": {"type": "string", "description": "Directory to list, default workspace root"}}}),
    ToolDef(name="deploy.github", description="Push the workspace files (html/css/js/md) to a GitHub repo; a Render static site on that repo auto-deploys. Needs GITHUB_TOKEN + DEPLOY_REPO configured.",
            permission_key="deploy", risk_level="high",
            config={"parameters": {"repo": {"type": "string", "description": "owner/name repo to push to (defaults to DEPLOY_REPO env)"}}}),
    ToolDef(name="render.manage", description="Manage Render static sites: list, create (from a GitHub repo, autoDeploy), delete. GUARDED: Bellam & Kaaram / QueSnack services are untouchable. Needs RENDER_API_KEY + RENDER_OWNER.",
            permission_key="deploy", risk_level="high",
            config={"parameters": {"action": {"type": "string", "description": "list | create | delete"}, "name": {"type": "string", "description": "service name"}, "repo": {"type": "string", "description": "github repo URL for create"}, "service_id": {"type": "string", "description": "render service id for delete"}}, "required": ["action"]}),
    ToolDef(name="filesystem.write", description="Write a file inside the project workspace.",
            permission_key="filesystem.write", risk_level="medium",
            config={"parameters": {"path": {"type": "string", "description": "File path inside the project workspace"}, "content": {"type": "string", "description": "Complete file content"}}, "required": ["path", "content"]}),
    ToolDef(name="shell", description="Run a shell command inside the workspace (write commands need permission).",
            permission_key="shell", risk_level="high",
            config={"parameters": {"command": {"type": "string", "description": "Shell command"}}, "required": ["command"]}),
    ToolDef(name="web.search", description="Search the web via DuckDuckGo (keyless). Returns ranked results with titles/URLs/snippets.",
            permission_key="web.search", risk_level="low",
            config={"parameters": {"query": {"type": "string", "description": "Search query"}, "limit": {"type": "integer", "description": "max results (default 6)"}}, "required": ["query"]}),
    ToolDef(name="hn.search", description="Search Hacker News via the Algolia HN API (keyless): stories, comments, points.",
            permission_key="web.search", risk_level="low",
            config={"parameters": {"query": {"type": "string", "description": "Search query"}, "limit": {"type": "integer", "description": "max hits (default 10)"}}, "required": ["query"]}),
    ToolDef(name="reddit.search", description="Search Reddit (keyless, old.reddit JSON): posts, comments, scores — real community signal.",
            permission_key="web.search", risk_level="low",
            config={"parameters": {"query": {"type": "string", "description": "Search query"}, "subreddit": {"type": "string", "description": "optional subreddit to scope"}, "limit": {"type": "integer", "description": "max posts (default 10)"}}, "required": ["query"]}),
    ToolDef(name="calculator", description="Evaluate a safe arithmetic expression.",
            permission_key="calculator", risk_level="low",
            config={"parameters": {"expression": {"type": "string", "description": "Math expression"}}, "required": ["expression"]}),
    ToolDef(name="memory.recall", description="Recall persisted memory entries (agent/project/org/task scope).",
            permission_key="memory.recall", risk_level="low",
            config={"parameters": {"query": {"type": "string", "description": "What to recall"}}, "required": ["query"]}),
    ToolDef(name="memory.save", description="Persist a memory entry (fact, decision, lesson, preference).",
            permission_key="memory.save", risk_level="low",
            config={"parameters": {"content": {"type": "string", "description": "Fact to remember"}}, "required": ["content"]}),
    ToolDef(name="project.state", description="Inspect the current project and its task list.",
            permission_key="project.state", risk_level="low",
            config={"parameters": {}}),
    ToolDef(name="mattermost.post", description="Post a message to a Mattermost channel under this agent's identity.",
            permission_key="mattermost.post", risk_level="low",
            config={"parameters": {"channel": {"type": "string", "description": "Channel name (defaults to the agent's branch channel)"}, "message": {"type": "string", "description": "Message text"}}, "required": ["message"]}),
    ToolDef(name="api.call", description="Call a registered API from the API catalog (rate-limited, logged).",
            permission_key="api.call", risk_level="medium"),
    ToolDef(name="mcp.call", description="Call a tool on a governed MCP server (trust/permission/health gated, injection-scanned).",
            permission_key="mcp.call", risk_level="medium",
            config={"parameters": {"server": {"type": "string", "description": "MCP server name"}, "tool": {"type": "string", "description": "Tool name on that server"}, "args": {"type": "object", "description": "Tool arguments"}}, "required": ["server", "tool"]}),
    ToolDef(name="mcp.select", description="Rank available MCP tools for a capability need (trust-, permission- and health-aware).",
            permission_key="mcp.call", risk_level="low",
            config={"parameters": {"need": {"type": "string", "description": "Capability need to satisfy"}}, "required": ["need"]}),
    ToolDef(name="repo.search", description="Search file contents under the project workspace.",
            permission_key="repo.search", risk_level="low",
            config={"parameters": {"pattern": {"type": "string", "description": "Substring to search for in workspace files"}}, "required": ["pattern"]}, category="capability"),
    ToolDef(name="repo.tree", description="List the project workspace file tree.",
            permission_key="repo.tree", risk_level="low", category="capability"),
    ToolDef(name="db.query", description="Run a read-only SQL query against a SQLite DB in the workspace.",
            permission_key="db.query", risk_level="medium",
            config={"parameters": {"db": {"type": "string", "description": "Relative path to the SQLite file"}, "query": {"type": "string", "description": "Read-only SQL"}}, "required": ["db", "query"]}, category="capability"),
    ToolDef(name="file.patch", description="Apply a unified diff to a file inside the workspace (must apply cleanly).",
            permission_key="file.patch", risk_level="medium",
            config={"parameters": {"path": {"type": "string", "description": "File to patch"}, "patch": {"type": "string", "description": "Unified diff text"}}, "required": ["path", "patch"]}, category="capability"),
    ToolDef(name="json.query", description="Query a JSON file in the workspace with a dotted path.",
            permission_key="json.query", risk_level="low",
            config={"parameters": {"path": {"type": "string", "description": "JSON file path"}, "path_expr": {"type": "string", "description": "Dotted path expression (defaults to the whole document)"}}, "required": ["path"]}, category="capability"),
    ToolDef(name="web.scrape", description="Fetch a URL and extract visible text (network).",
            permission_key="web.scrape", risk_level="medium",
            config={"parameters": {"url": {"type": "string", "description": "http(s) URL to fetch"}}, "required": ["url"]}, category="capability"),
    ToolDef(name="git.status", description="Read-only git introspection (status/log/diff/branch).",
            permission_key="git.status", risk_level="low",
            config={"parameters": {"command": {"type": "string", "description": "status | log | diff | branch | remote | show"}}}, category="capability"),
    ToolDef(name="tool.discover", description="Discover which tools suit a capability need.",
            permission_key="tool.discover", risk_level="low", category="system"),
    ToolDef(name="tool.health", description="Check a tool's health/availability.",
            permission_key="tool.health", risk_level="low", category="system"),
    ToolDef(name="agent.delegate", description="Delegate a bounded subtask to a child agent (depth/parallel/budget limited). Use agent='auto' for purposeful selection among authorized, best-suited agents.",
            permission_key="agent.delegate", risk_level="medium",
            config={"parameters": {"agent": {"type": "string", "description": "Child agent id or 'auto'"}, "description": {"type": "string", "description": "Subtask description"}}, "required": ["agent", "description"]}, category="system"),
    ToolDef(name="agent.challenge", description="Raise a structured disagreement with another agent: claim + evidence + severity + recommended action.",
            permission_key="mattermost.post", risk_level="low",
            config={"parameters": {"agent": {"type": "string", "description": "Recipient agent id"}, "concern": {"type": "string", "description": "The disagreement"}, "severity": {"type": "string", "description": "low | medium | high | blocking"}, "evidence": {"type": "array", "description": "Supporting evidence"}}, "required": ["agent", "concern"]}, category="system"),
    ToolDef(name="agent.resolve", description="Resolve a challenge you received: accept | reject (rationale required) | escalate.",
            permission_key="mattermost.post", risk_level="low",
            config={"parameters": {"challenge_id": {"type": "string", "description": "Challenge to resolve"}, "verdict": {"type": "string", "description": "accept | reject | escalate"}, "rationale": {"type": "string", "description": "Required when rejecting"}}, "required": ["challenge_id", "verdict"]}, category="system"),
    ToolDef(name="browser.open", description="Open a URL in the agent's isolated browser session (http/https/file/data).",
            permission_key="browser.open", risk_level="low",
            config={"parameters": {"url": {"type": "string", "description": "URL to open"}}, "required": ["url"]}, category="browser"),
    ToolDef(name="browser.snapshot", description="Read the current page: headings, links, buttons, inputs and visible text.",
            permission_key="browser.snapshot", risk_level="low", category="browser"),
    ToolDef(name="browser.click", description="Click the first element matching a CSS selector on the current page.",
            permission_key="browser.click", risk_level="low",
            config={"parameters": {"selector": {"type": "string", "description": "CSS selector"}}, "required": ["selector"]}, category="browser"),
    ToolDef(name="browser.type", description="Type text into the first input matching a CSS selector.",
            permission_key="browser.type", risk_level="low",
            config={"parameters": {"selector": {"type": "string", "description": "CSS selector"}, "text": {"type": "string", "description": "Text to type"}}, "required": ["selector", "text"]}, category="browser"),
    ToolDef(name="browser.screenshot", description="Save a full-page screenshot into the workspace and return its path.",
            permission_key="browser.screenshot", risk_level="low", category="browser"),
    ToolDef(name="browser.evaluate", description="Run a JavaScript expression in the page (high risk — approval gated).",
            permission_key="browser.evaluate", risk_level="high",
            config={"parameters": {"expression": {"type": "string", "description": "JS expression"}}, "required": ["expression"]}, category="browser"),
    ToolDef(name="browser.close", description="Close the agent's browser session (releases the process).",
            permission_key="browser.close", risk_level="low", category="browser"),
    ToolDef(name="github", description="Read-only GitHub adapter: search_repos | get_repo | list_issues.",
            permission_key="github", risk_level="medium", category="adapter"),
    ToolDef(name="postgres.query", description="Read-only SQL SELECT against the configured POSTGRES_QUERY_URL (approval gated).",
            permission_key="postgres.query", risk_level="high",
            config={"parameters": {"query": {"type": "string", "description": "Read-only SELECT"}}, "required": ["query"]}, category="adapter"),
    ToolDef(name="docker", description="Read-only Docker adapter: ps | inspect | logs (never mutates).",
            permission_key="docker", risk_level="medium", category="adapter"),
]

HANDLERS: dict[str, ToolHandler] = {
    "filesystem.read": _h_filesystem_read,
    "repo.tree": _h_repo_tree,
    "deploy.github": _h_deploy_github,
    "render.manage": _h_render_manage,
    "filesystem.write": _h_filesystem_write,
    "shell": _h_shell,
    "web.search": _h_web_search,
    "hn.search": _h_hn_search,
    "reddit.search": _h_reddit_search,
    "calculator": _h_calculator,
    "memory.recall": _h_memory_recall,
    "memory.save": _h_memory_save,
    "project.state": _h_project_state,
    "mattermost.post": _h_mattermost_post,
    "api.call": _h_api_call,
    "mcp.call": _h_mcp_call,
    "mcp.select": _h_mcp_select,
    "repo.search": _h_repo_search,
    "repo.tree": _h_repo_tree,
    "db.query": _h_db_query,
    "file.patch": _h_file_patch,
    "json.query": _h_json_query,
    "web.scrape": _h_web_scrape,
    "git.status": _h_git,
    "tool.discover": _h_tool_discover,
    "tool.health": _h_tool_health,
    "agent.delegate": _h_agent_delegate,
    "agent.challenge": _h_agent_challenge,
    "agent.resolve": _h_agent_resolve,
    "browser.open": _h_browser_open,
    "browser.snapshot": _h_browser_snapshot,
    "browser.click": _h_browser_click,
    "browser.type": _h_browser_type,
    "browser.evaluate": _h_browser_evaluate,
    "browser.screenshot": _h_browser_screenshot,
    "browser.close": _h_browser_close,
    "github": _h_github,
    "postgres.query": _h_postgres_query,
    "docker": _h_docker,
}


def load_builtin_defs() -> dict[str, ToolDef]:
    return {t.name: t for t in BUILTIN_TOOLS}