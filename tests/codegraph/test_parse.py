from agentco.code.parse import (
    LANGUAGES,
    Symbol,
    content_hash,
    innermost,
    language_for,
    parse_source,
)

PY = b"""import x
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
"""


def test_python_symbols():
    fs = parse_source("src/app/view.py", PY)
    assert [(s.kind, s.qualname, s.start_line, s.end_line) for s in fs.symbols] == [
        ("Function", "top", 2, 5),  # decorated: starts at the decorator
        ("Class", "GraphView", 7, 12),
        ("Function", "GraphView.switch", 8, 11),
        ("Function", "GraphView.switch.inner", 9, 10),
        ("Function", "GraphView.load", 12, 12),
    ]
    by = {s.qualname: s for s in fs.symbols}
    assert by["top"].calls == ("helper", "method")
    assert by["GraphView.switch"].calls == ("load",)  # inner's calls are inner's own
    assert by["GraphView.switch"].name == "switch"
    assert fs.path == "src/app/view.py"
    assert fs.language == "python" and fs.hash == content_hash(PY)


JS = b"""function top(a) { helper(a); return obj.method(a); }
class GraphView extends Base {
  switch(g) { this.load(g); }
  async load(g) {}
}
const arrow = (x) => { foo(x) };
export default function () {}
"""


def test_javascript_symbols():
    fs = parse_source("web/view.js", JS)
    assert [s.qualname for s in fs.symbols] == [
        "top",
        "GraphView",
        "GraphView.switch",
        "GraphView.load",
        "arrow",
    ]
    calls = {s.qualname: s.calls for s in fs.symbols}
    assert calls["arrow"] == ("foo",)
    assert calls["top"] == ("helper", "method")
    assert fs.language == "javascript"


def test_typescript_and_tsx():
    ts = parse_source(
        "a.ts",
        b"abstract class A { g(): number { return h(); } }\n"
        b"export function k<T>(x: T): T { return x }",
    )
    assert [s.qualname for s in ts.symbols] == ["A", "A.g", "k"]
    assert ts.language == "typescript"
    tsx = parse_source(
        "App.tsx", b"export function App(){ return <div onClick={() => go()}/> }"
    )
    assert [(s.qualname, s.calls) for s in tsx.symbols] == [("App", ("go",))]
    assert tsx.language == "tsx"


def test_unsupported_extension():
    assert parse_source("README.md", b"# hi") is None


def test_broken_source_still_yields_symbols():
    fs = parse_source(
        "x.py", b"def broken(:\n    x(\nclass Ok:\n    def m(self): pass\n"
    )
    assert "Ok.m" in [s.qualname for s in fs.symbols]


def test_innermost():
    syms = parse_source("src/app/view.py", PY).symbols
    assert innermost(syms, 10).qualname == "GraphView.switch.inner"
    assert innermost(syms, 11).qualname == "GraphView.switch"
    assert innermost(syms, 8).qualname == "GraphView.switch"  # boundary inclusive
    assert innermost(syms, 6) is None


def test_innermost_tie_prefers_smallest_span():
    outer = Symbol("Function", "o", "o", 3, 9)
    inner = Symbol("Function", "i", "i", 3, 5)
    assert innermost([outer, inner], 4) is inner
    assert innermost([inner, outer], 4) is inner


def test_language_for():
    paths = [
        "a.py",
        "a.js",
        "a.jsx",
        "a.mjs",
        "a.cjs",
        "a.ts",
        "a.tsx",
        "a.md",
        "Makefile",
    ]
    assert [language_for(p) for p in paths] == [
        "python",
        "javascript",
        "javascript",
        "javascript",
        "javascript",
        "typescript",
        "tsx",
        None,
        None,
    ]
    assert dict(LANGUAGES) == {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
    }


def test_content_hash_is_sha256_hex():
    assert (
        content_hash(b"")
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_duplicate_qualname_first_wins():
    fs = parse_source("d.py", b"def f():\n    a()\ndef f():\n    b()\n")
    assert [(s.qualname, s.start_line, s.calls) for s in fs.symbols] == [
        ("f", 1, ("a",))
    ]


def test_calls_deduped_and_module_level_dropped():
    fs = parse_source("c.py", b"top_level()\ndef f():\n    a()\n    b()\n    a()\n")
    assert [(s.qualname, s.calls) for s in fs.symbols] == [("f", ("a", "b"))]
