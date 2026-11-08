from pathlib import Path

import pytest
import yaml

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
    assert cfg.database_url == f"sqlite:///{tmp_path.resolve()}/.agentco/state.db"
    assert family_warnings(cfg) == []


BASE = yaml.safe_load(EXAMPLE)


def test_loop_defaults(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE))
    assert (cfg.price_per_request_usd, cfg.concurrency) == (0.04, 1)
    assert cfg.thresholds.known_min_facts == 1
    assert cfg.thresholds.lead_min_confidence == 0.7
    assert cfg.loop.model_dump() == {
        "max_changed_lines": 400,
        "fix_max_changed_lines": 150,
        "preflight_attempts": 2,
        "command_timeout_s": 900,
        "agent_max_turns": 40,
        "agent_timeout_s": 1200,
        "fast_lane": True,
        "fast_lane_max_changed_lines": 60,
    }


def test_loop_overrides(tmp_path):
    raw = BASE | {
        "price_per_request_usd": 0,
        "concurrency": 3,
        "thresholds": {"known_min_facts": 2, "lead_min_confidence": 1},
        "loop": {"fast_lane": False, "preflight_attempts": 5},
    }
    cfg = load_config(write(tmp_path, yaml.safe_dump(raw)))
    assert (cfg.price_per_request_usd, cfg.concurrency) == (0, 3)
    assert (cfg.thresholds.known_min_facts, cfg.thresholds.lead_min_confidence) == (
        2,
        1,
    )
    assert (cfg.loop.fast_lane, cfg.loop.preflight_attempts) == (False, 5)
    assert cfg.loop.max_changed_lines == 400


def test_intake_defaults(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE))
    assert cfg.intake.review_bots == ["copilot-pull-request-reviewer[bot]", "Copilot"]
    assert cfg.intake.failing_conclusions == [
        "failure",
        "timed_out",
        "action_required",
        "startup_failure",
    ]


def test_intake_override(tmp_path):
    raw = BASE | {"intake": {"review_bots": ["my-bot[bot]"]}}
    cfg = load_config(write(tmp_path, yaml.safe_dump(raw)))
    assert cfg.intake.review_bots == ["my-bot[bot]"]
    assert cfg.intake.failing_conclusions[0] == "failure"


@pytest.mark.parametrize(
    "extra",
    [
        {"intake": {"bogus": 1}},
        {"pool": {"size": -1}},
        {"pool": {"size": 9}},
        {"pool": {"setup_timeout_s": 0}},
        {"pool": {"bogus": 1}},
        {"concurrency": 0},
        {"price_per_request_usd": -1},
        {"loop": {"max_changed_lines": 0}},
        {"loop": {"command_timeout_s": 0}},
        {"loop": {"fast_lane_max_changed_lines": -1}},
        {"loop": {"bogus": 1}},
        {"thresholds": {"known_min_facts": 0}},
        {"thresholds": {"lead_min_confidence": 1.1}},
        {"thresholds": {"lead_min_confidence": -0.1}},
        {"thresholds": {"bogus": 1}},
        {"thresholds": {"root_cause_after": 0}},
    ],
)
def test_loop_invalid(tmp_path, extra):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, yaml.safe_dump(BASE | extra)))


def test_root_cause_after_default_and_validation(tmp_path):
    assert load_config(write(tmp_path, EXAMPLE)).thresholds.root_cause_after == 2
    raw = BASE | {"thresholds": {"root_cause_after": 3}}
    cfg = load_config(write(tmp_path, yaml.safe_dump(raw)))
    assert cfg.thresholds.root_cause_after == 3


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


def error_locs(exc_info) -> list[tuple]:
    return [e["loc"] for e in exc_info.value.__cause__.errors()]


def test_config_rejects_bad_tenant(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, EXAMPLE.replace("tenant: acme", "tenant: acme__x")))
    assert error_locs(exc) == [("tenant",)]


def test_tenant_length_boundary(tmp_path):
    ok = "a" * 31
    cfg = load_config(write(tmp_path, EXAMPLE.replace("tenant: acme", f"tenant: {ok}")))
    assert cfg.tenant == ok
    with pytest.raises(ConfigError) as exc:
        load_config(
            write(tmp_path, EXAMPLE.replace("tenant: acme", f"tenant: {'a' * 32}"))
        )
    assert error_locs(exc) == [("tenant",)]


