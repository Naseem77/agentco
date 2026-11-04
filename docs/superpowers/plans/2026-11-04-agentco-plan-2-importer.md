# Agent Company — Plan 2: Importer + code structure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agentco index` parses a local checkout with tree-sitter into `{tenant}__rnd` (File / Class / Function, DEFINES / CALLS), and `agentco import` loads a repo's GitHub history (issues, PRs incl. human-authored, human review comments as ReviewRule episodes linked to the innermost function/class) into memory through the gate — resumable, idempotent, with a dry-run that shows the cost first.

**Architecture:** Three new packages. `agentco.github` reads GitHub through a `GitHub` protocol backed by `gh api` (fake in tests). `agentco.code` parses source into `FileStructure`s and stores them through a `CodeGraphBackend` seam (plain Cypher on FalkorDB in production, in-memory fake in unit tests), reached only via a role-checked `ScopedCodeGraph`. `agentco.importer` turns GitHub items into `MemoryItem` episodes, resolves each review comment's file+line to a symbol (at the comment's commit when a checkout is given), and writes as Librarian (`rnd`) / PM (`product`) with per-item `meta` markers.

**Tech Stack:** Python 3.12, uv, `gh` CLI (subprocess, JSON), `tree-sitter>=0.25` with `tree-sitter-python>=0.25`, `tree-sitter-javascript>=0.25`, `tree-sitter-typescript>=0.23`, `falkordb` async client (already installed via graphiti-core[falkordb]), Graphiti 0.30.x, typer, pytest.

**Spec:** `docs/superpowers/specs/2026-11-04-agent-company-design.md` (§4 roles, §5 "Learning from all PRs", §8 Memory + code structure, §9 learning loop, §14 "Bootstrapping memory") and `docs/superpowers/specs/2026-11-04-agentco-build-decisions.md` (D5, D6, D12).

## Global Constraints

Plan 1's constraints all still hold (graph names `{tenant}__{scope}`, tenant regex, no Docker/network in unit tests, commit trailer). In addition:

- GitHub is read only through the `gh` CLI (`gh api …`, GET only — query parameters go in the path, never `-f`, which switches gh to POST). Plan 2 never writes to GitHub.
- tree-sitter grammars come from the per-language wheels `tree-sitter-python`, `tree-sitter-javascript`, `tree-sitter-typescript`; **not** `tree-sitter-language-pack` (its 1.x releases download grammars over the network on first use, which breaks D12).
- Every code-structure node carries the label `Code` plus one of `File`, `Class`, `Function`, and the properties `repo` (`owner/name`) and `path` (repo-relative, `/`-separated). Plan 2's Cypher matches only `:Code` nodes — never Graphiti's `:Entity` nodes (Graphiti labels its extracted `File`/`Function` entities `:Entity:File` etc. in the same graph) — except for creating `(:Episodic)-[:ABOUT]->(:Code)` edges.
- Code symbols are keyed by `(repo, path, qualname)`; `qualname` joins enclosing class/function names with `.` (`GraphView.switch`). Line numbers are 1-based and inclusive everywhere (tree-sitter rows + 1; GitHub lines as given).
- Supported languages: `.py` → python; `.js .jsx .mjs .cjs` → javascript; `.ts` → typescript; `.tsx` → tsx. Anything else is not indexed.
- Import writes only `rnd` (as `Role.LIBRARIAN`) and `product` (as `Role.PM`); nothing to `qa` or `company`. The code graph is written only as `Role.LIBRARIAN` and is unreadable by roles without `rnd` read access (QA).
- Authors that are bots (`user.type == "Bot"` or login ending in `[bot]`) never produce episodes: AI reviewer findings are not the team's human standards (spec §9).
- Every import write sets its `meta` marker only after the write succeeds; marker keys are `import:{tenant}:{repo}:{kind}:{id}`.
- CLI exit codes: `1` config / bad argument / LLM client, `2` FalkorDB unreachable (existing `_falkordb_guard`), `3` GitHub (`gh`) error.

## Review Focus

- **A review reply newer than the cursor whose thread root is older** (root not fetched this run) → the reply is dropped as `orphan_reply`, never imported as a standalone ReviewRule. Test in Task 6.
- **Re-indexing a file after an edit** → `ABOUT` links from review episodes to functions that still exist survive; links to removed functions go with them; Graphiti `:Entity` nodes in the same graph are untouched. Tests in Task 3 (contract) and Task 4 (FalkorDB).
- **A review comment whose line has since moved or vanished on main** → resolved against the file at the comment's own commit (base commit for `LEFT`-side comments), not today's code. Test in Task 6.
- **`gh` missing, not logged in, or repo not found** → exit 3 with gh's own message; nothing written, no cursor stored. Tests in Task 1 and Task 8.
- **A run cut short by `--limit` (or a crash)** → cursor not advanced; the next run continues with the next-oldest items and writes no duplicates. Tests in Task 7.

## Not in this plan

Automatic re-index on merge / task branch (Plan 3 sandbox, Plan 4 intake call `index_repo`); ongoing PR-comment intake (Plan 4, D9); review *summary* bodies and PR conversation comments (no file + line to link — Plan 4); re-importing an issue/PR/comment after it was edited or closed (a marker means "imported once").

## File Structure

```
src/agentco/github/__init__.py
src/agentco/github/models.py        GhUser, GhIssue, GhPull, GhReviewComment, parse_*, linked_issues
src/agentco/github/client.py        GitHub protocol, GhCli, FakeGitHub, GitHubError, validate_repo
src/agentco/code/__init__.py
src/agentco/code/parse.py           language_for, Symbol, FileStructure, parse_source, innermost, content_hash
src/agentco/code/graph.py           CallEdge, CodeGraphBackend, InMemoryCodeGraph, ScopedCodeGraph, code_graph_for, resolve_calls
src/agentco/code/falkor_graph.py    FalkorCodeGraph, build_falkor_code_graph
src/agentco/code/indexer.py         IndexReport, tracked_files, index_repo, NotAGitRepo
src/agentco/importer/__init__.py
src/agentco/importer/episodes.py    module_of, ReviewThread, review_threads, issue/feature/pull/review items
src/agentco/importer/locate.py      git_show, SymbolLocator
src/agentco/importer/run.py         ImportOptions, ImportReport, run_import
src/agentco/cli.py                  + index, import commands; make_code_graph, make_github seams
tests/github/fixtures/*.json        gh api --paginate --slurp shaped payloads
tests/github/, tests/codegraph/, tests/importer/, tests/test_cli.py
```

---

### Task 1: GitHub read models + `gh` client

