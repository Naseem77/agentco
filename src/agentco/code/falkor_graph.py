"""FalkorDB-backed code graph, written with plain Cypher.

Every code node carries the `:Code` label so it never collides with the `:Entity`
nodes Graphiti keeps in the same graph. Identity is (repo, path) for files and
(repo, path, qualname) for symbols, so one path in two repos never collides.
"""

from collections import defaultdict
from typing import Any

from falkordb.asyncio import FalkorDB

from agentco.code.graph import CallEdge
from agentco.code.parse import FileStructure, Symbol
from agentco.config import TenantConfig

_INDEXES = (
    "CREATE INDEX FOR (n:Code) ON (n.path)",
    "CREATE INDEX FOR (n:Code) ON (n.repo)",
    "CREATE INDEX FOR (e:Episodic) ON (e.name)",
)

_FILE = "(f:Code {repo: $repo, path: $path, kind: 'File'})"

_UPSERT_SYMBOLS = (
    "UNWIND $rows AS row "
    "MERGE (s:Code {{repo: $repo, path: $path, qualname: row.qualname}}) "
    "SET s:{label}, s.name = row.name, s.kind = row.kind, "
    "s.start_line = row.start_line, s.end_line = row.end_line{extra}"
)

_SYMBOL_COLUMNS = "s.kind, s.qualname, s.name, s.start_line, s.end_line, s.calls"
_SYMBOL_ORDER = "ORDER BY s.start_line, s.end_line DESC, s.qualname"


def _symbol(row: list[Any]) -> Symbol:
    kind, qualname, name, start, end, calls = row
    return Symbol(kind, name, qualname, start, end, tuple(calls or ()))


def _symbol_row(s: Symbol) -> dict[str, Any]:
    return {
        "qualname": s.qualname,
        "name": s.name,
        "kind": s.kind,
        "start_line": s.start_line,
        "end_line": s.end_line,
        "calls": list(s.calls),
    }


