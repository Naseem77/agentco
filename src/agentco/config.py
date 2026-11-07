from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.engine import make_url

from agentco.memory.scopes import TENANT_RE
from agentco.roles import Role

CODER_ROLES = (Role.SENIOR, Role.JUNIOR)
CHECKER_ROLES = (Role.REVIEWER, Role.QA, Role.LEAD)


class ConfigError(ValueError):
    pass


class RoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    family: str


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_task_usd: float = Field(5, gt=0, allow_inf_nan=False)
    max_rounds: int = Field(3, gt=0)


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    known_min_facts: int = Field(1, ge=1)
    lead_min_confidence: float = Field(0.7, ge=0, le=1)
    stale_after: int = Field(3, ge=1)


class LoopLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_changed_lines: int = Field(400, gt=0)
    fix_max_changed_lines: int = Field(150, gt=0)
    preflight_attempts: int = Field(2, gt=0)
    command_timeout_s: float = Field(900, gt=0, allow_inf_nan=False)
    agent_max_turns: int = Field(40, gt=0)
    agent_timeout_s: float = Field(1200, gt=0, allow_inf_nan=False)
    fast_lane: bool = True
    fast_lane_max_changed_lines: int = Field(60, gt=0)


class IntakeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_bots: list[str] = [
        "copilot-pull-request-reviewer[bot]",
        "Copilot",
    ]
    failing_conclusions: list[str] = [
        "failure",
        "timed_out",
        "action_required",
        "startup_failure",
    ]


class WebhookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_env: str = "AGENTCO_WEBHOOK_SECRET"
    max_body_bytes: int = Field(1_048_576, gt=0, le=26_214_400)
    debounce_s: float = Field(2.0, ge=0, allow_inf_nan=False)


class TenantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    database_url: str = "sqlite:///.agentco/state.db"
    roles: dict[Role, RoleModel]
    budgets: Budgets = Budgets()
    price_per_request_usd: float = Field(0.04, ge=0, allow_inf_nan=False)
    concurrency: int = Field(1, ge=1)
    thresholds: Thresholds = Thresholds()
    loop: LoopLimits = LoopLimits()
    intake: IntakeConfig = IntakeConfig()
    webhook: WebhookConfig = WebhookConfig()

    @field_validator("tenant")
    @classmethod
    def _valid_tenant(cls, v: str) -> str:
        if not TENANT_RE.fullmatch(v):
            raise ValueError(f"tenant {v!r} must match {TENANT_RE.pattern}")
        return v


def _anchor_sqlite_url(url: str, base: Path) -> str:
    db = make_url(url)
    if db.get_backend_name() != "sqlite" or db.database in (None, "", ":memory:"):
        return url
    if Path(db.database).is_absolute():
        return url
    anchored = db.set(database=str(base / db.database))
    return anchored.render_as_string(hide_password=False)


def load_config(path: Path) -> TenantConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as e:
        raise ConfigError(f"config {path} is not valid UTF-8: {e}") from e
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config {path} must be a YAML mapping")
    try:
        cfg = TenantConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid config {path}: {e}") from e
    cfg.database_url = _anchor_sqlite_url(cfg.database_url, path.resolve().parent)

    missing = [r.value for r in Role if r is not Role.HUMAN and r not in cfg.roles]
    if missing:
        raise ConfigError(f"missing required roles: {', '.join(missing)}")

    for checker in CHECKER_ROLES:
        for coder in CODER_ROLES:
            if cfg.roles[checker].model == cfg.roles[coder].model:
                raise ConfigError(
                    f"{checker.value} uses the same model as {coder.value} "
                    f"({cfg.roles[checker].model!r}); a checker must differ from the coder"
                )
    return cfg


def family_warnings(cfg: TenantConfig) -> list[str]:
    coder_families = {cfg.roles[r].family for r in CODER_ROLES}
    return [
        f"{checker.value} is in the same family ({cfg.roles[checker].family!r}) "
        "as a coder role; prefer a different model family for checking"
        for checker in CHECKER_ROLES
        if cfg.roles[checker].family in coder_families
    ]
