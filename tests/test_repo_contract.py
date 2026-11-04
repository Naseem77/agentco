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


def test_missing_file(tmp_path):
    with pytest.raises(RepoContractMissing):
        load_repo_contract(tmp_path)


def test_e2e_key_required_but_may_be_null(tmp_path):
    write_contract(tmp_path, without="e2e")
    with pytest.raises(RepoContractError, match="e2e"):
        load_repo_contract(tmp_path)
    write_contract(tmp_path, e2e=None)
    assert load_repo_contract(tmp_path).e2e is None


def test_sandbox_needs_exactly_one(tmp_path):
    write_contract(
        tmp_path, sandbox={"devcontainer": ".devcontainer", "compose": "c.yml"}
    )
    with pytest.raises(RepoContractError, match="sandbox"):
        load_repo_contract(tmp_path)
    write_contract(tmp_path, sandbox={})
    with pytest.raises(RepoContractError, match="sandbox"):
        load_repo_contract(tmp_path)


@pytest.mark.parametrize("key", ["build", "test", "lint"])
@pytest.mark.parametrize("value", ["", "   "])
def test_commands_must_be_non_empty(tmp_path, key, value):
    write_contract(tmp_path, **{key: value})
    with pytest.raises(RepoContractError, match=key):
        load_repo_contract(tmp_path)


def test_e2e_string_must_be_non_empty(tmp_path):
    write_contract(tmp_path, e2e=" ")
    with pytest.raises(RepoContractError, match="e2e"):
        load_repo_contract(tmp_path)


def test_unknown_keys_rejected(tmp_path):
    write_contract(tmp_path, lnt="ruff check")
    with pytest.raises(RepoContractError, match="lnt"):
        load_repo_contract(tmp_path)
    write_contract(tmp_path, sandbox={"devcontainer": ".d", "dockerfile": "x"})
    with pytest.raises(RepoContractError, match="dockerfile"):
        load_repo_contract(tmp_path)


def test_invalid_yaml(tmp_path):
    (tmp_path / ".agentco.yml").write_text("build: [unclosed")
    with pytest.raises(RepoContractError, match="YAML"):
        load_repo_contract(tmp_path)


def test_non_mapping(tmp_path):
    (tmp_path / ".agentco.yml").write_text("- a\n- b\n")
    with pytest.raises(RepoContractError, match="mapping"):
        load_repo_contract(tmp_path)


def test_non_utf8(tmp_path):
    (tmp_path / ".agentco.yml").write_bytes(b"build: \xff\xfe\n")
    with pytest.raises(RepoContractError, match="UTF-8"):
        load_repo_contract(tmp_path)