@pytest.mark.parametrize("bad", ['"acme\\n"', "Acme"])
def test_tenant_rejects_trailing_newline_and_uppercase(tmp_path, bad):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, EXAMPLE.replace("tenant: acme", f"tenant: {bad}")))
    assert error_locs(exc) == [("tenant",)]


def test_unknown_top_level_key_is_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, EXAMPLE + "falkordb_hots: example\n"))
    assert error_locs(exc) == [("falkordb_hots",)]


def test_unknown_budget_key_is_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, EXAMPLE.replace("max_rounds", "max_round")))
    assert error_locs(exc) == [("budgets", "max_round")]


def test_unknown_role_model_key_is_error(tmp_path):
    text = EXAMPLE.replace(
        "pm:        { model: fast-cheap, family: anthropic }",
        "pm:        { model: fast-cheap, family: anthropic, famly: x }",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text))
    assert error_locs(exc) == [("roles", "pm", "famly")]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("per_task_usd", "0"),
        ("per_task_usd", "-1"),
        ("per_task_usd", ".nan"),
        ("per_task_usd", ".inf"),
        ("max_rounds", "0"),
        ("max_rounds", "-2"),
    ],
)
def test_budgets_must_be_positive_and_finite(tmp_path, field, value):
    text = EXAMPLE.split("budgets:")[0] + f"budgets:\n  {field}: {value}\n"
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text))
    assert error_locs(exc) == [("budgets", field)]


def test_non_utf8_config_is_config_error(tmp_path):
    path = tmp_path / "tenant.yaml"
    path.write_bytes(b"tenant: \xff\xfe\n")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert isinstance(exc.value.__cause__, UnicodeDecodeError)
    assert str(exc.value).startswith(f"config {path} is not valid UTF-8: ")


def test_relative_sqlite_url_resolves_against_config_dir(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE))
    assert cfg.database_url == f"sqlite:///{tmp_path.resolve()}/.agentco/state.db"


def test_relative_sqlite_url_independent_of_cwd(tmp_path, monkeypatch):
    path = write(tmp_path, EXAMPLE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = load_config(path)
    assert cfg.database_url == f"sqlite:///{tmp_path.resolve()}/.agentco/state.db"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:////var/lib/agentco/state.db",
        "sqlite://",
        "sqlite:///:memory:",
        "postgresql://user:pw@db.example/agentco",
    ],
)
def test_other_database_urls_unchanged(tmp_path, url):
    cfg = load_config(write(tmp_path, EXAMPLE + f'database_url: "{url}"\n'))
    assert cfg.database_url == url


def test_webhook_config_defaults(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE))
    assert cfg.webhook.secret_env == "AGENTCO_WEBHOOK_SECRET"
    assert cfg.webhook.max_body_bytes == 1_048_576
    assert cfg.webhook.debounce_s == 2.0


def test_webhook_override(tmp_path):
    raw = BASE | {"webhook": {"secret_env": "MY_SECRET", "debounce_s": 0}}
    cfg = load_config(write(tmp_path, yaml.safe_dump(raw)))
    assert (cfg.webhook.secret_env, cfg.webhook.debounce_s) == ("MY_SECRET", 0)


@pytest.mark.parametrize(
    "extra",
    [
        {"max_body_bytes": 0},
        {"max_body_bytes": 26_214_401},
        {"debounce_s": -1},
        {"secret": "inline"},
    ],
)
def test_webhook_invalid(tmp_path, extra):
    raw = BASE | {"webhook": extra}
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, yaml.safe_dump(raw)))


def test_pool_defaults_and_disable(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE))
    assert (cfg.pool.size, cfg.pool.setup_timeout_s) == (2, 1800)
    raw = BASE | {"pool": {"size": 0}}
    assert load_config(write(tmp_path, yaml.safe_dump(raw))).pool.size == 0


def test_memory_models_default(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE))
    models = cfg.memory_models
    assert (models.model, models.small_model, models.embedder) == (
        "gpt-5.5",
        "gpt-4.1-mini",
        "text-embedding-3-small",
    )
    raw = BASE | {"memory_models": {"small_model": "gpt-5-mini"}}
    overridden = load_config(write(tmp_path, yaml.safe_dump(raw))).memory_models
    assert (overridden.model, overridden.small_model) == ("gpt-5.5", "gpt-5-mini")


def test_memory_models_reject_unknown_key(tmp_path):
    raw = BASE | {"memory_models": {"nano": "gpt-4.1-nano"}}
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, yaml.safe_dump(raw)))
