"""Services bundle.

One composition root: every subsystem is constructed here from settings and
storage, and passed around as `svc`. Subsystems only depend on interfaces
(registries, stores), never on each other's internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agentos.agents.runtime import AgentRuntime, RuntimeContext
from agentos.budgets.manager import BudgetManager
from agentos.capabilities import CapabilityManager
from agentos.config import Settings
from agentos.db.memory import MemoryKV, MemoryQueue, MemoryRepository
from agentos.db.postgres import PostgresRepository, RedisKV, RedisQueue
from agentos.db.store import EntityStore
from agentos.evaluation import BenchmarkRunner
from agentos.memory.store import MemoryStore
from agentos.messaging.inbox import MessageBus
from agentos.models.provider import build_providers
from agentos.models.router import ModelRouter
from agentos.observability.events import EventBus
from agentos.observability.trace import Tracer
from agentos.performance import PerformanceTracker
from agentos.planning import DynamicPlanner
from agentos.projects.service import ProjectService
from agentos.prompts.library import PromptLibrary
from agentos.registries.agent_registry import AgentRegistry
from agentos.registries.api_registry import ApiRegistry
from agentos.registries.mcp_registry import McpRegistry
from agentos.security.rollback import RollbackLedger, compensate_filesystem_write
from agentos.registries.model_registry import ModelRegistry
from agentos.registries.skill_registry import SkillRegistry
from agentos.registries.tool_registry import ToolRegistry
from agentos.security.approvals import ApprovalService
from agentos.security.audit import AuditLog
from agentos.security.policy import PolicyEngine
from agentos.tasks.service import TaskService
from agentos.workspaces.manager import WorkspaceManager
from agentos.tools.executor import ToolExecutor
from agentos.workflows.loader import WorkflowRegistry


@dataclass
class Services:
    settings: Settings
    store: Any = None
    entity_store: EntityStore = None  # type: ignore[assignment]
    kv: Any = None
    queue: Any = None

    agent_registry: AgentRegistry = None  # type: ignore[assignment]
    skill_registry: SkillRegistry = None  # type: ignore[assignment]
    tool_registry: ToolRegistry = None  # type: ignore[assignment]
    mcp_registry: McpRegistry = None  # type: ignore[assignment]
    api_registry: ApiRegistry = None  # type: ignore[assignment]
    model_registry: ModelRegistry = None  # type: ignore[assignment]
    workflow_registry: WorkflowRegistry = None  # type: ignore[assignment]

    providers: dict = field(default_factory=dict)
    router: ModelRouter = None  # type: ignore[assignment]
    budgets: BudgetManager = None  # type: ignore[assignment]

    events: EventBus = None  # type: ignore[assignment]
    tracer: Tracer = None  # type: ignore[assignment]
    audit: AuditLog = None  # type: ignore[assignment]
    approvals: ApprovalService = None  # type: ignore[assignment]
    policy: PolicyEngine = None  # type: ignore[assignment]
    workspaces: WorkspaceManager = None  # type: ignore[assignment]
    coding: Any = None  # CodingHarness — see agentos.workspaces.harness
    memory: MemoryStore = None  # type: ignore[assignment]
    messages: MessageBus = None  # type: ignore[assignment]
    projects: ProjectService = None  # type: ignore[assignment]
    tasks: TaskService = None  # type: ignore[assignment]
    prompts: PromptLibrary = None  # type: ignore[assignment]
    executor: ToolExecutor = None  # type: ignore[assignment]
    capabilities: CapabilityManager = None  # type: ignore[assignment]
    performance: PerformanceTracker = None  # type: ignore[assignment]
    evaluation: BenchmarkRunner = None  # type: ignore[assignment]
    planner: DynamicPlanner = None  # type: ignore[assignment]

    mattermost: Any = None
    web_search: Any = None
    locks: Any = None  # LockManager — distributed mutexes (Redis or in-process)

    workspace: Path = None  # type: ignore[assignment]
    graph: Any = None
    engine: Any = None

    def __post_init__(self) -> None:
        if self.settings.database_url:
            from agentos.db.relational import RelationalRepository

            self.store = RelationalRepository(self.settings.database_url)
        else:
            self.store = MemoryRepository()
        self.entity_store = EntityStore(self.store)
        from agentos.db.locks import LockManager

        self.locks = LockManager(redis_url=self.settings.redis_url,
                                 default_ttl=self.settings.lock_ttl_seconds)
        if self.settings.redis_url:
            self.kv = RedisKV(self.settings.redis_url)
            self.queue = RedisQueue(self.settings.redis_url)
        else:
            self.kv = MemoryKV()
            self.queue = MemoryQueue()
        self.workspace = Path(self.settings.workspace_dir).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

        self.agent_registry = AgentRegistry(self.entity_store)
        repo_root = Path(__file__).resolve().parents[1]
        self.skill_registry = SkillRegistry(self.entity_store, repo_root / "skills")
        self.tool_registry = ToolRegistry(self.entity_store)
        self.events = EventBus(self.entity_store)
        self.tracer = Tracer(self.entity_store, self.events)
        self.mcp_registry = McpRegistry(self.entity_store, event_bus=self.events,
                                        tracer=self.tracer)
        self.api_registry = ApiRegistry(self.entity_store)
        self.model_registry = ModelRegistry(self.entity_store)
        self.workflow_registry = WorkflowRegistry(self.entity_store, repo_root / "workflows")

        self.audit = AuditLog(self.entity_store)
        self.approvals = ApprovalService(self.entity_store)
        self.policy = PolicyEngine(
            self.entity_store,
            policies_path=self.settings.security_policies_file,
            environment=self.settings.environment,
            event_bus=self.events, audit=self.audit,
        )
        self.workspaces = WorkspaceManager(
            self.entity_store, worktrees_root=self.workspace / "worktrees")
        from agentos.workspaces.harness import LocalCodingHarness
        self.coding = LocalCodingHarness(self.workspaces)
        self.memory = MemoryStore(self.entity_store,
                                  fact_ttl_days=self.settings.memory_fact_ttl_days)
        from agentos.memory.scoping import ScopedMemoryStore
        self.scoped_memory = ScopedMemoryStore(self.memory, audit=self.audit)
        self.messages = MessageBus(self.entity_store)
        self.projects = ProjectService(self.entity_store)
        self.tasks = TaskService(self.entity_store, event_bus=self.events)
        self.prompts = PromptLibrary(self.settings)
        self.capabilities = CapabilityManager(self.tool_registry)
        self.performance = PerformanceTracker(self.entity_store, emit=self.events.publish)

        self.providers = build_providers(self.settings, {})

        async def _pick_downgrade(tier: str) -> Optional[str]:
            models = await self.model_registry.by_tier(tier)
            models.sort(key=lambda m: m.price_in_per_million + m.price_out_per_million)
            return models[0].id if models else None

        self.budgets = BudgetManager(self.settings, self.entity_store,
                                     emit=self.events.publish,
                                     downgrade_picker=_pick_downgrade)
        self.router = ModelRouter(self.settings, self.model_registry, self.budgets,
                                  self.providers, performance=self.performance)
        self.rollback = RollbackLedger(audit=self.audit, event_bus=self.events)
        self.rollback.register_compensation("compensate_filesystem_write",
                                            compensate_filesystem_write)
        self.executor = ToolExecutor(self.tool_registry, self.approvals, self.audit,
                                     require_approval_risk=self.settings.require_approval_risk,
                                     rollback=self.rollback,
                                     tracer=self.tracer, capabilities=self.capabilities,
                                     retry_transient=self.settings.tool_retry_transient)
        self.evaluation = BenchmarkRunner(self, use_judge=True,
                                          judge_tier=self.settings.eval_judge_tier)
        self.planner = DynamicPlanner()

    async def seed(self) -> dict[str, int]:
        """Populate default registries (idempotent)."""
        counts = {
            "agents": await self.agent_registry.seed_defaults(),
            "skills": await self.skill_registry.load_from_disk(),
            "tools": await self.tool_registry.seed_defaults(),
            "apis": await self.api_registry.seed_defaults(),
            "models": await self.model_registry.seed_defaults(),
            "workflows": await self.workflow_registry.load_from_disk(),
        }
        # refresh providers with the (seeded) model definitions
        model_defs = {m.id: m for m in await self.model_registry.list()}
        self.providers = build_providers(self.settings, model_defs)
        self.router.providers = self.providers
        return counts

    async def init_db(self) -> None:
        if hasattr(self.store, "init"):
            await self.store.init()

    async def close(self) -> None:
        if hasattr(self.store, "close"):
            await self.store.close()
        await self.locks.close()

    # -- runtime context for one agent run ---------------------------------
    @property
    def api_catalog(self) -> Any:
        return self.api_registry

    @property
    def mcp(self) -> Any:
        return self.mcp_registry

    @property
    def tools(self) -> Any:
        return self.tool_registry

    def runtime(self, agent: Any, task: Any, project: Any,
                approved_tools: Optional[set[str]] = None,
                spawn_depth: int = 0,
                workspace_path: Optional[Path] = None) -> AgentRuntime:
        workspace = (workspace_path
                     or self.workspace / (project.project_id if project else "default"))
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        ctx = RuntimeContext(
            agent=agent, task=task, project=project,
            workspace=workspace,
            services=self, executor=self.executor, router=self.router,
            prompts=self.prompts, skill_registry=self.skill_registry,
            agent_registry=self.agent_registry, event_bus=self.events,
            budget_manager=self.budgets, approved_tools=approved_tools or set(),
            spawn_depth=spawn_depth,
        )
        ctx.services = self  # expose the full bundle to handlers
        ctx.approved_tools = approved_tools or set()
        return AgentRuntime(ctx)