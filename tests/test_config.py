from pathlib import Path

import pytest

from agentco.config import ConfigError, family_warnings, load_config
from agentco.roles import Role

EXAMPLE = """
tenant: acme
roles:
  pm:        { model: fast-cheap, family: anthropic }
  triage:    { model: fast-cheap, family: anthropic }
  architect: { model: strongest, family: anthropic }
  senior:    { model: claude-strong, family: anthropic }
  junior:    { model: claude-cheaper, family: anthropic }
  reviewer:  { model: gpt-reviewer, family: openai }
  qa:        { model: gpt-family, family: openai }
  lead:      { model: gemini-family, family: google }
  librarian: { model: fast-cheap, family: anthropic }
budgets:
  per_task_usd: 5
  max_rounds: 3
"""

EXAMPLE_WITHOUT_REVIEWER = EXAMPLE.replace(
    "  reviewer:  { model: gpt-reviewer, family: openai }\n", ""
)

EXAMPLE_QA_EQUALS_SENIOR = EXAMPLE.replace(
    "qa:        { model: gpt-family, family: openai }",
    "qa:        { model: claude-strong, family: openai }",
)

EXAMPLE_ALL_ANTHROPIC_DIFFERENT_MODELS = EXAMPLE.replace(
    "family: openai", "family: anthropic"
).replace("family: google", "family: anthropic")


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "tenant.yaml"
    path.write_text(text)
    return path


def test_loads_example_config(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE))
    assert cfg.tenant == "acme"
    assert cfg.roles[Role.QA].family == "openai"
    assert cfg.budgets.per_task_usd == 5 and cfg.budgets.max_rounds == 3
    assert cfg.falkordb_host == "localhost" and cfg.falkordb_port == 6379
    assert cfg.database_url == "sqlite:///.agentco/state.db"
    assert family_warnings(cfg) == []


def test_budgets_default_when_omitted(tmp_path):
    text = EXAMPLE.split("budgets:")[0]
    cfg = load_config(write(tmp_path, text))
    assert cfg.budgets.per_task_usd == 5 and cfg.budgets.max_rounds == 3


def test_missing_role_is_error(tmp_path):
    with pytest.raises(ConfigError, match="reviewer"):
        load_config(write(tmp_path, EXAMPLE_WITHOUT_REVIEWER))


def test_checker_same_model_as_coder_is_error(tmp_path):
    with pytest.raises(ConfigError, match="qa.*same model"):
        load_config(write(tmp_path, EXAMPLE_QA_EQUALS_SENIOR))


def test_same_family_is_warning_not_error(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE_ALL_ANTHROPIC_DIFFERENT_MODELS))
    warnings = family_warnings(cfg)
    assert any("qa" in w and "same family" in w for w in warnings)
    assert len(warnings) == 3  # one per checker: reviewer, qa, lead


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "tenant: [unclosed"))


def test_non_mapping_yaml_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "- just\n- a list\n"))


def test_invalid_schema_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, EXAMPLE.replace("tenant: acme\n", "")))


def test_config_rejects_bad_tenant(tmp_path):
    with pytest.raises(ConfigError, match="tenant"):
        load_config(write(tmp_path, EXAMPLE.replace("tenant: acme", "tenant: acme__x")))
