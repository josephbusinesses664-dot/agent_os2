# Agent Debrief — Team AI Shared Workspace runs (2026-09-07)

Compiled from every agent post, handoff, reflection and report in the Mattermost
workspace (1255 posts across all channels) after the day's pipeline runs.
This is the handoff document for the next fixing session.

## Status — fixed in the 2026-09-07 build-out session

- **B (tool aliases)**: fixed — aliases (`project_state`, `repo_tree`,
  `arch.tree`, `git_status`, …) resolve to the real tools, and unknown tools
  reply with the closest real name. Tests: `tests/test_debrief_fixes.py`.
- **G (cross-run state confusion)**: fixed — agent inboxes are scoped to the
  current task's project.
- **GO/NO-GO gate defaults GO when tooling fails**: fixed — the gate now
  sees the research stage that just finished, falls back through the router
  when the preferred model's provider is unconfigured, and FAILS CLOSED
  (NO-GO) when the model is unreachable or evidence is absent.
- **H (artifacts not written)**: fixed — planned stage artifacts are
  guaranteed on disk; stub files (<200 bytes) are replaced with the agent's
  real output.
- **I (duplicate/parallel stage executions)**: fixed — the worker skips
  queued tasks that are already running/queued/completed.
- **A (web.scrape on Reddit)**: fixed — reddit.com URLs are fetched through
  the old.reddit JSON endpoint (real titles + selftext).

Still environment-bound (operational, not code): live verification of the
DDG search fallbacks from the box, freeing a Render slot (25-service cap),
and a clean full run with API-only monitoring.

## 1. Problem clusters (ranked by how often agents hit them)

### A. Research tools: registered-but-broken (BLOCKER #1)
- `web.search` returned **empty errors** (`web_search failed: `) on live calls
  while `tool.health` reported the tool "ok" — a registration-vs-runtime mismatch.
  Root cause: the handler was a stub (no search API wired). Later replaced with
  a DuckDuckGo HTML scraper, which then hit **DDG rate limiting** from the box IP
  (now has a lite-endpoint fallback — still needs live verification).
- Agents reported `hn.search` and `reddit.search` did **not exist** in their
  environment (they were added mid-day; runs started before the deploy kept the
  old toolset).
- `web.scrape` on Reddit returned HTTP 200 with the literal text "Reddit" —
  old.reddit JSON works, but the HTML page is JS-rendered; scraper must use the
  JSON endpoints.
- Consequence (agents' own words): "Every evidence-gathering tool failed",
  "I cannot fabricate community evidence", Phase 1 never reached decision-grade.

### B. Model hallucinates tool names that don't exist
Agents repeatedly tried `project_state`, `repo_tree`, `arch.tree`,
`arch.explore`, `git.status` — all "unknown or disabled tool". The real names
are `project.state`, `repo.tree`. The tool-call schemas exist, but models fall
back to guessed names. Fix candidates: aliases (register `project_state` →
`project.state`, `repo_tree` → `repo.tree`), or a tool-lookup error message
that lists the closest real tool.

### C. filesystem.write failures ("missing path param" / "disabled")
- Was a real KeyError: handler read `args["path"]` and the model sometimes
  omitted it (fixed — now defensive with clear errors).
- Models also reported "filesystem_write tool is disabled" when the template
  said "do not use tools" — instruction misread. Template wording fixed, but
  the confusion pattern (instruction → belief → behavior) recurs.

### D. Stages handing off without doing the work
"stage X finished: ok" handoffs where the TASK_RESULT was just the opening
line ("I need to run tests... let me explore"). Root causes fixed during the
day: loop ended on a stub turn; duplicate tool-call nudge exhausted; content
accepted with <400 chars. Now: min-work guard (fails honestly), tool-capable
finish guard with tool_choice=none, MAX_TOOL_ROUNDS 12. Still needs a full
clean run to prove stability.

### E. Tool-round exhaustion
"exceeded 6 tool rounds" killed research repeatedly (now 12 + keep-content).
v4-flash models especially loop on tool calls; research/requirements/build
stages moved to deepseek-pro as a result.

### F. Deploy tooling missing at run time
Agents reported `deploy.github` absent (only the read-only github adapter)
and `render.manage` missing. Both are now registered + granted to
deployment-agent, but no run has exercised them end-to-end yet. Render
creation also hit the account limit: **"Hobby Tier is limited to 25
services"** — the Render workspace `tea-d8ovkj36sc1c73c819vg` is full.

### G. Cross-run state confusion
The final report cited "prior-stage handoffs (recorded in sibling project
prj_bcbf226584f7)" while the run was prj_bc32484b1a30 — agents mix artifacts
and handoffs across runs. Fix candidates: per-project handoff filtering in
the report prompt, or clearing/namespacing inbox messages per run.

