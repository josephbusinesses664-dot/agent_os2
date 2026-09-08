"""The built-in agent organization chart.

Agents are *definitions*, not processes: they are instantiated on demand by
the orchestrator. Every agent carries its own permission set (least
privilege), skill list, model policy and budget policy.

Permission keys: filesystem.read, filesystem.write, shell, web.search,
web.scrape, api.call, mattermost.post, memory.recall, memory.save,
project.state, mcp.call, deploy, github, exec (any tool).
"""

from __future__ import annotations

from agentos.agents.identities import identity_for
from agentos.domain.models import AgentDef


_EXEC_IDS = {
    "executive", "chief-of-staff",
    "product-director", "cto", "design-director", "research-director",
    "marketing-director", "sales-director", "qa-director", "operations-director",
}


def _agent(
    agent_id: str,
    name: str,
    role: str,
    description: str,
    parent: str | None,
    children: list[str],
    skills: list[str],
    tools: list[str],
    tier: str,
    risk: str = "low",
    extra_permissions: dict[str, str] | None = None,
    mcp_servers: list[str] | None = None,
) -> AgentDef:
    perms: dict[str, str] = {
        "filesystem.read": "allow",
        # Writes are confined to the per-project sandboxed workspace (the
        # handler enforces the boundary) and are audit-logged — safe default.
        "filesystem.write": "allow",
        "shell": "deny",
        "web.search": "allow",
        "web.scrape": "deny",
        "api.call": "deny",
        "mattermost.post": "allow",
        "memory.recall": "allow",
        "memory.save": "allow",
        "project.state": "allow",
        "mcp.call": "deny",
        "deploy": "deny",
        "github": "deny",
        "postgres.query": "deny",
        "docker": "deny",
        "agent.delegate": "deny",
        "browser.open": "deny",
        "browser.snapshot": "deny",
        "browser.click": "deny",
        "browser.type": "deny",
        "browser.screenshot": "deny",
        "browser.evaluate": "deny",
        "browser.close": "deny",
    }
    perms.update(extra_permissions or {})
    # execs (executive office + department directors) think with deepseek-pro;
    # every specialist below them works on deepseek-flash
    # deliverable-heavy + evidence-critical stages need pro's output depth;
    # flash caps at ~1-2K words and kept failing the substantive-output gate
    _PRO_IDS = {"frontend-lead", "backend-lead", "research-director",
                "market-researcher", "community-researcher", "competitor-analyst",
                "technical-researcher", "product-researcher", "requirements-analyst"}
    preferred = ("deepseek-pro" if agent_id in _EXEC_IDS or agent_id in _PRO_IDS
                 else "deepseek-flash")
    return AgentDef(
        id=agent_id,
        name=name,
        role=role,
        description=description,
        parent_agent=parent,
        allowed_children=children,
        skills=skills,
        tools=tools,
        model_policy={"tier": tier, "preferred_models": [preferred], "max_tier": "t3"},
        permissions=perms,
        risk_level=risk,
        mcp_servers=mcp_servers or [],
        identity=identity_for(agent_id),
    )


