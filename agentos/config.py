"""Central configuration.

Every important knob is configurable through environment variables or a `.env`
file (see `.env.example`). Nothing here is a secret — secrets come from env vars
only and are never committed.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Identity -----------------------------------------------------------
    agent_os_name: str = "Agent OS"
    agent_os_org: str = "prem-ium.inc"
    environment: str = "development"  # development | production

    # --- Persistence --------------------------------------------------------
    database_url: Optional[str] = None  # postgresql+asyncpg://user:pass@host:5432/agentos
    redis_url: Optional[str] = None  # redis://localhost:6379/0
    workspace_dir: str = "./workspace"  # sandboxed file area for agents

    # --- Mattermost ---------------------------------------------------------
    mattermost_url: Optional[str] = None  # https://chat.example.com
    mattermost_token: Optional[str] = None  # personal access token of the bot user
    mattermost_team: str = "ai-agency"
    mattermost_channel: str = "town-square"
    mattermost_poll_interval: int = 10  # seconds
    mattermost_enabled: bool = True

    # --- Model providers ----------------------------------------------------
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"
    anthropic_api_key: Optional[str] = None
    anthropic_base_url: str = "https://api.anthropic.com"
    glm_api_key: Optional[str] = None
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    openai_compat_api_key: Optional[str] = None
    openai_compat_base_url: Optional[str] = None  # any OpenAI-compatible endpoint
    default_provider: str = "echo"  # echo | deepseek | anthropic | glm | openai_compat

    # --- Budgets (USD) ------------------------------------------------------
    global_budget_monthly: float = 100.0
    global_budget_daily: float = 20.0
    default_project_budget: float = 25.0
    auto_downgrade_on_budget: bool = True

    # --- Agent spawning limits ----------------------------------------------
    max_agent_depth: int = 4
    max_parallel_agents: int = 8
    max_task_retries: int = 3
    max_task_seconds: int = 1800
    default_max_subagents: int = 4

    # --- Security -----------------------------------------------------------
    require_approval_risk: str = "high"  # low | medium | high
    admin_user_ids: str = ""  # comma-separated Mattermost user ids with override power
    security_policies_file: str = "./config/policies.yaml"  # extra hard policies
    workspace_worktrees_dir: str = "./workspace/worktrees"  # isolated engineering workspaces
    workspace_isolation: bool = False  # coding stages run in isolated git worktrees
    api_auth_token: Optional[str] = None  # if set, /api/* requires Bearer <token>

    # --- Memory -------------------------------------------------------------
    memory_fact_ttl_days: int = 30  # consolidation: archive stale facts after this

    # --- Evaluation ---------------------------------------------------------
    eval_judge_tier: str = "t3"  # model tier used by the LLM-as-judge evaluator
    eval_sets_dir: str = "./eval_sets"
    perf_min_runs_for_influence: int = 3  # runs before stats shape routing

    # --- Adapters -----------------------------------------------------------
    github_token: Optional[str] = None  # read-only GitHub adapter
    postgres_query_url: Optional[str] = None  # read-only SELECT URL (asyncpg)
    browser_timeout_ms: int = 15000

    # --- Tools --------------------------------------------------------------
    tool_retry_transient: int = 2  # retries for transient tool failures

    # --- Model reliability ---------------------------------------------------
    model_circuit_threshold: int = 3  # consecutive failures before a model's circuit opens
    model_circuit_cooldown_seconds: float = 120.0  # how long an open circuit stays open

    # --- Concurrency (§34) ---------------------------------------------------
    lock_ttl_seconds: float = 120.0  # default distributed-lock TTL (task locks)

    # --- Operations ---------------------------------------------------------
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8300
    worker_poll_interval: int = 2  # seconds
    max_event_bus_workers: int = 4

    @property
    def admin_user_id_list(self) -> list[str]:
        return [u.strip() for u in self.admin_user_ids.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()