class FalkorCodeGraph:
    """CodeGraphBackend over FalkorDB; each graph name is its own FalkorDB graph."""

    def __init__(self, db: FalkorDB) -> None:
        self.db = db

    async def _run(
        self, graph: str, query: str, params: dict[str, Any] | None = None
    ) -> list[list[Any]]:
        result = await self.db.select_graph(graph).query(query, params or {})
        return result.result_set

    async def ensure_schema(self, graph: str) -> None:
        for statement in _INDEXES:
            try:
                await self._run(graph, statement)
            except Exception as e:
                if "already indexed" not in str(e):
                    raise

    async def file_hashes(self, graph: str, repo: str) -> dict[str, str]:
        # a file whose upsert did not finish has no hash yet and counts as unseen
        rows = await self._run(
            graph,
            "MATCH (f:Code {repo: $repo, kind: 'File'}) "
            "WHERE f.hash IS NOT NULL RETURN f.path, f.hash",
            {"repo": repo},
        )
        return {path: digest for path, digest in rows}

    async def upsert_file(self, graph: str, repo: str, fs: FileStructure) -> None:
        # The old hash is cleared first and the new one written last, so an interrupted
        # upsert is retried even if the file is later reverted to its old content.
        scope = {"repo": repo, "path": fs.path}
        await self._run(
            graph,
            f"MERGE {_FILE} SET f:File, f.name = $name, f.language = $language, "
            "f.hash = null",
            {**scope, "name": fs.path.rsplit("/", 1)[-1], "language": fs.language},
        )
        # Symbols still present are updated in place, never deleted, so ABOUT edges stay.
        await self._run(
            graph,
            "MATCH (s:Code {repo: $repo, path: $path}) "
            "WHERE s.kind IN ['Class', 'Function'] "
            "AND NOT (s.kind + ':' + s.qualname) IN $keep DETACH DELETE s",
            {**scope, "keep": [f"{s.kind}:{s.qualname}" for s in fs.symbols]},
        )
        for kind, extra in (("Class", ""), ("Function", ", s.calls = row.calls")):
            rows = [_symbol_row(s) for s in fs.symbols if s.kind == kind]
            if rows:
                await self._run(
                    graph,
                    _UPSERT_SYMBOLS.format(label=kind, extra=extra),
                    {**scope, "rows": rows},
                )
        await self._run(
            graph,
            "MATCH (:Code {repo: $repo, path: $path})-[r:DEFINES]->"
            "(:Code {repo: $repo, path: $path}) DELETE r",
            scope,
        )
        qualnames = {s.qualname for s in fs.symbols}
        top: list[dict[str, str]] = []
        nested: list[dict[str, str]] = []
        for s in fs.symbols:
            parent = s.qualname.rpartition(".")[0]
            if parent in qualnames:
                nested.append({"parent": parent, "qualname": s.qualname})
            else:
                top.append({"qualname": s.qualname})
        if top:
            await self._run(
                graph,
                f"MATCH {_FILE} UNWIND $rows AS row "
                "MATCH (s:Code {repo: $repo, path: $path, qualname: row.qualname}) "
                "CREATE (f)-[:DEFINES]->(s)",
                {**scope, "rows": top},
            )
        if nested:
            await self._run(
                graph,
                "UNWIND $rows AS row "
                "MATCH (p:Code {repo: $repo, path: $path, qualname: row.parent}) "
                "MATCH (s:Code {repo: $repo, path: $path, qualname: row.qualname}) "
                "CREATE (p)-[:DEFINES]->(s)",
                {**scope, "rows": nested},
            )
        await self._run(
            graph, f"MATCH {_FILE} SET f.hash = $hash", {**scope, "hash": fs.hash}
        )

    async def delete_file(self, graph: str, repo: str, path: str) -> None:
        await self._run(
            graph,
            "MATCH (n:Code {repo: $repo, path: $path}) DETACH DELETE n",
            {"repo": repo, "path": path},
        )

    async def symbols_in(self, graph: str, repo: str, path: str) -> list[Symbol]:
        rows = await self._run(
            graph,
            "MATCH (s:Code {repo: $repo, path: $path}) "
            "WHERE s.kind IN ['Class', 'Function'] "
            f"RETURN {_SYMBOL_COLUMNS} {_SYMBOL_ORDER}",
            {"repo": repo, "path": path},
        )
        return [_symbol(row) for row in rows]

    async def all_symbols(self, graph: str, repo: str) -> dict[str, list[Symbol]]:
        files = await self.file_hashes(graph, repo)
        by_path: dict[str, list[Symbol]] = defaultdict(list)
        rows = await self._run(
            graph,
            "MATCH (s:Code {repo: $repo}) WHERE s.kind IN ['Class', 'Function'] "
            f"RETURN s.path, {_SYMBOL_COLUMNS} {_SYMBOL_ORDER}",
            {"repo": repo},
        )
        for path, *rest in rows:
            by_path[path].append(_symbol(rest))
        return {path: by_path.get(path, []) for path in files}

    async def replace_calls(self, graph: str, repo: str, edges: list[CallEdge]) -> None:
        await self._run(
            graph,
            "MATCH (:Code {repo: $repo, kind: 'Function'})-[r:CALLS]->"
            "(:Code {repo: $repo, kind: 'Function'}) DELETE r",
            {"repo": repo},
        )
        rows = [
            {"cp": e.caller_path, "c": e.caller, "tp": e.callee_path, "t": e.callee}
            for e in sorted(set(edges))
        ]
        if rows:
            await self._run(
                graph,
                "UNWIND $edges AS e "
                "MATCH (a:Code {repo: $repo, path: e.cp, qualname: e.c, "
                "kind: 'Function'}) "
                "MATCH (b:Code {repo: $repo, path: e.tp, qualname: e.t, "
                "kind: 'Function'}) "
                "MERGE (a)-[:CALLS]->(b)",
                {"repo": repo, "edges": rows},
            )

    async def call_edges(self, graph: str, repo: str) -> list[CallEdge]:
        rows = await self._run(
            graph,
            "MATCH (a:Code {repo: $repo, kind: 'Function'})-[:CALLS]->"
            "(b:Code {repo: $repo, kind: 'Function'}) "
            "RETURN a.path, a.qualname, b.path, b.qualname",
            {"repo": repo},
        )
        return sorted(CallEdge(*row) for row in rows)

    async def link_episode(
        self, graph: str, repo: str, episode: str, path: str, qualname: str
    ) -> bool:
        """False when the symbol or the episode is not in the graph."""
        rows = await self._run(
            graph,
            "MATCH (s:Code {repo: $repo, path: $path, qualname: $qualname}) "
            "WHERE s.kind IN ['Class', 'Function'] "
            "MATCH (e:Episodic {name: $episode}) "
            "MERGE (e)-[r:ABOUT]->(s) RETURN count(r)",
            {"repo": repo, "path": path, "qualname": qualname, "episode": episode},
        )
        return bool(rows and rows[0][0])


def build_falkor_code_graph(cfg: TenantConfig) -> FalkorCodeGraph:
    return FalkorCodeGraph(FalkorDB(host=cfg.falkordb_host, port=cfg.falkordb_port))
