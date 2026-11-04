from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError, field_validator

from agentco.memory.scopes import TENANT_RE
from agentco.roles import Role

CODER_ROLES = (Role.SENIOR, Role.JUNIOR)
CHECKER_ROLES = (Role.REVIEWER, Role.QA, Role.LEAD)


class ConfigError(ValueError):
    pass


class RoleModel(BaseModel):
    model: str
    family: str


class Budgets(BaseModel):
    per_task_usd: float = 5
    max_rounds: int = 3


class TenantConfig(BaseModel):
    tenant: str
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    database_url: str = "sqlite:///.agentco/state.db"
    roles: dict[Role, RoleModel]
    budgets: Budgets = Budgets()

    @field_validator("tenant")
    @classmethod
    def _valid_tenant(cls, v: str) -> str:
        if not TENANT_RE.fullmatch(v):
            raise ValueError(f"tenant {v!r} must match {TENANT_RE.pattern}")
        return v


def load_config(path: Path) -> TenantConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text())
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