**Files:**
- Create: `src/agentco/github/__init__.py`, `src/agentco/github/models.py`, `src/agentco/github/client.py`
- Create: `tests/github/fixtures/issues.json`, `pulls.json`, `pull_comments.json`, `pull_files_3.json`
- Test: `tests/github/test_models.py`, `tests/github/test_client.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) GhUser`: `login: str`, `is_bot: bool`.
  - `@dataclass(frozen=True) GhIssue`: `number: int`, `title: str`, `body: str`, `author: GhUser`, `state: str`, `state_reason: str | None`, `labels: tuple[str, ...]`, `created_at: datetime`, `updated_at: datetime`, `closed_at: datetime | None`, `url: str`.
  - `@dataclass(frozen=True) GhPull`: `number: int`, `title: str`, `body: str`, `author: GhUser`, `state: str`, `merged_at: datetime | None`, `created_at: datetime`, `updated_at: datetime`, `base_sha: str`, `head_sha: str`, `labels: tuple[str, ...]`, `url: str`, `closes_issues: tuple[int, ...]`.
  - `@dataclass(frozen=True) GhReviewComment`: `id: int`, `pr_number: int`, `author: GhUser`, `association: str`, `body: str`, `path: str`, `line: int | None`, `original_line: int | None`, `side: Literal["LEFT", "RIGHT"]`, `subject_type: Literal["line", "file"]`, `commit_id: str`, `original_commit_id: str`, `diff_hunk: str`, `in_reply_to: int | None`, `created_at: datetime`, `updated_at: datetime`, `url: str`.
  - `parse_user(raw: dict | None) -> GhUser`; `parse_issue(raw: dict) -> GhIssue | None` (None when `raw` has a `pull_request` key); `parse_pull(raw: dict) -> GhPull`; `parse_review_comment(raw: dict) -> GhReviewComment`; `linked_issues(text: str) -> tuple[int, ...]`.
  - `GitHubError(RuntimeError)`; `validate_repo(repo: str) -> str` (raises `ValueError`); `REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")`.
  - `class GitHub(Protocol)`: `issues(repo: str, since: datetime | None = None) -> list[GhIssue]`; `pulls(repo: str) -> list[GhPull]`; `review_comments(repo: str, since: datetime | None = None) -> list[GhReviewComment]`; `pull_files(repo: str, number: int) -> list[str]`.
  - `Runner = Callable[[list[str]], str]`; `GhCli(runner: Runner | None = None)` implements `GitHub`.
  - `FakeGitHub(issues, pulls, comments, files: dict[int, list[str]])` implements `GitHub`, filters by `updated_at >= since` like the API, records every call in `calls: list[tuple[str, object]]` (e.g. `("issues", since)`, `("pull_files", 3)`); `FakeGitHub.from_fixtures(directory: Path) -> FakeGitHub`.

**Fixtures** (repo `acme-org/web`; each file is a list of pages, as `gh api --paginate --slurp` prints; `updated_at == created_at`). Later tasks rely on exactly these items:

| Item | Author (type, association) | Key fields | created_at |
|---|---|---|---|
| issue #1 "Graph switch flickers" | alice (User) | closed/completed, label `bug` | 2026-01-01 |
| issue #2 "Export graph as PNG" | bob (User) | open, label `enhancement` | 2026-01-06 |
| issue #3 | carol | has `pull_request` key (is PR #3) | 2026-01-03 |
| issue #4 (page 2) | dependabot[bot] (Bot) | open | 2026-01-02 |
| PR #3 "Debounce graph switch" | carol (User) | merged 2026-01-03, base `b1`, head `h1`, body `Fixes #1` | 2026-01-03 |
| PR #5 "Bump lodash" | dependabot[bot] (Bot) | open | 2026-01-02 |
| comment 101 on PR 3 | alice (User, MEMBER) | `src/app/graph.py`, original_line 6, line 6, RIGHT, original_commit `h1` | 2026-01-04 |
| comment 102 | carol (PR author) | in_reply_to 101, "done" | 2026-01-04 |
| comment 103 | copilot-pull-request-reviewer[bot] (Bot) | root | 2026-01-04 |
| comment 104 | carol (PR author, OWNER) | root | 2026-01-04 |
| comment 105 | stranger (User, NONE) | root | 2026-01-04 |
| comment 106 | alice (User, MEMBER) | `src/app/api.py`, subject_type `file`, line/original_line null | 2026-01-05 |
| comment 107 | alice | in_reply_to 999 (root not in payload) | 2026-01-05 |
| `pull_files_3.json` | — | `filename`: `src/app/graph.py`, `README.md` | — |

- [ ] **Step 1: Write the failing tests**

```python
def test_bot_detection():
    assert parse_user({"login": "dependabot[bot]", "type": "Bot"}).is_bot
    assert parse_user({"login": "renovate[bot]", "type": "User"}).is_bot
    assert parse_user(None) == GhUser("ghost", False)          # deleted account

def test_issue_endpoint_pull_requests_are_not_issues():
    pages = load("issues.json")
    parsed = [parse_issue(r) for page in pages for r in page]
    assert [i.number for i in parsed if i] == [1, 2, 4]

def test_null_body_and_dates():
    i = parse_issue(raw_issue(body=None, created_at="2026-01-01T10:00:00Z"))
    assert i.body == "" and i.created_at == datetime(2026, 1, 1, 10, tzinfo=UTC)

def test_review_comment_fields():
    c = parse_review_comment(raw_comment(101))
    assert (c.pr_number, c.path, c.original_line, c.side, c.subject_type) == (3, "src/app/graph.py", 6, "RIGHT", "line")
    assert parse_review_comment(raw_comment(106)).subject_type == "file"
    assert parse_review_comment(raw_comment(102)).in_reply_to == 101

def test_linked_issues():
    assert linked_issues("Fixes #1, closes #12 and resolved #7; see #9") == (1, 12, 7)

@pytest.mark.parametrize("bad", ["", "acme", "acme/web/x", "a b/c", "../x"])
def test_validate_repo(bad):
    with pytest.raises(ValueError):
        validate_repo(bad)
```

```python
def test_gh_cli_argv_and_page_flattening():
    seen = []
    def runner(argv): seen.append(argv); return FIXTURES["issues.json"]
    issues = GhCli(runner).issues("acme-org/web", since=datetime(2026, 1, 5, tzinfo=UTC))
    assert seen == [["gh", "api", "--paginate", "--slurp",
        "repos/acme-org/web/issues?state=all&per_page=100&sort=created&direction=asc&since=2026-01-05T00:00:00Z"]]
    assert [i.number for i in issues] == [1, 2, 4]

def test_gh_cli_other_paths():
    seen = []; cli = GhCli(lambda argv: seen.append(argv[-1]) or "[[]]")
    cli.pulls("o/r"); cli.review_comments("o/r"); cli.pull_files("o/r", 3)
    assert seen == ["repos/o/r/pulls?state=all&per_page=100&sort=created&direction=asc",
                    "repos/o/r/pulls/comments?per_page=100&sort=created&direction=asc",
                    "repos/o/r/pulls/3/files?per_page=100"]

def test_gh_missing_is_github_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", raising(FileNotFoundError("gh")))
    with pytest.raises(GitHubError, match="gh CLI not found"):
        GhCli().pulls("o/r")

def test_gh_failure_carries_stderr(monkeypatch):
    monkeypatch.setattr(subprocess, "run", completed(returncode=1, stderr="HTTP 404: Not Found\n"))
    with pytest.raises(GitHubError, match="HTTP 404: Not Found"):
        GhCli().pulls("o/missing")

def test_fake_filters_since_and_records_calls():
    gh = FakeGitHub.from_fixtures(FIXTURE_DIR)
    assert [i.number for i in gh.issues("acme-org/web", since=datetime(2026, 1, 5, tzinfo=UTC))] == [2]
    assert gh.calls[-1] == ("issues", datetime(2026, 1, 5, tzinfo=UTC))
```

