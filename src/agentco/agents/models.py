from agentco.config import ConfigError, RoleModel, TenantConfig
from agentco.roles import Role


def role_model(cfg: TenantConfig, role: Role) -> RoleModel:
    try:
        return cfg.roles[role]
    except KeyError:
        raise ConfigError(f"no model for {role}") from None
