import inspect
import subprocess

import pytest

import agentco.github.client
import agentco.workflow.pr
from agentco.github.client import FakeGitHub, GhCli
from agentco.github.errors import GitHubError
from agentco.github.models import PullRef

URL = "https://github.com/acme/app/pull/12"


def recording_gh(output):
    seen = []

    def runner(argv, input=None):
        seen.append((argv, input))
        return output

    return GhCli(runner=runner), seen


def test_create_pull_argv_and_number():
    gh, seen = recording_gh(f"{URL}\n")
    ref = gh.create_pull("acme/app", "agentco/task-1", "main", "t", "body")
    assert ref == PullRef(number=12, url=URL)
    assert seen == [
        (
            [
                "gh", "pr", "create", "--repo", "acme/app", "--head", "agentco/task-1",
                "--base", "main", "--title", "t", "--body-file", "-",
            ],
            "body",
        )
    ]  # fmt: skip


def test_create_pull_keeps_hostile_strings_as_single_argv_items():
    gh, seen = recording_gh(URL)
    title = "x; rm -rf / $(id) `id`"
    gh.create_pull("acme/app", "feat/a b", "main", title, "b")
    argv = seen[0][0]
    assert argv[argv.index("--title") + 1] == title
    assert argv[argv.index("--head") + 1] == "feat/a b"
    assert "b" not in argv


def test_create_pull_takes_number_from_last_output_line():
    gh, _ = recording_gh(f"Creating pull request for x into main\n\n{URL}\n")
    assert gh.create_pull("acme/app", "h", "main", "t", "b").number == 12


def test_create_pull_rejects_output_without_url():
    gh, _ = recording_gh("nothing useful\n")
    with pytest.raises(GitHubError) as err:
        gh.create_pull("acme/app", "h", "main", "t", "b")
    assert str(err.value) == "gh pr create did not print a pull request URL"


def test_find_open_pull_parses_json():
    gh, seen = recording_gh(f'[{{"number": 12, "url": "{URL}"}}]')
    assert gh.find_open_pull("acme/app", "agentco/task-1") == PullRef(
        number=12, url=URL
    )
    assert seen == [
        (
            [
                "gh", "pr", "list", "--repo", "acme/app", "--head", "agentco/task-1",
                "--state", "open", "--json", "number,url",
            ],
            None,
        )
    ]  # fmt: skip


def test_find_open_pull_none_when_empty():
    gh, _ = recording_gh("[]")
    assert gh.find_open_pull("acme/app", "h") is None


@pytest.mark.parametrize("output", ["not json", '{"number": 1}', '[{"number": 1}]'])
def test_find_open_pull_malformed_is_github_error(output):
    gh, _ = recording_gh(output)
    with pytest.raises(GitHubError):
        gh.find_open_pull("acme/app", "h")


def test_edit_pull_argv_and_stdin():
    gh, seen = recording_gh("")
    gh.edit_pull("acme/app", 12, "new body")
    assert seen == [
        (
            ["gh", "pr", "edit", "12", "--repo", "acme/app", "--body-file", "-"],
            "new body",
        )
    ]


def test_invalid_repo_rejected_before_running():
    gh, seen = recording_gh(URL)
    with pytest.raises(ValueError):
        gh.create_pull("not a repo", "h", "main", "t", "b")
    assert seen == []


def test_one_argument_runner_still_serves_reads():
    seen = []
    gh = GhCli(runner=lambda argv: seen.append(argv) or "[[]]")
    assert gh.pulls("o/r") == []
    assert len(seen) == 1


def test_default_runner_sends_body_on_stdin(monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs["input"]))
        return subprocess.CompletedProcess(argv, 0, f"{URL}\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert GhCli().create_pull("acme/app", "h", "main", "t", "the body").number == 12
    assert seen[0][1] == "the body"


def test_default_runner_failure_is_github_error(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "denied\n"),
    )
    with pytest.raises(GitHubError):
        GhCli().edit_pull("acme/app", 1, "b")


def test_no_merge_anywhere():
    public = [n for n in dir(GhCli) + dir(FakeGitHub) if not n.startswith("_")]
    assert not any("merge" in n for n in public)
    src = inspect.getsource(agentco.github.client) + inspect.getsource(
        agentco.workflow.pr
    )
    assert '"merge"' not in src and "'merge'" not in src


def test_fake_records_and_finds_writes():
    gh = FakeGitHub([], [], [], {})
    assert gh.find_open_pull("acme/app", "h") is None
    ref = gh.create_pull("acme/app", "h", "main", "t", "b")
    assert ref == PullRef(number=1, url="https://github.com/acme/app/pull/1")
    assert gh.created == [
        {
            "repo": "acme/app",
            "head": "h",
            "base": "main",
            "title": "t",
            "body": "b",
            "number": 1,
            "url": "https://github.com/acme/app/pull/1",
        }
    ]
    assert gh.find_open_pull("acme/app", "h") == ref
    assert gh.find_open_pull("acme/app", "other") is None
    gh.edit_pull("acme/app", 1, "b2")
    assert gh.edited == [{"repo": "acme/app", "number": 1, "body": "b2"}]


def test_fake_fail_writes_raises_and_records_nothing():
    gh = FakeGitHub([], [], [], {}, fail_writes=True)
    with pytest.raises(GitHubError):
        gh.create_pull("acme/app", "h", "main", "t", "b")
    with pytest.raises(GitHubError):
        gh.edit_pull("acme/app", 1, "b")
    assert (gh.created, gh.edited) == ([], [])