def build_org() -> list[AgentDef]:
    """The default organization: 12 agents in a hierarchical org chart."""
    return [
        # --- Executive -----------------------------------------------------
        _agent(
            "executive", "Executive Director", "CEO / Orchestrator",
            "Top-level coordinator: understands goals, creates projects, delegates, "
            "prioritizes, resolves conflicts, reviews progress, manages resources.",
            None, ["chief-of-staff", "product-director", "cto", "design-director",
                   "research-director", "marketing-director", "sales-director",
                   "qa-director", "operations-director"],
            ["strategy", "opportunity-scoring", "executive-briefing"],
            ["mattermost.post", "memory.recall", "memory.save", "project.state", "api.call"],
            "t3", "medium",
            {"api.call": "allow", "filesystem.write": "allow", "shell": "allow",
             # the executive may reach across the org for purposeful
             # delegation; directors delegate down their own chains
             "agent.delegate": "allow"},
        ),
        _agent(
            "chief-of-staff", "Chief of Staff", "Coordination",
            "Tracks executive priorities, summarizes work, identifies blockers, "
            "prepares status reports, coordinates departments.",
            "executive", [],
            ["status-reporting", "coordination"],
            ["mattermost.post", "memory.recall", "project.state"],
            "t2",
        ),
        # --- Product -------------------------------------------------------
        _agent(
            "product-director", "Product Director", "Product leadership",
            "Owns product direction across the portfolio; delegates to PMs.",
            "executive", ["product-manager", "requirements-analyst", "product-researcher"],
            ["product-strategy", "prioritization"],
            ["mattermost.post", "memory.recall", "project.state"],
            "t3", "medium",
        ),
        _agent(
            "product-manager", "Product Manager", "Product definition",
            "Converts goals into milestones, tasks, dependencies and reviews; "
            "writes PRDs, user stories and acceptance criteria.",
            "product-director", [],
            ["prd-writing", "user-stories", "roadmap-planning", "feature-prioritization"],
            ["mattermost.post", "memory.recall", "project.state", "filesystem.write"],
            "t2",
            extra_permissions={"filesystem.write": "allow"},
        ),
        _agent(
            "requirements-analyst", "Requirements Analyst", "Requirements",
            "Extracts precise, testable requirements and acceptance criteria.",
            "product-director", [],
            ["requirements-analysis", "acceptance-criteria"],
            ["memory.recall", "project.state"],
            "t1",
        ),
        _agent(
            "product-researcher", "Product Researcher", "Product research",
            "Validates demand, studies users, analyzes competitors and gaps.",
            "product-director", [],
            ["market-research", "competitor-analysis", "demand-validation"],
            ["web.search", "web.scrape", "hn.search", "reddit.search", "memory.recall"],
            "t1",
            extra_permissions={"web.scrape": "allow"},
        ),
        # --- Engineering ---------------------------------------------------
        _agent(
            "cto", "CTO", "Technology leadership",
            "Owns architecture and engineering delivery; delegates to leads.",
            "executive", ["software-architect", "frontend-lead", "backend-lead",
                          "database-engineer", "devops-engineer", "ai-engineer"],
            ["architecture", "tech-decision"],
            ["mattermost.post", "memory.recall", "project.state", "filesystem.read", "mcp.call"],
            "t3", "medium",
            mcp_servers=["context7", "github"],
        ),
        _agent(
            "software-architect", "Software Architect", "Architecture",
            "Designs system architecture, data models and integration contracts.",
            "cto", [],
            ["architecture", "api-design", "database-design"],
            ["filesystem.read", "memory.recall", "project.state", "mcp.call"],
            "t2",
            mcp_servers=["context7", "github"],
            extra_permissions={"mcp.call": "allow"},
        ),
        _agent(
            "frontend-lead", "Frontend Lead", "Frontend engineering",
            "Builds polished production-quality frontends: React/Next/TS, design "
            "quality treated as an engineering requirement.",
            "cto", [],
            ["frontend-engineering", "react-nextjs", "design-quality", "ui-qa", "ui-excellence"],
            ["filesystem.read", "filesystem.write", "shell", "memory.recall", "mcp.call"],
            "t2",
            mcp_servers=["context7", "github"],
            extra_permissions={"filesystem.write": "allow", "shell": "allow", "mcp.call": "allow",
                              "browser.open": "allow", "browser.snapshot": "allow",
                              "browser.click": "allow", "browser.type": "allow",
                              "browser.screenshot": "allow", "browser.close": "allow"},
        ),
        _agent(
            "backend-lead", "Backend Lead", "Backend engineering",
            "Builds APIs, services, data layers and integrations.",
            "cto", [],
            ["backend-engineering", "api-design", "database-design"],
            ["filesystem.read", "filesystem.write", "shell", "memory.recall", "mcp.call"],
            "t2",
            mcp_servers=["context7", "github"],
            extra_permissions={"filesystem.write": "allow", "shell": "allow", "mcp.call": "allow"},
        ),
        _agent(
            "database-engineer", "Database Engineer", "Data layer",
            "Designs schemas, migrations and query optimization. Read-only query "
            "adapter is approval-gated.",
            "cto", [],
            ["database-design", "data-modeling"],
            ["filesystem.read", "memory.recall", "mcp.call"],
            "t1",
            mcp_servers=["context7"],
            extra_permissions={"postgres.query": "allow", "mcp.call": "allow"},
        ),
        _agent(
            "devops-engineer", "DevOps Engineer", "Deployment & infra",
            "Owns deployment, CI/CD and infrastructure; deployments require approval.",
            "cto", [],
            ["deployment", "docker", "monitoring"],
            ["filesystem.read", "shell", "deploy", "github", "memory.recall"],
            "t2", "high",
            extra_permissions={"shell": "allow", "deploy": "allow", "github": "allow",
                              "docker": "allow"},
        ),
        _agent(
            "ai-engineer", "AI Engineer", "AI/ML engineering",
            "Builds agent tooling, prompt systems, evaluations and model plumbing.",
            "cto", [],
            ["ai-engineering", "prompt-engineering", "evaluation"],
            ["filesystem.read", "filesystem.write", "memory.recall", "mcp.call"],
            "t2",
            mcp_servers=["context7", "github"],
            extra_permissions={"filesystem.write": "allow", "mcp.call": "allow"},
        ),
        # --- Design --------------------------------------------------------
        _agent(
            "design-director", "Design Director", "Design leadership",
            "Owns design quality across products; judges whether advanced motion "
            "genuinely improves UX before approving it.",
            "executive", ["ux-designer", "ui-designer", "motion-engineer"],
            ["design-review", "design-quality", "accessibility", "ui-excellence"],
            ["mattermost.post", "memory.recall", "project.state", "filesystem.read", "mcp.call"],
            "t3", "medium",
            mcp_servers=["context7"],
        ),
        _agent(
            "ux-designer", "UX Designer", "UX",
            "Information architecture, flows, hierarchy, usability, accessibility.",
            "design-director", [],
            ["ux-design", "information-architecture", "accessibility", "ui-excellence"],
            ["filesystem.read", "memory.recall", "mcp.call"],
            "t2",
            mcp_servers=["context7"],
            extra_permissions={"mcp.call": "allow"},
        ),
        _agent(
            "ui-designer", "UI Designer", "UI / visual",
            "Visual design: typography, color systems, spacing, grids, components.",
            "design-director", [],
            ["ui-design", "design-systems", "visual-hierarchy", "ui-excellence"],
            ["filesystem.read", "memory.recall", "mcp.call"],
            "t2",
            mcp_servers=["context7"],
            extra_permissions={"browser.open": "allow", "browser.snapshot": "allow", "mcp.call": "allow",
                              "browser.close": "allow"},
        ),
        _agent(
            "motion-engineer", "Motion / Creative Engineer", "Motion & creative engineering",
            "GSAP, ScrollTrigger, Lenis, Three.js, R3F, WebGL, shaders — used only "
            "when they genuinely improve the experience (performance/a11y/mobile aware).",
            "design-director", [],
            ["motion-engineering", "gsap", "threejs", "webgl"],
            ["filesystem.read", "filesystem.write", "shell", "memory.recall"],
            "t2",
            extra_permissions={"filesystem.write": "allow", "shell": "allow"},
        ),
        # --- Research ------------------------------------------------------
        _agent(
            "research-director", "Research Director", "Research leadership",
            "Owns market, community, competitor and technical research.",
            "executive", ["market-researcher", "community-researcher",
                          "competitor-analyst", "technical-researcher"],
            ["research-planning", "evidence-synthesis"],
            ["mattermost.post", "memory.recall", "web.search", "hn.search", "reddit.search"],
            "t3", "medium",
        ),
        _agent(
            "market-researcher", "Market Researcher", "Market research",
            "Market sizing, trends, demand validation, customer language.",
            "research-director", [],
            ["market-research", "demand-validation", "opportunity-scoring"],
            ["web.search", "web.scrape", "hn.search", "reddit.search", "memory.recall"],
            "t1",
            extra_permissions={"web.scrape": "allow"},
        ),
        _agent(
            "community-researcher", "Community Researcher", "Community intelligence",
            "Reddit/community research: pain points, feature requests, buying "
            "signals, sentiment — evidence-oriented, multi-source by design.",
            "research-director", [],
            ["community-intelligence", "reddit-research", "sentiment-analysis"],
            ["web.search", "web.scrape", "api.call", "hn.search", "reddit.search", "memory.recall"],
            "t2",
            extra_permissions={"web.scrape": "allow", "api.call": "allow",
                              "browser.open": "allow", "browser.snapshot": "allow",
                              "browser.close": "allow"},
        ),
        _agent(
            "competitor-analyst", "Competitor Analyst", "Competitive analysis",
            "Competitor monitoring, differentiation, moat analysis.",
            "research-director", [],
            ["competitor-analysis", "positioning", "moat-analysis"],
            ["web.search", "memory.recall"],
            "t1",
        ),
        _agent(
            "technical-researcher", "Technical Researcher", "Technical research",
            "Documentation research, tooling evaluation, technical deep dives.",
            "research-director", [],
            ["technical-research", "api-evaluation"],
            ["web.search", "web.scrape", "hn.search", "reddit.search", "memory.recall"],
            "t1",
            extra_permissions={"web.scrape": "allow",
                              "browser.open": "allow", "browser.snapshot": "allow",
                              "browser.close": "allow"},
        ),
        # --- Marketing -----------------------------------------------------
        _agent(
            "marketing-director", "Marketing Director", "Marketing leadership",
            "Owns positioning, messaging and go-to-market.",
            "executive", ["seo-agent", "content-agent", "social-agent", "growth-agent"],
            ["marketing-strategy", "positioning", "messaging"],
            ["mattermost.post", "memory.recall", "web.search", "hn.search", "reddit.search"],
            "t3", "medium",
        ),
        _agent(
            "seo-agent", "SEO Agent", "SEO",
            "Keyword research, on-page SEO, technical SEO, structured data.",
            "marketing-director", [],
            ["seo", "content-strategy", "web-seo-jsonld"],
            ["web.search", "filesystem.read", "memory.recall"],
            "t1",
        ),
        _agent(
            "content-agent", "Content Agent", "Content",
            "Copywriting, landing pages, content strategy grounded in the product.",
            "marketing-director", [],
            ["copywriting", "landing-page-design", "content-strategy"],
            ["filesystem.read", "filesystem.write", "memory.recall"],
            "t1",
            extra_permissions={"filesystem.write": "allow"},
        ),
        _agent(
            "social-agent", "Social Agent", "Social",
            "Social content, audience research, campaign planning.",
            "marketing-director", [],
            ["social-content", "audience-research"],
            ["web.search", "memory.recall"],
            "t1",
        ),
        _agent(
            "growth-agent", "Growth Agent", "Growth",
            "Conversion optimization, growth loops, experimentation.",
            "marketing-director", [],
            ["conversion-optimization", "growth-strategy"],
            ["web.search", "memory.recall"],
            "t1",
        ),
        # --- Sales ---------------------------------------------------------
        _agent(
            "sales-director", "Sales Director", "Sales leadership",
            "Owns lead pipeline and outreach quality; external sends need approval.",
            "executive", ["lead-researcher", "sales-analyst", "outreach-specialist"],
            ["sales-strategy", "pipeline-analysis"],
            ["mattermost.post", "memory.recall", "web.search", "hn.search", "reddit.search"],
            "t2", "medium",
        ),
        _agent(
            "lead-researcher", "Lead Researcher", "Lead research",
            "Lead discovery and prospect analysis.",
            "sales-director", [],
            ["lead-research", "prospect-analysis"],
            ["web.search", "web.scrape", "api.call", "hn.search", "reddit.search", "memory.recall"],
            "t1",
            extra_permissions={"web.scrape": "allow"},
        ),
        _agent(
            "sales-analyst", "Sales Analyst", "Sales analysis",
            "Pipeline analysis, sales strategy, personalization.",
            "sales-director", [],
            ["sales-analysis", "sales-strategy"],
            ["memory.recall"],
            "t1",
        ),
        _agent(
            "outreach-specialist", "Outreach Specialist", "Outreach",
            "Drafts personalized outreach; drafts are reviewed before any send.",
            "sales-director", [],
            ["outreach-drafting"],
            ["filesystem.read", "filesystem.write", "memory.recall"],
            "t1", "medium",
            extra_permissions={"filesystem.write": "allow"},
        ),
        # --- QA ------------------------------------------------------------
        _agent(
            "qa-director", "QA Director", "Quality leadership",
            "Owns testing, review and security gates.",
            "executive", ["test-engineer", "code-reviewer", "security-reviewer",
                          "performance-reviewer"],
            ["testing-strategy", "quality-gates"],
            ["mattermost.post", "memory.recall", "project.state"],
            "t2", "medium",
        ),
        _agent(
            "test-engineer", "Test Engineer", "Testing",
            "Writes and RUNS unit/integration/e2e/browser tests. Evidence, not claims.",
            "qa-director", [],
            ["testing", "test-automation", "e2e-testing", "ui-qa"],
            ["filesystem.read", "filesystem.write", "shell", "memory.recall"],
            "t1",
            extra_permissions={"filesystem.write": "allow", "shell": "allow",
                              "browser.open": "allow", "browser.snapshot": "allow",
                              "browser.click": "allow", "browser.type": "allow",
                              "browser.screenshot": "allow", "browser.close": "allow"},
        ),
        _agent(
            "code-reviewer", "Code Reviewer", "Code review",
            "Structured reviews: correctness, readability, architecture, security, "
            "performance, testing, maintainability.",
            "qa-director", [],
            ["code-review", "implementation-review"],
            ["filesystem.read", "memory.recall"],
            "t2",
        ),
        _agent(
            "security-reviewer", "Security Reviewer", "Security review",
            "Secrets, injection, unsafe commands, authz, dependency risk.",
            "qa-director", [],
            ["security-review", "threat-modeling"],
            ["filesystem.read", "memory.recall"],
            "t2", "high",
        ),
        _agent(
            "performance-reviewer", "Performance Reviewer", "Performance review",
            "Runtime/load/UX performance review.",
            "qa-director", [],
            ["performance-review"],
            ["filesystem.read", "memory.recall"],
            "t1",
        ),
        # --- Operations ----------------------------------------------------
        _agent(
            "operations-director", "Operations Director", "Operations leadership",
            "Owns delivery operations: projects, docs, deployments, monitoring.",
            "executive", ["project-manager", "documentation-agent", "deployment-agent",
                          "monitoring-agent"],
            ["operations-strategy"],
            ["mattermost.post", "memory.recall", "project.state"],
            "t2", "medium",
        ),
        _agent(
            "project-manager", "Project Manager", "Project management",
            "Converts goals into milestones, tasks, dependencies, assignments and "
            "reviews; monitors progress continuously.",
            "operations-director", [],
            ["project-planning", "task-decomposition", "status-reporting"],
            ["filesystem.read", "filesystem.write", "memory.recall", "project.state"],
            "t2",
            extra_permissions={"filesystem.write": "allow"},
        ),
        _agent(
            "documentation-agent", "Documentation Agent", "Documentation",
            "Writes and maintains documentation.",
            "operations-director", [],
            ["documentation"],
            ["filesystem.read", "filesystem.write", "memory.recall"],
            "t1",
            extra_permissions={"filesystem.write": "allow"},
        ),
        _agent(
            "deployment-agent", "Deployment Agent", "Deployment",
            "Executes deployment workflows. Production deploys require approval.",
            "operations-director", [],
            ["deployment", "release-management"],
            ["shell", "deploy", "github", "deploy.github", "render.manage", "filesystem.read", "memory.recall"],
            "t2", "high",
            extra_permissions={"shell": "allow", "deploy": "allow", "github": "allow"},
        ),
        _agent(
            "monitoring-agent", "Monitoring Agent", "Monitoring",
            "Health checks, error triage, incident response.",
            "operations-director", [],
            ["monitoring", "incident-response"],
            ["shell", "memory.recall", "mattermost.post"],
            "t1", "medium",
            extra_permissions={"shell": "allow", "docker": "allow"},
        ),
    ]


ORG_AGENTS = {a.id: a for a in build_org()}