- [ ] **Step 2: Run** `uv run pytest tests/github -v` — Expected: FAIL (import error).
- [ ] **Step 3: Implement `models.py`.** Dates via `datetime.fromisoformat` (handles `Z`). `body`/`title` `None` → `""`. Missing `side` → `"RIGHT"`, missing `subject_type` → `"line"`. `pr_number` from the trailing number of `pull_request_url`. `association` from `author_association`. `linked_issues`: regex `(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)`, order kept, duplicates removed.
- [ ] **Step 4: Implement `client.py`.** Default runner: `subprocess.run(argv, capture_output=True, text=True, check=False)`; `FileNotFoundError` → `GitHubError("gh CLI not found; install it from https://cli.github.com and run gh auth login")`; non-zero exit → `GitHubError(f"gh api failed: {stderr.strip()}")`. Output is a list of pages; flatten. `since` is rendered `%Y-%m-%dT%H:%M:%SZ` in UTC. `pull_files` returns `filename` values. Every method calls `validate_repo` first.
- [ ] **Step 5: Run** `uv run pytest tests/github -v` — PASS; `uv run ruff check src tests` — clean.
- [ ] **Step 6: Commit** `feat: read GitHub history through the gh CLI`

---

### Task 2: tree-sitter parsing (Python, JS, TS)

**Files:**
- Modify: `pyproject.toml` (`uv add "tree-sitter>=0.25" "tree-sitter-python>=0.25" "tree-sitter-javascript>=0.25" "tree-sitter-typescript>=0.23"`)
- Create: `src/agentco/code/__init__.py`, `src/agentco/code/parse.py`
- Test: `tests/codegraph/test_parse.py`

**Interfaces:**
- Produces:
  - `LANGUAGES: Mapping[str, str]` (extension → language, per Global Constraints); `language_for(path: str) -> str | None`.
  - `@dataclass(frozen=True) Symbol`: `kind: Literal["Class", "Function"]`, `name: str`, `qualname: str`, `start_line: int`, `end_line: int`, `calls: tuple[str, ...] = ()`.
  - `@dataclass(frozen=True) FileStructure`: `path: str`, `language: str`, `hash: str`, `symbols: tuple[Symbol, ...]`.
  - `content_hash(data: bytes) -> str` (sha256 hex).
  - `parse_source(path: str, data: bytes) -> FileStructure | None` (None for unsupported extensions).
  - `innermost(symbols: Iterable[Symbol], line: int) -> Symbol | None`.

- [ ] **Step 1: Write the failing tests**

```python
PY = b'''import x
@deco
def top(a):
    helper(a)
    return self.obj.method(a)

class GraphView(Base):
    def switch(self, g):
        def inner():
            pass
        self.load(g)
    async def load(self, g): ...
'''

def test_python_symbols():
    fs = parse_source("src/app/view.py", PY)
    assert [(s.kind, s.qualname, s.start_line, s.end_line) for s in fs.symbols] == [
        ("Function", "top", 2, 5),                        # decorated: starts at the decorator
        ("Class", "GraphView", 7, 12),
        ("Function", "GraphView.switch", 8, 11),
        ("Function", "GraphView.switch.inner", 9, 10),
        ("Function", "GraphView.load", 12, 12),
    ]
    by = {s.qualname: s for s in fs.symbols}
    assert by["top"].calls == ("helper", "method")
    assert by["GraphView.switch"].calls == ("load",)      # inner's calls are inner's own
    assert fs.language == "python" and fs.hash == content_hash(PY)

JS = b'''function top(a) { helper(a); return obj.method(a); }
class GraphView extends Base {
  switch(g) { this.load(g); }
  async load(g) {}
}
const arrow = (x) => { foo(x) };
export default function () {}
'''

def test_javascript_symbols():
    fs = parse_source("web/view.js", JS)
    assert [s.qualname for s in fs.symbols] == ["top", "GraphView", "GraphView.switch", "GraphView.load", "arrow"]
    assert {s.qualname: s.calls for s in fs.symbols}["arrow"] == ("foo",)

def test_typescript_and_tsx():
    ts = parse_source("a.ts", b"abstract class A { g(): number { return h(); } }\nexport function k<T>(x: T): T { return x }")
    assert [s.qualname for s in ts.symbols] == ["A", "A.g", "k"]
    tsx = parse_source("App.tsx", b"export function App(){ return <div onClick={() => go()}/> }")
    assert [(s.qualname, s.calls) for s in tsx.symbols] == [("App", ("go",))]   # anonymous arrow → enclosing named fn

def test_unsupported_extension():
    assert parse_source("README.md", b"# hi") is None

def test_broken_source_still_yields_symbols():
    fs = parse_source("x.py", b"def broken(:\n    x(\nclass Ok:\n    def m(self): pass\n")
    assert "Ok.m" in [s.qualname for s in fs.symbols]

def test_innermost():
    syms = parse_source("src/app/view.py", PY).symbols
    assert innermost(syms, 10).qualname == "GraphView.switch.inner"
    assert innermost(syms, 11).qualname == "GraphView.switch"
    assert innermost(syms, 8).qualname == "GraphView.switch"      # boundary inclusive
    assert innermost(syms, 6) is None
```

