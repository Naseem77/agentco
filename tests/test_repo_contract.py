import pytest
import yaml

from agentco.repo_contract import (
    RepoContractError,
    RepoContractMissing,
    load_repo_contract,
)


def write_contract(tmp_path, without=None, **overrides):
    data = {
        "build": "make build",
        "test": "make test",
        "e2e": "make e2e",
        "lint": "make lint",
        "sandbox": {"devcontainer": ".devcontainer"},
    }
    data.update(overrides)
    if without is not None:
        del data[without]
    (tmp_path / ".agentco.yml").write_text(yaml.safe_dump(data))


def contract_errors(tmp_path):
    """Load a bad contract; return pydantic's (loc, message) pairs.

    Asserting on these instead of str(exc) keeps tests independent of the
    tmp_path, which pytest derives from the test name and which the
    RepoContractError message embeds.
    """
    with pytest.raises(RepoContractError) as exc:
        load_repo_contract(tmp_path)
    return [(e["loc"], e["msg"]) for e in exc.value.__cause__.errors()]


def test_valid_contract(tmp_path):
    write_contract(tmp_path)
    c = load_repo_contract(tmp_path)
    assert c.build == "make build"
    assert c.e2e == "make e2e"
    assert c.sandbox.devcontainer == ".devcontainer"
    assert c.sandbox.compose is None


def test_compose_sandbox(tmp_path):
    write_contract(tmp_path, sandbox={"compose": "docker-compose.yml"})
    assert load_repo_contract(tmp_path).sandbox.compose == "docker-compose.yml"


def test_test_files_defaults_to_none_and_accepts_placeholder(tmp_path):
    write_contract(tmp_path)
    assert load_repo_contract(tmp_path).test_files is None
    write_contract(tmp_path, test_files="uv run pytest {files}")
    assert load_repo_contract(tmp_path).test_files == "uv run pytest {files}"


def test_test_files_needs_placeholder(tmp_path):
    write_contract(tmp_path, test_files="uv run pytest")
    errors = contract_errors(tmp_path)
    assert errors == [
        (
            ("test_files",),
            "Value error, test_files must contain the {files} placeholder",
        )
    ]


def test_sandbox_service_only_with_compose(tmp_path):
    write_contract(tmp_path, sandbox={"compose": "c.yml", "service": "app"})
    sandbox = load_repo_contract(tmp_path).sandbox
    assert (sandbox.compose, sandbox.service) == ("c.yml", "app")
    write_contract(tmp_path, sandbox={"compose": "c.yml"})
    assert load_repo_contract(tmp_path).sandbox.service is None
    write_contract(tmp_path, sandbox={"devcontainer": ".d", "service": "app"})
    assert [loc for loc, _ in contract_errors(tmp_path)] == [("sandbox",)]


def test_missing_file(tmp_path):
    with pytest.raises(RepoContractMissing):
        load_repo_contract(tmp_path)


def test_e2e_key_required_but_may_be_null(tmp_path):
    write_contract(tmp_path, without="e2e")
    assert [loc for loc, _ in contract_errors(tmp_path)] == [("e2e",)]
    write_contract(tmp_path, e2e=None)
    assert load_repo_contract(tmp_path).e2e is None


def test_sandbox_needs_exactly_one(tmp_path):
    write_contract(
        tmp_path, sandbox={"devcontainer": ".devcontainer", "compose": "c.yml"}
    )
    errors = contract_errors(tmp_path)
    assert [loc for loc, _ in errors] == [("sandbox",)]
    assert "exactly one" in errors[0][1]
    write_contract(tmp_path, sandbox={})
    errors = contract_errors(tmp_path)
    assert [loc for loc, _ in errors] == [("sandbox",)]
    assert "exactly one" in errors[0][1]


@pytest.mark.parametrize("key", ["build", "test", "lint"])
@pytest.mark.parametrize("value", ["", "   "])
def test_commands_must_be_non_empty(tmp_path, key, value):
    write_contract(tmp_path, **{key: value})
    errors = contract_errors(tmp_path)
    assert [loc for loc, _ in errors] == [(key,)]
    assert "non-empty" in errors[0][1]


def test_e2e_string_must_be_non_empty(tmp_path):
    write_contract(tmp_path, e2e=" ")
    errors = contract_errors(tmp_path)
    assert [loc for loc, _ in errors] == [("e2e",)]
    assert "non-empty" in errors[0][1]


def test_unknown_keys_rejected(tmp_path):
    write_contract(tmp_path, lnt="ruff check")
    assert [loc for loc, _ in contract_errors(tmp_path)] == [("lnt",)]
    write_contract(tmp_path, sandbox={"devcontainer": ".d", "dockerfile": "x"})
    assert [loc for loc, _ in contract_errors(tmp_path)] == [("sandbox", "dockerfile")]


def test_invalid_yaml(tmp_path):
    (tmp_path / ".agentco.yml").write_text("build: [unclosed")
    with pytest.raises(RepoContractError, match="YAML"):
        load_repo_contract(tmp_path)


def test_non_mapping(tmp_path):
    (tmp_path / ".agentco.yml").write_text("- a\n- b\n")
    with pytest.raises(RepoContractError, match="must be a YAML mapping"):
        load_repo_contract(tmp_path)


def test_non_utf8(tmp_path):
    (tmp_path / ".agentco.yml").write_bytes(b"build: \xff\xfe\n")
    with pytest.raises(RepoContractError, match="UTF-8"):
        load_repo_contract(tmp_path)


def test_contract_setup_optional(tmp_path):
    write_contract(tmp_path)
    assert load_repo_contract(tmp_path).setup is None
    write_contract(tmp_path, setup="uv sync")
    assert load_repo_contract(tmp_path).setup == "uv sync"
    write_contract(tmp_path, setup="  ")
    assert contract_errors(tmp_path)[0][0] == ("setup",)
