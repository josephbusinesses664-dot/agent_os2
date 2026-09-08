"""Core domain models.

These are the canonical types for every subsystem: agents, tasks, projects,
skills, tools, MCP servers, models, budgets, events, memory, approvals, audit
and agent-to-agent messages. They are plain pydantic objects so they can be
stored, serialized, validated and exchanged freely.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Agent identity (structured cognitive layer)
# ---------------------------------------------------------------------------

class AgentIdentity(BaseModel):
    """Structured cognitive identity for one agent. Operated by
    agentos.agents.identity (archetypes, prompt rendering) and the runtime.
    Identity shapes judgment; it never grants permissions."""

    archetype: str = "engineer"  # executive/architect/researcher/product/
    #                              designer/engineer/qa/security/operations/
    #                              sales/marketing
    mission: str = ""            # one sentence: what this agent is FOR
    priorities: list[str] = Field(default_factory=list)  # ranked, first = highest
    decision_framework: list[str] = Field(default_factory=list)  # ordered questions
    risk_tolerance: str = "moderate"  # minimal | low | moderate | high
    autonomy: str = "L2"             # L0..L5 (see agents/identity.py)
    evidence_standard: str = ""      # what counts as proof for this role
    quality_standard: str = ""       # the bar the agent holds work to
    anti_patterns: list[str] = Field(default_factory=list)  # behaviors to refuse
    communication_style: str = ""
    disagreement_style: str = ""
    escalation_policy: str = ""
    failure_behavior: str = ""


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

class AgentStatus(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"
    FAILED = "failed"
    PAUSED = "paused"
    OFFLINE = "offline"


class AgentDef(BaseModel):
    """Static definition of an agent (lives in the Agent Registry)."""

    id: str
    name: str
    role: str
    description: str = ""
    parent_agent: Optional[str] = None
    allowed_children: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    model_policy: dict[str, Any] = Field(
        default_factory=lambda: {"tier": "t2", "preferred_models": [], "max_tier": "t3"}
    )
    budget_policy: dict[str, Any] = Field(
        default_factory=lambda: {"max_per_task": 0.5, "max_per_day": 5.0}
    )
    permissions: dict[str, str] = Field(default_factory=dict)  # tool -> allow|deny
    memory_scope: str = "agent"  # agent | project | org
    risk_level: str = "low"  # low | medium | high
    # Structured identity: archetype, priorities, decision framework, risk
    # tolerance, autonomy, comms/disagreement style. See agents/identity.py.
    # Optional so persisted pre-upgrade agents still load.
    identity: Optional[AgentIdentity] = None
    version: int = 1
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)

    def allows(self, tool: str) -> bool:
        return self.permissions.get(tool, "allow") == "allow"


class AgentInstance(BaseModel):
    """Runtime state of an agent."""

    agent_id: str
    status: AgentStatus = AgentStatus.IDLE
    current_task_id: Optional[str] = None
    current_project_id: Optional[str] = None
    model: Optional[str] = None
    started_at: Optional[datetime] = None
    latest_action: str = ""
    error: Optional[str] = None
    spawn_depth: int = 0


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"   # evidence check / repair iteration in progress
    RECOVERING = "recovering" # failed run being retried or reassigned
    BLOCKED = "blocked"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(BaseModel):
    task_id: str
    project_id: str
    title: str
    description: str = ""
    parent_task: Optional[str] = None
    assigned_agent: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    priority: str = "normal"  # low | normal | high | critical
    dependencies: list[str] = Field(default_factory=list)
    budget: Optional[float] = None
    deadline: Optional[datetime] = None
    artifacts: list[str] = Field(default_factory=list)
    result: Optional[str] = None
    review_status: Optional[str] = None  # pending | passed | failed
    created_by: str = "executive"
    agent_chain: list[str] = Field(default_factory=list)
    model_used: Optional[str] = None
    cost: float = 0.0
    error: Optional[str] = None
    retry_count: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch(self) -> None:
        self.updated_at = utcnow()


class TaskDependency(BaseModel):
    """Declares that `task_id` depends on `depends_on`."""

    task_id: str
    depends_on: str
    project_id: str


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class ProjectStatus(str, Enum):
    IDEA = "idea"
    DISCOVERY = "discovery"
    ACTIVE = "active"
    IN_REVIEW = "in_review"
    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class Project(BaseModel):
    project_id: str
    name: str
    objective: str = ""
    status: ProjectStatus = ProjectStatus.IDEA
    stage: str = ""  # current workflow stage id
    workflow_id: Optional[str] = None
    requirements: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    budget: float = 0.0
    cost: float = 0.0
    created_by: str = "executive"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch(self) -> None:
        self.updated_at = utcnow()


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

class SkillContract(BaseModel):
    """Machine-readable operational contract for a skill (the skill-system
    upgrade's Phase-2 contract). The registry, planner and evaluator can read
    these fields; the skill body carries the human-readable operating
    procedure. All fields optional — a knowledge-only skill may carry only
    prerequisites and references.
    """

    prerequisites: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)  # capability/tool-group ids
    preferred_agents: list[str] = Field(default_factory=list)
    preferred_models: list[str] = Field(default_factory=list)
    minimum_model_capability: str = ""  # t0 | t1 | t2 | t3
    expected_cost: str = ""  # low | medium | high
    expected_latency: str = ""  # e.g. "minutes" | "hours"
    evidence_requirements: list[str] = Field(default_factory=list)
    artifact_contract: list[str] = Field(default_factory=list)  # artifacts produced
    quality_gates: list[str] = Field(default_factory=list)  # definition of done
    verification: list[str] = Field(default_factory=list)  # how artifacts are verified
    failure_modes: dict[str, str] = Field(default_factory=dict)  # failure -> recovery strategy
    escalation: list[str] = Field(default_factory=list)  # when to escalate to a human/parent
    handoff_in: list[str] = Field(default_factory=list)  # fields expected from predecessor
    handoff_out: list[str] = Field(default_factory=list)  # fields passed to the next skill
    evaluation: list[str] = Field(default_factory=list)  # how Agent OS measures this skill
    observability: list[str] = Field(default_factory=list)  # what to record in traces/audit
    related_skills: list[str] = Field(default_factory=list)


class SkillDef(BaseModel):
    id: str
    name: str
    description: str
    category: str
    version: str = "1.0.0"
    source: str = ""  # provenance: repo / author / license
    license: str = ""
    capability_type: str = "skill"  # skill | agent | workflow | tool | prompt | hook
    required_tools: list[str] = Field(default_factory=list)
    required_models: list[str] = Field(default_factory=list)
    risk_level: str = "low"
    cost_level: str = "low"
    dependencies: list[str] = Field(default_factory=list)
    compatible_agents: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    body: str = ""  # full markdown body (loaded lazily where practical)
    source_path: str = ""
    # --- skill contract (machine-readable operational metadata) -----------
    contract: SkillContract = Field(default_factory=SkillContract)
    # reference filenames under <skill_dir>/references/ for progressive
    # disclosure — loaded on demand, never into the base context
    references: list[str] = Field(default_factory=list)
    # --- executable capability layer -------------------------------------
    # A skill can be more than a prompt: attach executable tools, hooks,
    # validators, model settings, permissions, examples and tests.
    tools: list[CapabilityTool] = Field(default_factory=list)
    hooks: dict[str, str] = Field(default_factory=dict)  # pre_<tool>/post_<tool> -> inline python
    validators: list[str] = Field(default_factory=list)  # inline python: async def validate(ctx, result) -> dict
    model_settings: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, str] = Field(default_factory=dict)  # extra permission grants while skill active
    examples: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)  # inline python: async def test(ctx) -> dict


# ---------------------------------------------------------------------------
# Tools / MCP
# ---------------------------------------------------------------------------

class ToolDef(BaseModel):
    name: str
    description: str
    permission_key: str = ""
    risk_level: str = "low"
    enabled: bool = True
    category: str = "builtin"
    config: dict[str, Any] = Field(default_factory=dict)


class McpServer(BaseModel):
    name: str
    description: str = ""
    transport: str = "streamable-http"  # streamable-http | stdio | builtin
    endpoint: Optional[str] = None
    command: Optional[str] = None  # for stdio
    args: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    permissions: dict[str, str] = Field(default_factory=dict)
    risk_level: str = "medium"
    required_credentials: list[str] = Field(default_factory=list)
    # auth config: {"type": "bearer" | "header" | "basic", ...} — the token
    # itself is redacted from serialized output; prefer set_credentials()
    auth: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    # -- governance (MCP upgrade): provenance & trust ------------------------
    trust: str = "untrusted"  # trusted | reviewed | untrusted | blocked
    owner: str = ""  # provenance: who maintains this server ("" = unknown)
    server_version: str = ""  # as reported by the server; never invented
    allowed_agents: list[str] = Field(default_factory=list)  # empty = all authorized
    tool_trust: dict[str, str] = Field(default_factory=dict)  # tool -> trust override
    data_sensitivity: str = "normal"  # normal | sensitive | restricted
    # -- health / circuit breaker -------------------------------------------
    rate_limit_per_min: Optional[int] = None  # unknown stays None

    def public_dict(self) -> dict[str, Any]:
        """Serialize without credential material."""
        d = self.model_dump(mode="json")
        if d.get("auth"):
            d["auth"] = {"type": self.auth.get("type", "configured"),
                          "token": "<redacted>" if self.auth.get("token") else None}
        return d


class McpTrustLevel(str, Enum):
    """Trust ladder for MCP servers/tools. Trust influences discovery,
    recommendation and approval — it never bypasses permissions."""

    TRUSTED = "trusted"      # official / internal / verified
    REVIEWED = "reviewed"    # externally maintained, inspected
    UNTRUSTED = "untrusted"  # unknown provenance
    BLOCKED = "blocked"      # explicitly prohibited


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ModelDef(BaseModel):
    id: str
    provider: str  # deepseek | anthropic | glm | openai_compat | echo
    name: str  # provider-side model name
    tier: str = "t2"  # t0 deterministic | t1 cheap | t2 senior | t3 executive
    context_window: int = 128000
    price_in_per_million: float = 0.0  # USD
    price_out_per_million: float = 0.0
    enabled: bool = True
    capabilities: list[str] = Field(default_factory=list)
    fallbacks: list[str] = Field(default_factory=list)


class ModelRequest(BaseModel):
    model_id: str
    messages: list[dict[str, str]] = Field(default_factory=list)  # [{role, content}]
    system: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    request_id: str = Field(default_factory=lambda: new_id("req"))
    project_id: Optional[str] = None
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    tools: list[dict] = Field(default_factory=list)  # OpenAI-format tool schemas


class ModelResponse(BaseModel):
    request_id: str
    model_id: str
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    finish_reason: str = "stop"
    error: Optional[str] = None


class UsageRecord(BaseModel):
    usage_id: str = Field(default_factory=lambda: new_id("use"))
    ts: datetime = Field(default_factory=utcnow)
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost: float = 0.0
    project_id: Optional[str] = None
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    request_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

class BudgetScope(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"
    AGENT = "agent"
    TASK = "task"


class Budget(BaseModel):
    scope: BudgetScope
    scope_id: str = "global"
    monthly_limit: Optional[float] = None
    daily_limit: Optional[float] = None
    spent_month: float = 0.0
    spent_day: float = 0.0
    day_bucket: str = ""  # YYYY-MM-DD for the spent_day bucket
    updated_at: datetime = Field(default_factory=utcnow)


class BudgetDecision(BaseModel):
    allowed: bool
    action: str  # approve | downgrade | reject
    reason: str
    suggested_model: Optional[str] = None


# ---------------------------------------------------------------------------
# Events / Observability
# ---------------------------------------------------------------------------

class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    ts: datetime = Field(default_factory=utcnow)
    type: str
    source: str = "system"
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    severity: str = "info"  # info | warning | error | critical
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump()
        d["ts"] = self.ts.isoformat()
        return d


class AuditEntry(BaseModel):
    audit_id: str = Field(default_factory=lambda: new_id("aud"))
    ts: datetime = Field(default_factory=utcnow)
    actor: str = "system"
    action: str
    target: str = ""
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    tool: Optional[str] = None
    model: Optional[str] = None
    result: str = "ok"
    details: dict[str, Any] = Field(default_factory=dict)


class HealthReport(BaseModel):
    service: str
    status: str  # ok | degraded | down
    latency_ms: int = 0
    detail: str = ""


class ApiDef(BaseModel):
    """An API in the API catalog."""

    api: str
    category: str
    description: str = ""
    authentication: str = "none"  # none | api_key | oauth
    pricing: str = "free"  # free | freemium | paid
    rate_limit: str = "unknown"
    commercial_use: bool = True
    documentation: str = ""
    reliability: str = "good"
    agent_compatibility: int = 3  # 1..5
    base_url: str = ""
    enabled: bool = True


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class MemoryScope(str, Enum):
    AGENT = "agent"
    PROJECT = "project"
    ORG = "org"
    USER = "user"
    TASK = "task"


class MemoryEntry(BaseModel):
    memory_id: str = Field(default_factory=lambda: new_id("mem"))
    scope: MemoryScope
    owner_id: str  # agent id / project id / "org" / user id / task id
    kind: str = "fact"  # decision | preference | lesson | fact | instruction | episode | evaluation
    content: str
    importance: int = 3  # 1..5
    tags: list[str] = Field(default_factory=list)
    # provenance / lifecycle (Graphiti/Mem0-style: who said it, when, and how sure)
    source: str = "agent"  # agent | tool | human | system | evaluation
    provenance: str = ""  # e.g. "agent:frontend-lead task:task_abc"
    confidence: float = 0.8  # 0..1
    supersedes: Optional[str] = None  # memory_id of the entry this one replaces
    archived: bool = False
    access_count: int = 0
    expires_at: Optional[datetime] = None
    # temporal validity (Graphiti-style): facts hold between valid_from and
    # valid_to; expired facts are archived lazily and never recalled
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def is_valid(self, at: Optional[datetime] = None) -> bool:
        now = at or utcnow()
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_to and now > self.valid_to:
            return False
        return True


class MemoryLink(BaseModel):
    """A typed relationship between two memory entries — the knowledge-graph
    layer on top of the flat memory store (Graphiti-inspired)."""

    link_id: str = Field(default_factory=lambda: new_id("lnk"))
    source_id: str
    target_id: str
    relation: str = "related"  # related | part_of | contradicts | supersedes | applies_to | caused_by | used_by
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Hard security policies (immutable — always above the hierarchy)
# ---------------------------------------------------------------------------

class PolicyKind(str, Enum):
    """What a hard security policy does when it matches a tool call.

    deny_tool         → the call is refused outright, even for executives
    require_approval  → the call needs human approval regardless of the
                        agent's permissions or risk level
    restrict_scope    → the call is allowed but coerced into a narrower
                        permission scope (e.g. shell → read-only)
    """

    DENY_TOOL = "deny_tool"
    REQUIRE_APPROVAL = "require_approval"
    RESTRICT_SCOPE = "restrict_scope"


class SecurityPolicy(BaseModel):
    """A declarative, immutable rule evaluated on EVERY tool call BEFORE the
    agent's own permissions. Empty match lists mean "all". The org chart
    grants authority; policies bound it — a senior agent cannot override a
    matching policy, because enforcement happens above the hierarchy (the
    executor checks policies first)."""

    id: str
    kind: PolicyKind
    description: str = ""
    tools: list[str] = Field(default_factory=list)       # empty = all tools
    agents: list[str] = Field(default_factory=list)      # empty = all agents
    roles: list[str] = Field(default_factory=list)       # empty = all roles
    environments: list[str] = Field(default_factory=list)  # empty = all environments
    arg_pattern: str = ""  # regex on serialized args; only matches when found
    reason: str = ""       # shown to the agent / audit log when the policy fires
    immutable: bool = True
    enabled: bool = True
    approval_risk: str = "high"  # require_approval: risk level of the approval
    scope: str = ""              # restrict_scope: the coerced permission scope


# ---------------------------------------------------------------------------
# Workspaces (isolated engineering worktrees)
# ---------------------------------------------------------------------------

class WorkspaceStatus(str, Enum):
    ISOLATED = "isolated"
    WORKING = "working"
    MERGED = "merged"
    DISCARDED = "discarded"
    FAILED = "failed"


class WorkspaceRecord(BaseModel):
    """One isolated engineering workspace (git worktree, or a plain sandbox
    directory when the project has no repository). Agents work here without
    touching the shared tree; integrate() merges the work back."""

    workspace_id: str = Field(default_factory=lambda: new_id("ws"))
    project_id: str
    task_id: str = ""
    agent_id: str = ""
    repo_path: str = ""       # base repository ("" = no repository)
    worktree_path: str = ""   # the isolated working directory
    branch: str = ""          # the worktree branch (worktree mode)
    base_branch: str = ""     # branch the worktree was cut from
    mode: str = "worktree"    # worktree | plain
    status: WorkspaceStatus = WorkspaceStatus.ISOLATED
    commits: list[str] = Field(default_factory=list)  # hashes made in the workspace
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch(self) -> None:
        self.updated_at = utcnow()


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class ApprovalRequest(BaseModel):
    approval_id: str = Field(default_factory=lambda: new_id("apr"))
    agent_id: str
    agent_name: str = ""
    task_id: Optional[str] = None
    project_id: Optional[str] = None
    action: str
    risk_level: str = "high"
    reason: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    decision_note: str = ""
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Agent-to-agent messaging
# ---------------------------------------------------------------------------

class MessageType(str, Enum):
    TASK_REQUEST = "task_request"
    TASK_RESULT = "task_result"
    QUESTION = "question"
    APPROVAL = "approval"
    ESCALATION = "escalation"
    WARNING = "warning"
    FAILURE = "failure"
    STATUS_UPDATE = "status_update"
    REVIEW = "review"
    DISCOVERY = "discovery"
    DECISION = "decision"
    HANDOFF = "handoff"
    BLOCKER = "blocker"
    CHALLENGE = "challenge"
    ARTIFACT = "artifact"
    APPROVAL_RESULT = "approval_result"


class AgentMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: new_id("msg"))
    message_type: MessageType
    sender: str
    recipient: str = "executive"
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    priority: str = "normal"
    payload: dict[str, Any] = Field(default_factory=dict)
    requires_response: bool = False
    response_to: Optional[str] = None
    status: str = "sent"  # sent | delivered | answered
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

class DecisionRecord(BaseModel):
    decision_id: str = Field(default_factory=lambda: new_id("dec"))
    project: Optional[str] = None
    decision: str
    alternatives: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""
    decided_by: str = "executive"
    date: datetime = Field(default_factory=utcnow)
    reversible: bool = True


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

class WorkflowStage(BaseModel):
    stage_id: str
    name: str
    agent_role: str  # agent registry id used to run this stage
    description: str = ""
    requires_approval: bool = False
    depends_on: list[str] = Field(default_factory=list)
    next: Optional[str] = None
    artifact_prefix: str = ""


class WorkflowDef(BaseModel):
    workflow_id: str
    name: str
    description: str = ""
    entry_stage: str
    stages: list[WorkflowStage]
    version: str = "1.0.0"
    source_path: str = ""

    def stage_map(self) -> dict[str, WorkflowStage]:
        return {s.stage_id: s for s in self.stages}


# ---------------------------------------------------------------------------
# Results / evaluation
# ---------------------------------------------------------------------------

class StageResult(BaseModel):
    stage_id: str
    agent_id: str
    task_id: Optional[str] = None
    status: str = "completed"  # completed | failed | skipped | awaiting_approval
    output: str = ""
    artifacts: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    cost: float = 0.0
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    reflection: dict[str, Any] = Field(default_factory=dict)


class AgentEvaluation(BaseModel):
    """Legacy per-agent evaluation record (kept for compatibility)."""

    eval_id: str = Field(default_factory=lambda: new_id("ev"))
    agent_id: str
    ts: datetime = Field(default_factory=utcnow)
    task_id: Optional[str] = None
    outcome: str = "completed"
    review_score: float = 0.0  # 0..5
    cost: float = 0.0
    tool_usage: list[str] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Capabilities (executable skills)
# ---------------------------------------------------------------------------

class CapabilityTool(BaseModel):
    """An executable tool attached to a skill/capability.

    Either `handler_ref` points at a built-in handler name (preferred, safe)
    or `code` carries inline async python source defining `handler(ctx, args)`.
    """

    name: str
    description: str = ""
    permission_key: str = ""
    risk_level: str = "low"
    handler_ref: str = ""  # e.g. "repo.search" resolved against the tool registry
    code: str = ""  # inline python: async def handler(ctx, args) -> dict
    timeout_seconds: int = 30
    cost_per_call: float = 0.0
    category: str = "capability"
    config: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Telemetry / tracing
# ---------------------------------------------------------------------------

class TraceSpan(BaseModel):
    span_id: str = Field(default_factory=lambda: new_id("sp"))
    trace_id: str = ""  # task id or run id the span belongs to
    parent_span: Optional[str] = None
    kind: str = "agent"  # agent | stage | model | tool | evaluator | memory | workflow
    name: str = ""
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    project_id: Optional[str] = None
    model: Optional[str] = None
    tool: Optional[str] = None
    status: str = "ok"  # ok | error | pending
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    error: Optional[str] = None
    fail_class: Optional[str] = None
    result_summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d["started_at"] = self.started_at.isoformat()
        d["finished_at"] = self.finished_at.isoformat() if self.finished_at else None
        return d


# ---------------------------------------------------------------------------
# Agent performance (operational incentives)
# ---------------------------------------------------------------------------

class PerformanceStats(BaseModel):
    """Rolling performance statistics for an agent. These are operational:
    routing and delegation decisions read them, nothing cosmetic."""

    agent_id: str
    window: str = "all"  # all | daily | weekly
    day_bucket: str = ""
    runs: int = 0
    completed: int = 0
    failed: int = 0
    success_rate: float = 1.0  # 1.0 when no data (neutral prior)
    avg_review_score: float = 0.0
    review_count: int = 0
    total_cost: float = 0.0
    avg_cost: float = 0.0
    total_tokens: int = 0
    avg_latency_ms: float = 0.0
    total_retries: int = 0
    tool_calls: int = 0
    failed_tool_calls: int = 0
    tool_efficiency: float = 1.0  # 1 - failed/total (1.0 with no data)
    evaluations_passed: int = 0
    evaluations_total: int = 0
    # downstream success: whether work this agent delegated came back green
    downstream_success: int = 0
    downstream_total: int = 0
    downstream_rate: float = 1.0
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

class EvaluationRecord(BaseModel):
    """Canonical evaluation record: ties a task outcome to an evaluator score."""

    eval_id: str = Field(default_factory=lambda: new_id("evl"))
    ts: datetime = Field(default_factory=utcnow)
    task_id: Optional[str] = None
    project_id: Optional[str] = None
    agent_id: Optional[str] = None
    model_id: Optional[str] = None
    skill_id: Optional[str] = None
    workflow_id: Optional[str] = None
    evaluator: str = "deterministic"  # deterministic | llm_judge | task_specific
    metric: str = "quality"
    score: float = 0.0  # 0..5
    passed: bool = False
    verdict: str = ""
    reasons: list[str] = Field(default_factory=list)
    cost: float = 0.0
    latency_ms: int = 0
    fail_class: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d["ts"] = self.ts.isoformat()
        return d


class SkillPerformance(BaseModel):
    """Per-skill performance aggregation (one more view over the same
    EvaluationRecords — not a second evaluation system). Tracks how often a
    skill is used and how well it performs per agent and per model, so the
    organization can learn "agent A + model X is unusually good at this
    skill". Only real evaluation observations are folded in; nothing fake.
    """

    skill_id: str
    runs: int = 0
    passed: int = 0
    pass_rate: float = 0.0
    avg_score: float = 0.0
    total_cost: float = 0.0
    avg_latency_ms: float = 0.0
    total_retries: int = 0
    by_agent: dict[str, dict[str, float]] = Field(default_factory=dict)
    by_model: dict[str, dict[str, float]] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utcnow)


class EvaluationRunSummary(BaseModel):
    """Result of one benchmark/regression run over a dataset."""

    run_id: str = Field(default_factory=lambda: new_id("evrun"))
    dataset: str = ""
    ts: datetime = Field(default_factory=utcnow)
    total: int = 0
    passed: int = 0
    avg_score: float = 0.0
    total_cost: float = 0.0
    avg_latency_ms: float = 0.0
    by_agent: dict[str, dict[str, float]] = Field(default_factory=dict)
    by_model: dict[str, dict[str, float]] = Field(default_factory=dict)
    by_skill: dict[str, dict[str, float]] = Field(default_factory=dict)
    by_fail_class: dict[str, int] = Field(default_factory=dict)