- [ ] **Step 2: Run** `uv run pytest tests/codegraph/test_parse.py -v` — FAIL.
- [ ] **Step 3: Implement `parse.py`.** Build parsers once per language: `Parser(Language(tree_sitter_python.language()))`, `tree_sitter_javascript.language()`, `tree_sitter_typescript.language_typescript()` / `language_tsx()`. Walk the tree keeping a stack of enclosing named symbols. Node kinds (name from field `name`):
  - python — class: `class_definition`; function: `function_definition` (if the parent is `decorated_definition`, `start_line` is the parent's); call: `call`, callee from field `function` (`identifier` → its text, `attribute` → field `attribute`).
  - javascript / typescript / tsx — class: `class_declaration`, `abstract_class_declaration`; function: `function_declaration`, `generator_function_declaration`, `method_definition`, and `variable_declarator` whose `value` is `arrow_function` / `function_expression` (name and span from the declarator); call: `call_expression`, callee from field `function` (`identifier` → text, `member_expression` → field `property`).
  - Anonymous functions are not symbols; their calls belong to the nearest named enclosing function. Calls are de-duplicated, first-seen order. Calls outside any function are dropped. A repeated `qualname` in one file: first wins.
  - `innermost`: among symbols with `start_line <= line <= end_line`, the one with the greatest `start_line` (ties → smallest span).
- [ ] **Step 4: Run** the tests — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat: tree-sitter code structure for Python and JS/TS`

---

### Task 3: Code graph seam, in-memory fake, role gate, call resolution

**Files:**
- Create: `src/agentco/code/graph.py`
- Create: `tests/codegraph/code_graph_contract.py` (shared by Task 4's integration test)
- Test: `tests/codegraph/test_graph.py`

**Interfaces:**
- Consumes: `Symbol`, `FileStructure`, `innermost` (Task 2); `ROLE_ACCESS`, `Scope`, `graph_name` (Plan 1 `memory/scopes.py`); `AccessDenied` (Plan 1 `memory/gate.py`); `Role`.
- Produces:
  - `@dataclass(frozen=True, order=True) CallEdge`: `caller_path: str`, `caller: str`, `callee_path: str`, `callee: str` (qualnames).
  - `class CodeGraphBackend(Protocol)` — every method async, first argument `graph: str`, second `repo: str` (except `ensure_schema(graph)`):
    `ensure_schema(graph) -> None`; `file_hashes(graph, repo) -> dict[str, str]`; `upsert_file(graph, repo, fs: FileStructure) -> None`; `delete_file(graph, repo, path: str) -> None`; `symbols_in(graph, repo, path: str) -> list[Symbol]`; `all_symbols(graph, repo) -> dict[str, list[Symbol]]`; `replace_calls(graph, repo, edges: list[CallEdge]) -> None`; `call_edges(graph, repo) -> list[CallEdge]`; `link_episode(graph, repo, episode: str, path: str, qualname: str) -> bool` (False when the symbol is not in the graph).
  - `InMemoryCodeGraph` implementing it, plus `links(graph: str) -> set[tuple[str, str, str]]` (`(episode, path, qualname)`) and `upserts: int` (call counter).
  - `ScopedCodeGraph` — same methods minus `graph` (bound to `graph_name(tenant, Scope.RND)`), plus `async enclosing(repo, path, line) -> Symbol | None`; read methods (`file_hashes`, `symbols_in`, `all_symbols`, `call_edges`, `enclosing`) need `Scope.RND` in the role's read set, the rest need it in the write set, else `AccessDenied` naming role and `rnd`.
  - `code_graph_for(tenant: str, role: Role, backend: CodeGraphBackend) -> ScopedCodeGraph`.
  - `resolve_calls(symbols_by_path: Mapping[str, Sequence[Symbol]]) -> list[CallEdge]` (sorted, unique).
  - `async check_contract(backend, graph: str, add_episode: Callable[[str], Awaitable[object]], count_links: Callable[[], Awaitable[int]] | None = None) -> None` in the test helper module.

- [ ] **Step 1: Write the failing tests**

```python
async def test_qa_cannot_read_code_graph():
    with pytest.raises(AccessDenied, match="qa.*rnd"):
        await code_graph_for("acme", Role.QA, InMemoryCodeGraph()).symbols_in("o/r", "a.py")

async def test_junior_reads_but_cannot_write():
    g = code_graph_for("acme", Role.JUNIOR, InMemoryCodeGraph())
    assert await g.symbols_in("o/r", "a.py") == []
    with pytest.raises(AccessDenied):
        await g.upsert_file("o/r", fs("a.py", "def f(): pass"))

async def test_tenants_and_repos_never_mix():
    b = InMemoryCodeGraph()
    await code_graph_for("acme", Role.LIBRARIAN, b).upsert_file("o/r", fs("a.py", "def f(): pass"))
    assert await code_graph_for("globex", Role.LEAD, b).symbols_in("o/r", "a.py") == []
    assert await code_graph_for("acme", Role.LEAD, b).symbols_in("o/other", "a.py") == []

def test_resolve_calls_prefers_same_file_then_unique_repo_wide():
    edges = resolve_calls({
        "a.py": syms(("run", ["load", "save", "missing"]), ("load", [])),
        "b.py": syms(("load", []), ("save", [])),
        "c.py": syms(("go", ["dup"])), "d.py": syms(("dup", [])), "e.py": syms(("dup", [])),
    })
    assert edges == [CallEdge("a.py", "run", "a.py", "load"), CallEdge("a.py", "run", "b.py", "save")]
    # "missing" unknown, "dup" ambiguous across files → no edge

async def noop(name: str) -> None:   # the fake links by name; no Episodic node needed
    return None

async def test_in_memory_graph_honours_contract():
    await check_contract(InMemoryCodeGraph(), "acme__rnd", add_episode=noop)
```

`check_contract` (in `code_graph_contract.py`) asserts, in order: after `ensure_schema` + `upsert_file(v1)` of `src/app/graph.py` (`GraphView`, `GraphView.load`, `GraphView.switch`), `file_hashes == {path: v1.hash}` and `symbols_in` round-trips kind/qualname/lines/calls; `add_episode("review:o/r:101")` then `link_episode(..., "GraphView.switch") is True` and linking an unknown qualname is `False`; `upsert_file(v2)` where `GraphView.load` is removed and `GraphView.switch` moved → `symbols_in` reflects v2, the switch link survives, nothing for `load` remains; `replace_calls([e1])` then `replace_calls([e2])` → `call_edges == [e2]`; `delete_file` → `file_hashes == {}` and `symbols_in == []`. "Link survives" is checked through `count_links` (Task 4 passes a Cypher count of `ABOUT` edges); when it is None, `len(backend.links(graph))`.

- [ ] **Step 2: Run** `uv run pytest tests/codegraph/test_graph.py -v` — FAIL.
- [ ] **Step 3: Implement `graph.py`.** `upsert_file` replaces the file's symbols but keeps links whose `(path, qualname)` is still present; `delete_file` drops the file's symbols, links and call edges touching it. `resolve_calls`: a call name matches a Function whose last qualname segment equals it; exactly one match in the caller's file wins; otherwise exactly one match repo-wide; otherwise no edge (recursion allowed). `enclosing` = `innermost(await symbols_in(...), line)`.
- [ ] **Step 4: Run** — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat: code graph seam with role gate and call resolution`

---

### Task 4: FalkorDB code graph writer (plain Cypher)

**Files:**
- Create: `src/agentco/code/falkor_graph.py`
- Modify: `tests/conftest.py` (add `falkor_cfg` fixture: like `cfg` but needs only `FALKORDB_HOST`)
- Test: `tests/codegraph/test_falkor_graph_integration.py`

**Interfaces:**
- Consumes: `CodeGraphBackend`, `CallEdge` (Task 3); `Symbol`, `FileStructure` (Task 2); `TenantConfig`.
- Produces: `FalkorCodeGraph(db: falkordb.asyncio.FalkorDB)` implementing `CodeGraphBackend`, client exposed as attribute `db`; `build_falkor_code_graph(cfg: TenantConfig) -> FalkorCodeGraph` (`FalkorDB(host=cfg.falkordb_host, port=cfg.falkordb_port)`).

Graph schema (Plan 3's triage and pre-flight query this; keep it exact):

| Node / edge | Properties |
|---|---|
| `(:Code:File)` | `repo`, `path`, `name` (basename), `language`, `hash` |
| `(:Code:Class)` / `(:Code:Function)` | `repo`, `path`, `qualname`, `name`, `kind`, `start_line`, `end_line`; Function also `calls` (list of names) |
| `(:Code:File)-[:DEFINES]->(:Code)` | top-level symbols |
| `(:Code)-[:DEFINES]->(:Code)` | parent symbol (qualname prefix) → nested symbol / method |
| `(:Code:Function)-[:CALLS]->(:Code:Function)` | from `replace_calls` |
| `(:Episodic)-[:ABOUT]->(:Code)` | from `link_episode`; `Episodic` matched by `name` (Graphiti stores the episode name there) |

- [ ] **Step 1: Write the failing integration test**

```python
pytestmark = pytest.mark.integration

async def test_falkor_code_graph_honours_contract(falkor_cfg):
    backend = build_falkor_code_graph(falkor_cfg)
    graph = f"{falkor_cfg.tenant}__rnd"
    db_graph = backend.db.select_graph(graph)
    try:
        await check_contract(backend, graph,
            add_episode=lambda name: db_graph.query("CREATE (:Episodic {name: $n})", {"n": name}),
            count_links=lambda: count(db_graph, "MATCH (:Episodic)-[r:ABOUT]->(:Code) RETURN count(r)"))
    finally:
        await db_graph.delete()

async def test_graphiti_entities_are_never_touched(falkor_cfg):
    # an (:Entity:File {path}) / (:Entity:Function) node with the same repo/path survives upsert_file + delete_file
```

- [ ] **Step 2: Run** `uv run pytest -m integration tests/codegraph -v` without `FALKORDB_HOST` — SKIPPED; unit suite `uv run pytest -q` still PASS.
- [ ] **Step 3: Implement `falkor_graph.py`.** `ensure_schema` creates `CREATE INDEX FOR (n:Code) ON (n.path)`, the same on `n.repo`, and `CREATE INDEX FOR (e:Episodic) ON (e.name)`, ignoring errors containing `already indexed` (as Graphiti's `FalkorDriver` does). Graph handles come from `self.db.select_graph(graph)`. `upsert_file` (one graph, several queries): `MERGE` the `:Code:File` node and set its props; `DETACH DELETE` the file's `:Code` symbols whose `kind + ":" + qualname` is not in the new set; `UNWIND`-`MERGE` Classes and Functions keyed by `(repo, path, qualname)` (labels can't be parameters — one `UNWIND` per kind) and `SET` their props; delete and recreate the file's `DEFINES` edges. Never `DETACH DELETE` a symbol that still exists (that is what keeps `ABOUT` edges). `replace_calls` deletes all `CALLS` among the repo's `:Code:Function` nodes, then `UNWIND`s the new edges. Every `MATCH` on code nodes includes the `:Code` label and `repo`. Expose the client as `.db` for tests.
- [ ] **Step 4: Run** `docker compose up -d falkordb` then `FALKORDB_HOST=localhost uv run pytest -m integration tests/codegraph -v` — PASS.
- [ ] **Step 5: Commit** `feat: FalkorDB code graph writer`

---

### Task 5: Incremental indexer + `agentco index`

**Files:**
- Create: `src/agentco/code/indexer.py`
- Modify: `src/agentco/cli.py` (`make_code_graph` seam, `index` command)
- Test: `tests/codegraph/test_indexer.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `parse_source`, `language_for`, `content_hash` (Task 2); `ScopedCodeGraph`, `code_graph_for`, `resolve_calls` (Task 3); `build_falkor_code_graph` (Task 4); `validate_repo` (Task 1); CLI `_load`, `_falkordb_guard`.
- Produces:
  - `MAX_FILE_BYTES = 1_000_000`; `NotAGitRepo(ValueError)`.
  - `tracked_files(root: Path) -> list[str]` — `git -C root ls-files -z`, `/`-separated repo-relative paths.
  - `@dataclass(frozen=True) IndexReport`: `indexed: int`, `unchanged: int`, `deleted: int`, `skipped: int`, `calls: int`.
  - `async index_repo(repo: str, root: Path, code: ScopedCodeGraph) -> IndexReport`.
  - `cli.make_code_graph: Callable[[TenantConfig], CodeGraphBackend] = build_falkor_code_graph`.
  - `agentco index --repo OWNER/NAME --repo-path PATH --config PATH` → prints `indexed {n}, unchanged {n}, deleted {n}, skipped {n}, calls {n}`; runs as `Role.LIBRARIAN`.

- [ ] **Step 1: Write the failing tests** (`git_repo(tmp_path, files)` helper: `git init -q`, write files, `git add -A`; no commit needed for `ls-files`)

```python
async def test_first_index_then_noop(tmp_path):
    root = git_repo(tmp_path, {"a.py": "def f():\n    g()\n", "b.ts": "export function g() {}", "README.md": "x",
                               ".gitignore": "node_modules/\n", "node_modules/x.js": "function z(){}"})
    backend = InMemoryCodeGraph(); code = code_graph_for("acme", Role.LIBRARIAN, backend)
    assert await index_repo("o/r", root, code) == IndexReport(indexed=2, unchanged=0, deleted=0, skipped=0, calls=1)
    upserts = backend.upserts
    assert await index_repo("o/r", root, code) == IndexReport(indexed=0, unchanged=2, deleted=0, skipped=0, calls=0)
    assert backend.upserts == upserts

async def test_only_changed_file_reindexed_and_links_survive(tmp_path): ...
    # link episode to a.py:f, edit a.py adding h(); re-index → indexed=1, unchanged=1; link to f still present

async def test_removed_file_deleted(tmp_path): ...       # git rm b.ts → deleted=1, calls=0 edges remain
async def test_untracked_files_ignored(tmp_path): ...    # new.py on disk, not added → not in file_hashes
async def test_large_file_skipped(tmp_path): ...         # a .py of MAX_FILE_BYTES + 1 → skipped=1
async def test_not_a_git_repo(tmp_path):
    with pytest.raises(NotAGitRepo):
        await index_repo("o/r", tmp_path, code_graph_for("acme", Role.LIBRARIAN, InMemoryCodeGraph()))
```

```python
def test_index_cli(env, tmp_path):                       # env fixture also patches cli.make_code_graph → shared InMemoryCodeGraph
    root = git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    r = env("index", "--repo", "o/r", "--repo-path", str(root))
    assert r.exit_code == 0 and r.stdout.splitlines()[-1] == "indexed 1, unchanged 0, deleted 0, skipped 0, calls 0"

def test_index_cli_not_a_repo_exits_1(env, tmp_path): ...   # message names the path
def test_index_cli_bad_repo_name_exits_1(env, tmp_path): ...
def test_index_cli_falkordb_down_exits_2(tmp_path, monkeypatch): ...   # make_code_graph raising redis ConnectionError
```

- [ ] **Step 2: Run** `uv run pytest tests/codegraph/test_indexer.py tests/test_cli.py -v` — FAIL.
- [ ] **Step 3: Implement `index_repo`.** `ensure_schema`; read `file_hashes`; for each tracked path with a language: not a regular file or larger than `MAX_FILE_BYTES` → `skipped`; same hash → `unchanged`; else parse + `upsert_file` → `indexed`. Paths in the graph but no longer tracked-and-supported → `delete_file`. If anything was indexed or deleted: `replace_calls(resolve_calls(await all_symbols(repo)))` and `calls = len(edges)`; otherwise `calls = 0`. The working tree is indexed (uncommitted edits included).
- [ ] **Step 4: Implement the CLI command** (`asyncio.run` inside `_falkordb_guard`; `NotAGitRepo` / `ValueError` from `validate_repo` → message + exit 1).
- [ ] **Step 5: Run** the tests — PASS; full `uv run pytest -q`; ruff clean.
- [ ] **Step 6: Commit** `feat: incremental code indexer and agentco index`

---

### Task 6: Episodes, review threads, comment → symbol locator

**Files:**
- Create: `src/agentco/importer/__init__.py`, `src/agentco/importer/episodes.py`, `src/agentco/importer/locate.py`
- Create: `tests/importer/helpers.py` — `REPO = "acme-org/web"`, `FIXTURES` (path to `tests/github/fixtures`), `GRAPH_AT_H1`, `pr3()`, `comment_101()`, `thread_101()`, `thread_106()` built from the Task 1 fixtures; Task 7 reuses it
- Test: `tests/importer/test_episodes.py`, `tests/importer/test_locate.py`

**Interfaces:**
- Consumes: `GhIssue`, `GhPull`, `GhReviewComment`, `FakeGitHub` + fixtures (Task 1); `Symbol`, `parse_source`, `innermost` (Task 2); `ScopedCodeGraph` (Task 3); `MemoryItem` (Plan 1).
- Produces:
  - `TEAM_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})`; `FEATURE_LABELS = frozenset({"enhancement", "feature", "feature request", "feature-request"})` (compared lower-case); `TEXT_LIMIT = 2000`; `HUNK_LINES = 15`.
  - `module_of(path: str) -> str` — parent directory, `"."` for root files.
  - `@dataclass(frozen=True) ReviewThread`: `root: GhReviewComment`, `replies: tuple[GhReviewComment, ...]`.
  - `@dataclass(frozen=True) ThreadSelection`: `threads: list[ReviewThread]`, `skipped: Counter[str]` (reasons `bot`, `pr_author`, `not_team`, `orphan_reply`, `unknown_pr`).
  - `review_threads(comments: Iterable[GhReviewComment], pulls: Mapping[int, GhPull]) -> ThreadSelection`.
  - `is_feature(issue: GhIssue) -> bool`.
  - `issue_item(repo, issue) -> MemoryItem` (name `issue:{repo}#{n}`); `feature_item(repo, issue) -> MemoryItem` (`feature:{repo}#{n}`); `pull_item(repo, pr, files: list[str]) -> MemoryItem` (`pr:{repo}#{n}`); `review_item(repo, thread, pr, symbol: Symbol | None) -> MemoryItem` (`review:{repo}:{root.id}`). `reference_time` = the item's `created_at`.
  - `GitShow = Callable[[Path, str, str], bytes | None]`; `git_show(checkout: Path, commit: str, path: str) -> bytes | None` (`git -C checkout show {commit}:{path}`; non-zero → None).
  - `SymbolLocator(repo: str, code: ScopedCodeGraph | None, checkout: Path | None, show: GitShow = git_show)`; `async locate(comment: GhReviewComment, pr: GhPull) -> Symbol | None`.

Episode copy (Graphiti extracts from the body only, so the first line says what it is — Plan 1 seed lesson). First lines, exactly:
- issue: `GitHub issue #{n} in {repo}: {title} (state: {state}[, {state_reason}]; labels: {a, b | none}; opened by @{login}).`
- feature: `Feature request from GitHub issue #{n} in {repo}: {title} (state: {state}; labels: {…}).`
- PR: `Pull request #{n} in {repo} by @{login}: {title} ({merged YYYY-MM-DD | closed without merging | open}[; closes issues #1, #12]).` then `Modules touched: …` and `Files: …` (first 50, then `(+N more)`).
- review: `Review rule from human reviewer @{login} ({association}) on pull request #{pr} in {repo} (origin=human, module_path={module_of(path)}).` then `Code: {path}:{line} in {function|class} {qualname}` / `Code: {path}:{line}` (no symbol) / `Code: {path} (file-level)`, then `Comment: {body}`, then `Discussion:` with one `@{login}: {text}` line per reply (max 5, 500 chars each) when there are replies, then `Diff hunk (last 15 lines):` and the hunk tail.
- Bodies / comments longer than `TEXT_LIMIT` are cut and end with ` …[truncated]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_thread_selection_on_fixtures():
    gh = FakeGitHub.from_fixtures(FIXTURES); pulls = {p.number: p for p in gh.pulls(REPO)}
    sel = review_threads(gh.review_comments(REPO), pulls)
    assert [t.root.id for t in sel.threads] == [101, 106]
    assert [r.id for r in sel.threads[0].replies] == [102]
    assert sel.skipped == Counter(bot=1, pr_author=1, not_team=1, orphan_reply=1)

def test_review_item_names_the_function():
    t = thread_101(); item = review_item(REPO, t, pr3(), Symbol("Function", "switch", "GraphView.switch", 5, 6))
    lines = item.body.splitlines()
    assert item.name == "review:acme-org/web:101"
    assert lines[0] == ("Review rule from human reviewer @alice (MEMBER) on pull request #3 in acme-org/web "
                        "(origin=human, module_path=src/app).")
    assert lines[1] == "Code: src/app/graph.py:6 in function GraphView.switch"
    assert "@carol: done" in item.body and item.reference_time == t.root.created_at

def test_file_level_review_item():
    assert review_item(REPO, thread_106(), pr3(), None).body.splitlines()[1] == "Code: src/app/api.py (file-level)"

def test_pull_item_lists_modules_and_linked_issue():
    item = pull_item(REPO, pr3(), ["src/app/graph.py", "README.md"])
    assert item.body.splitlines()[0] == "Pull request #3 in acme-org/web by @carol: Debounce graph switch (merged 2026-01-03; closes issues #1)."
    assert "Modules touched: src/app, ." in item.body

def test_issue_and_feature_items(): ...     # first lines exactly as above for #1 and #2; is_feature(#2) and not #1
def test_long_text_truncated(): ...         # 5000-char body → "…[truncated]" in body, len(body) < 3000
def test_module_of():
    assert (module_of("src/app/graph.py"), module_of("setup.py")) == ("src/app", ".")
```

```python
GRAPH_AT_H1 = b"class GraphView:\n    def load(self, g):\n        return g\n\n    def switch(self, g):\n        self.load(g)\n"   # in helpers.py

async def test_locate_uses_the_comment_commit_not_current_code():
    code = code_graph_for("acme", Role.LIBRARIAN, InMemoryCodeGraph())
    today = b"class GraphView:\n    def other(self):\n" + b"        pass\n" * 10     # line 6 is in GraphView.other now
    await code.upsert_file(REPO, parse_source("src/app/graph.py", today))
    shown = []
    def show(checkout, commit, path): shown.append((commit, path)); return GRAPH_AT_H1
    loc = SymbolLocator(REPO, code, Path("/checkout"), show)
    assert (await loc.locate(comment_101(), pr3())).qualname == "GraphView.switch"
    assert shown == [("h1", "src/app/graph.py")]

async def test_left_side_uses_base_commit(): ...          # side="LEFT" → show called with pr.base_sha "b1"
async def test_falls_back_to_index_when_commit_missing(): # show returns None → code.enclosing(path, line or original_line)
async def test_no_checkout_no_index_is_none(): ...        # SymbolLocator(REPO, None, None).locate(...) is None
async def test_file_level_and_unsupported_are_none(): ... # comment 106; a .md path
async def test_git_show_real_repo(tmp_path): ...          # two commits; git_show(first_sha) returns first content; bad sha → None
```

- [ ] **Step 2: Run** `uv run pytest tests/importer -v` — FAIL.
- [ ] **Step 3: Implement `episodes.py`.** `review_threads`: roots are comments with `in_reply_to is None`; a root is skipped as `unknown_pr` (PR not in `pulls`), `bot`, `pr_author` (root author is the PR's author), `not_team` (association not in `TEAM_ASSOCIATIONS`), in that order. Replies attach to their root if the root is in the input (bot replies dropped silently); a reply whose root is absent counts once as `orphan_reply` and is never a root. Threads sorted by root `created_at`, then `id`.
- [ ] **Step 4: Implement `locate.py`.** Line = `original_line` (fallback `line`); None for `subject_type == "file"`, no line, or an unsupported language. Commit = `pr.base_sha` for `LEFT`, else `original_commit_id`. With a checkout: `show` → `parse_source` → `innermost`, caching parses per `(commit, path)`. If there is no checkout or `show` returns None: `code.enclosing(repo, path, comment.line or comment.original_line)` when `code` is given, else None.
- [ ] **Step 5: Run** — PASS; ruff clean.
- [ ] **Step 6: Commit** `feat: import episodes, review threads and comment-to-symbol locator`

---

### Task 7: Import run — markers, cursor, limit, since, dry-run

**Files:**
- Create: `src/agentco/importer/run.py`
- Test: `tests/importer/test_run.py`

**Interfaces:**
- Consumes: `GitHub`, `FakeGitHub` (Task 1); `ScopedCodeGraph` (Task 3); episodes + `SymbolLocator` (Task 6); `ScopedMemory`, `TaskStore.get_meta/set_meta`, `Scope`, `Role` (Plan 1).
- Produces:
  - `@dataclass(frozen=True) ImportOptions`: `since: datetime | None = None`, `limit: int | None = None`, `dry_run: bool = False`.
  - `@dataclass ImportReport`: `issues: int`, `features: int`, `pulls: int`, `reviews: int` (pending episodes of each kind found this run), `already: int` (items skipped by marker), `skipped: Counter[str]` (thread reasons + `bot` for bot-authored issues/PRs), `written: int`, `linked: int`, `complete: bool`; property `planned -> int` (`issues + features + pulls + reviews`).
  - `async run_import(repo: str, gh: GitHub, store: TaskStore, rnd: ScopedMemory, product: ScopedMemory, locator: SymbolLocator, code: ScopedCodeGraph | None, opts: ImportOptions, now: datetime) -> ImportReport`.
  - Meta keys: markers `import:{tenant}:{repo}:{issue|feature|pr|review}:{number-or-comment-id}`; cursor `import:{tenant}:{repo}:cursor` (ISO-8601 UTC). `tenant` = `rnd.tenant`.

Fixture totals (Task 1 table): issues #1, #2 · feature #2 · PR #3 · reviews 101, 106 → 6 episodes; `skipped == Counter(bot=3, pr_author=1, not_team=1, orphan_reply=1)`. Oldest-first order: issue #1, PR #3, review 101, review 106, issue #2, feature #2.

- [ ] **Step 1: Write the failing tests** (fixture `world`: `FakeGitHub.from_fixtures`, `InMemoryBackend`, `gate`, `rnd = gate.for_role(Role.LIBRARIAN)`, `product = gate.for_role(Role.PM)`, `codegraph = InMemoryCodeGraph()` with `src/app/graph.py` indexed from `GRAPH_AT_H1`, a locator whose `show` returns `GRAPH_AT_H1`, `store`; `world.run(**overrides)` calls `run_import` with `now=datetime(2026, 11, 4, tzinfo=UTC)` unless overridden)

```python
async def test_full_import_writes_rnd_and_product_only(world):
    r = await world.run()
    assert (r.issues, r.features, r.pulls, r.reviews, r.written, r.complete) == (2, 1, 1, 2, 6, True)
    assert names(world.backend, "acme__rnd") == ["issue:acme-org/web#1", "pr:acme-org/web#3",
        "review:acme-org/web:101", "review:acme-org/web:106", "issue:acme-org/web#2"]
    assert names(world.backend, "acme__product") == ["feature:acme-org/web#2"]
    assert world.backend.items("acme__qa") == [] and world.backend.items("acme__company") == []
    assert r.skipped == Counter(bot=3, pr_author=1, not_team=1, orphan_reply=1)

async def test_review_linked_to_function(world):
    r = await world.run()
    assert r.linked == 1 and ("review:acme-org/web:101", "src/app/graph.py", "GraphView.switch") in world.codegraph.links("acme__rnd")

async def test_rerun_writes_nothing(world):
    await world.run(); r = await world.run()        # 2nd run fetches issues/comments since the cursor; PR #3 counts in `already`
    assert (r.written, r.planned) == (0, 0) and len(world.backend.items("acme__rnd")) == 5

async def test_dry_run_writes_nothing(world):
    r = await world.run(dry_run=True)
    assert (r.planned, r.written) == (6, 0) and world.backend.items("acme__rnd") == []
    assert world.store.get_meta("import:acme:acme-org/web:cursor") is None
    assert not [c for c in world.gh.calls if c[0] == "pull_files"]

async def test_limit_takes_oldest_and_does_not_advance_cursor(world):
    r = await world.run(limit=2)
    assert (r.written, r.complete) == (2, False)
    assert names(world.backend, "acme__rnd") == ["issue:acme-org/web#1", "pr:acme-org/web#3"]
    assert world.store.get_meta("import:acme:acme-org/web:cursor") is None
    assert (await world.run()).written == 4 and len(world.backend.items("acme__rnd")) == 5

async def test_crash_resumes_without_duplicates(world): ...   # backend.add raises on 3rd call; rerun → 6 unique names total
async def test_since_filters_by_created_and_keeps_cursor(world):
    r = await world.run(since=datetime(2026, 1, 5, tzinfo=UTC))
    assert r.written == 3 and world.store.get_meta("import:acme:acme-org/web:cursor") is None

async def test_cursor_after_complete_run_is_used_as_since(world):
    await world.run(now=datetime(2026, 2, 1, tzinfo=UTC))
    assert world.store.get_meta("import:acme:acme-org/web:cursor") == "2026-02-01T00:00:00+00:00"
    world.gh.calls.clear(); await world.run()
    assert ("issues", datetime(2026, 2, 1, tzinfo=UTC)) in world.gh.calls
    assert ("review_comments", datetime(2026, 2, 1, tzinfo=UTC)) in world.gh.calls

async def test_wrong_role_cannot_import(world):
    with pytest.raises(AccessDenied):
        await world.run(rnd=world.gate.for_role(Role.QA))
```

- [ ] **Step 2: Run** `uv run pytest tests/importer/test_run.py -v` — FAIL.
- [ ] **Step 3: Implement `run_import`.**
  1. `fetch_since = opts.since or cursor or None`; fetch `issues(repo, fetch_since)`, `pulls(repo)` (that endpoint has no `since`), `review_comments(repo, fetch_since)`.
  2. Drop bot-authored issues/PRs (`skipped["bot"]`); `review_threads` for comments; with `opts.since`, drop items and threads created before it.
  3. Build pending work `(created_at, kind_order, marker, build)` with `kind_order` issue 0 < feature 1 < pr 2 < review 3; items whose marker exists count in `already`. Sort.
  4. `dry_run` → return the counts; no `pull_files`, no `locate`, no writes, no meta.
  5. Write in order until `limit`: PRs call `gh.pull_files`; reviews call `locator.locate`; `rnd.add(Scope.RND, …)` or `product.add(Scope.PRODUCT, …)` for features; after a review is added and a symbol was found, `linked += await code.link_episode(repo, item.name, path, symbol.qualname)` when `code` is given; then `set_meta(marker, "done")`.
  6. `complete = not cut by limit`; if `complete and opts.since is None`: `set_meta(cursor, now.isoformat())`. Exceptions propagate (markers already set stay set; cursor untouched).
- [ ] **Step 4: Run** — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat: resumable, idempotent GitHub history import`

---

### Task 8: `agentco import` CLI, README, end-to-end smoke

**Files:**
- Modify: `src/agentco/cli.py` (`make_github` seam, `import` command), `README.md` (Plan 2 link, `index` / `import` usage, cost note)
- Test: `tests/test_cli.py`, `tests/test_import_integration.py`

**Interfaces:**
- Consumes: everything above; CLI `_load`, `_build_backend`, `_falkordb_guard`, `make_backend`, `make_code_graph`.
- Produces:
  - `cli.make_github: Callable[[], GitHub] = GhCli`.
  - `agentco import --repo OWNER/NAME [--repo-path PATH] [--since YYYY-MM-DD] [--limit N] [--dry-run] --config PATH`. Registered with `@app.command("import")` on a function named `import_` (`import` is a keyword).
  - Behaviour: `store.create_all()`; `ensure_graph` for `rnd` and `product`; with `--repo-path` and not `--dry-run`, run `index_repo` first and print its line (Task 5 format); locator gets the checkout when `--repo-path` is given. `--since` date → midnight UTC; `--limit` must be ≥ 1.
  - Output, dry-run (one per line, exact):
    `issues: {n}` · `features: {n}` · `pull requests: {n}` · `review comments: {n}` · `already imported: {n}` · `skipped: {reason}={n} …` (sorted by reason, `none` when empty) · `episodes to write: {planned} (each runs one Graphiti LLM extraction)`.
  - Output, real run, last line: `imported {written} episodes ({linked} linked to code)`; when cut by `--limit`: `stopped at --limit {n}: imported {written} of {planned} episodes; run again to continue`.
  - Errors: bad `--repo` / `--since` / `--limit` → exit 1; `GitHubError` → its message, exit 3; FalkorDB → exit 2; LLM client → exit 1.

- [ ] **Step 1: Write the failing tests** (`env` fixture extended: `cli.make_github` → `FakeGitHub.from_fixtures`, `cli.make_code_graph` → shared `InMemoryCodeGraph`)

```python
def test_import_dry_run_prints_counts_and_writes_nothing(env):
    env("init"); r = env("import", "--repo", "acme-org/web", "--dry-run")
    assert r.exit_code == 0
    assert r.stdout.splitlines()[-7:] == ["issues: 2", "features: 1", "pull requests: 1", "review comments: 2",
        "already imported: 0", "skipped: bot=3 not_team=1 orphan_reply=1 pr_author=1",
        "episodes to write: 6 (each runs one Graphiti LLM extraction)"]
    assert env.backend.items("acme__rnd") == []

def test_import_then_rerun(env):
    env("init")
    assert env("import", "--repo", "acme-org/web").stdout.splitlines()[-1] == "imported 6 episodes (0 linked to code)"
    assert env("import", "--repo", "acme-org/web").stdout.splitlines()[-1] == "imported 0 episodes (0 linked to code)"

def test_import_limit_message(env):
    env("init")
    assert env("import", "--repo", "acme-org/web", "--limit", "2").stdout.splitlines()[-1] == \
        "stopped at --limit 2: imported 2 of 6 episodes; run again to continue"

def test_import_with_repo_path_indexes_first(env, tmp_path): ...   # git repo with src/app/graph.py → "indexed 1, …" line printed before the import line

def test_import_github_error_exits_3_and_writes_nothing(env, monkeypatch):
    monkeypatch.setattr(cli, "make_github", lambda: FailingGitHub(GitHubError("gh api failed: HTTP 404: Not Found")))
    r = env("import", "--repo", "acme-org/missing")
    assert r.exit_code == 3 and r.stdout.splitlines()[-1] == "gh api failed: HTTP 404: Not Found"
    assert env.store.get_meta("import:acme:acme-org/missing:cursor") is None

@pytest.mark.parametrize("args", [["--repo", "nope"], ["--repo", "o/r", "--since", "yesterday"], ["--repo", "o/r", "--limit", "0"]])
def test_import_bad_arguments_exit_1(env, args): ...
```

```python
pytestmark = pytest.mark.integration      # tests/test_import_integration.py

def test_import_smoke(cfg, tmp_path):
    repo = os.environ.get("AGENTCO_IT_REPO")             # a small repo with a few PRs + human review comments
    if not repo or subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        pytest.skip("needs AGENTCO_IT_REPO and gh auth")
    config = write_config(tmp_path, cfg)                 # cfg's tenant/host + the roles block from tests/test_cli.py
    before = dry_run_already(config, repo)
    r = invoke("import", "--repo", repo, "--limit", "2", config=config)
    assert r.exit_code == 0 and "imported 2 of" in r.stdout
    assert dry_run_already(config, repo) == before + 2
    out = invoke("memory", "search", "pull request", "--as", "triage", config=config).stdout
    assert f"{cfg.tenant}__rnd" in out
```

- [ ] **Step 2: Run** `uv run pytest tests/test_cli.py -v` — FAIL.
- [ ] **Step 3: Implement the `import` command** (`asyncio.run` inside `_falkordb_guard`; catch `GitHubError` → echo + `typer.Exit(3)`; rnd memory = `for_role(Role.LIBRARIAN)`, product memory = `for_role(Role.PM)`, code graph = `code_graph_for(cfg.tenant, Role.LIBRARIAN, make_code_graph(cfg))`).
- [ ] **Step 4: Update `README.md`**: link Plan 2; add `uv run agentco index --repo owner/name --repo-path .` and `uv run agentco import --repo owner/name --repo-path . --dry-run` then without `--dry-run`; one paragraph: each episode is one Graphiti extraction (several LLM calls), so run `--dry-run` first and use `--limit` / `--since`; interrupted runs resume; bot comments and comments by the PR's own author or by non-members are not imported; `gh auth login` is required.
- [ ] **Step 5: Run** the full unit suite `uv run pytest -q` and `uv run ruff check src tests` — PASS, clean.
- [ ] **Step 6: Manual end-to-end** — `docker compose up -d falkordb`; with a real `agentco.config.yml`, a local clone of a small repo, `gh auth status` OK: `uv run agentco index --repo o/r --repo-path ../r` → `indexed N, …`; again → `indexed 0, …`; `uv run agentco import --repo o/r --repo-path ../r --dry-run` → counts; `uv run agentco import --repo o/r --repo-path ../r --limit 5` → `stopped at --limit 5: …`; FalkorDB Browser (`localhost:3000`) on `acme__rnd`: `MATCH (e:Episodic)-[:ABOUT]->(f:Code:Function) RETURN e.name, f.qualname` returns rows for linked review comments. Then `AGENTCO_IT_REPO=o/r FALKORDB_HOST=localhost OPENAI_API_KEY=… uv run pytest -m integration -v` — PASS.
- [ ] **Step 7: Commit** `feat: agentco import command`