### H. Artifacts not written
Multiple runs completed stages without writing the planned md artifacts
(only understanding.md present; community.md 41 bytes in one run). The
PLANNED_TOOL_CALLS filesystem.write with content "AUTO" depends on the model
actually calling the write — it often didn't.

### I. Duplicate/parallel stage executions
Multiple "running" tasks for the same stage (e.g. 2× frontend, 2× design)
observed. Contributing causes: diagnostic `agent-os` CLI invocations inside
the container spawn worker loops that drain the queue and resurrect queued
tasks; plus retry storms. Fix candidates: never run CLI commands in the app
container for inspection (use the HTTP API), and de-dupe queued tasks per
(project, stage).

## 2. What the agents say remains to finish

1. **Phase 1 VALIDATE re-run** with working research tools; real cited
   evidence (quotes, links, dates) for: market demand, the YC post and its
   response, competitors, who pays. Agents refuse to proceed without it.
2. **Alex's GO/NO-GO on decision-grade evidence** — the gate currently
   defaults to GO when tooling fails (one run: "GO — but research was blocked
   by tooling constraints"). The gate should distinguish tool failure from
   evidence.
3. **Phase 3 DEPLOY**: deploy.github push + render.manage create + LIVE URL.
   Blocked on: Render 25-service cap (free a slot or upgrade), plus an
   end-to-end run that reaches deploy with the new tools.
4. **Browser-verified visual/state checks** of the built pages (agents listed
   it as a follow-up for the deploy stage; no browser tooling run yet).
5. **Community evidence re-run** (was empty/failed in most runs).
6. **Validation experiments** (landing page test, community engagement,
   practitioner interviews) — from research-director's reflection.
7. **Pricing research** — "needed to address the cost concern counter-signal"
   (requirements-analyst).

## 3. What agents told the next agents (verbatim intent)

- The understanding doc is the anchor — "subsequent stages should reference
  it to stay aligned on goal, decision, and success criteria."
- "Reconcile incoming handoffs against this understanding before proceeding."
- "Do not report test results when there is nothing to test — writing a
  report of executed tests would misrepresent the state."
- "Escalate tool failures to the human operator with evidence so research can
  be re-run" — environmental blockers are not analytical ones.
- "Never touch Bellam and Kaaram or QueSnack" (guards are in the tool layer).
- Handoff notes point to artifact paths per project — next agents should read
  `/data/workspace/<project>/artifacts/` first.

## 4. What is genuinely working (do not regress)

- Executive GOAL/CHAT decision, checkpoints in the asking channel
- Director gate (only needed stages fire; "build a landing page" = 4 tasks)
- Named agent accounts + branch channels + full-output mirroring
- Executive research gate (Alex decides GO/NO-GO internally)
- Tool-calling with per-tool parameter schemas (sanitized names for DeepSeek)
- Honest failure reporting (agents refuse to fabricate)
- 16K-token build budget → 25.7KB index.html + 42.2KB demo.html shipped
- Home Terminal bridge (Prem ↔ Claude Code)
- GitHub Pages live: https://josephbusinesses664-dot.github.io/agentos-huddle/

## 5. Suggested next-run checklist

1. Verify `web.search` (DDG + lite fallback), `hn.search`, `reddit.search`
   with real calls from the box.
2. Add tool aliases (`project_state`, `repo_tree`, `arch.tree`, `git.status`).
3. Free a Render slot or decide GitHub Pages is the deployment target.
4. One clean full run with NO mid-run rebuilds, NO CLI commands in the
   container, and API-only monitoring.
5. Confirm artifacts/*.md get written every stage (or drop the AUTO convention).
