from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

CONTRACT_FILE = ".agentco.yml"


class RepoContractError(ValueError):
    pass


class RepoContractMissing(FileNotFoundError):
    pass


class Sandbox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    devcontainer: str | None = None
    compose: str | None = None
    service: str | None = None  # compose service to run commands in

    @model_validator(mode="after")
    def _exactly_one(self) -> "Sandbox":
        if (self.devcontainer is None) == (self.compose is None):
            raise ValueError("sandbox must set exactly one of devcontainer or compose")
        if self.service is not None and self.compose is None:
            raise ValueError("sandbox service is only valid with compose")
        return self


class RepoContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build: str
    test: str
    e2e: str | None  # key required; null means the repo has no e2e tests
    lint: str
    sandbox: Sandbox
    test_files: str | None = None  # command template; must contain {files}
    env_passthrough: list[str] = []  # host variables the commands may see

    @field_validator("build", "test", "e2e", "lint")
    @classmethod
    def _non_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("command must be a non-empty string")
        return v

    @field_validator("test_files")
    @classmethod
    def _has_files_placeholder(cls, v: str | None) -> str | None:
        if v is not None and "{files}" not in v:
            raise ValueError("test_files must contain the {files} placeholder")
        return v


def load_repo_contract(repo_root: Path) -> RepoContract:
    path = Path(repo_root) / CONTRACT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise RepoContractMissing(f"repo contract {path} not found") from e
    except UnicodeDecodeError as e:
        raise RepoContractError(f"repo contract {path} is not valid UTF-8: {e}") from e
    except OSError as e:
        raise RepoContractError(f"cannot read repo contract {path}: {e}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RepoContractError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise RepoContractError(f"repo contract {path} must be a YAML mapping")
    try:
        return RepoContract.model_validate(raw)
    except ValidationError as e:
        raise RepoContractError(f"invalid repo contract {path}: {e}") from e
