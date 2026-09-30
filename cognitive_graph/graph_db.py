"""Neo4j access layer: schema, ingestion writes and retrieval reads.

Every File / Function node and every DEFINED_IN / CALLS relationship carries a
`project_id`, and every query filters on it. A GraphDatabase built without a
project id (the "base" handle) can only connect and create the schema; call
`scoped(project_id)` to get a handle that can read or write project data.
"""
import re
from pathlib import Path

from neo4j import GraphDatabase as Neo4jDriver

from .code_parser import FunctionEntity

# Constraints from before project scoping. They made (path) and
# (file_path, name, start_line) unique across the WHOLE database, which would stop
# two projects from both having e.g. `main.py`. Dropping a constraint keeps the data.
_LEGACY_CONSTRAINTS = ["file_path", "function_key"]

_CONSTRAINTS = [
    "CREATE CONSTRAINT file_project_path IF NOT EXISTS "
    "FOR (f:File) REQUIRE (f.project_id, f.path) IS UNIQUE",
    "CREATE CONSTRAINT function_project_key IF NOT EXISTS "
    "FOR (fn:Function) REQUIRE (fn.project_id, fn.file_path, fn.name, fn.start_line) IS UNIQUE",
]

_DELETE_FILE_FUNCTIONS = "MATCH (fn:Function {project_id: $pid, file_path: $path}) DETACH DELETE fn"

_CREATE_FILE_AND_FUNCTIONS = """
MERGE (f:File {project_id: $pid, path: $path})
  SET f.name = $name
WITH f
UNWIND $functions AS fn
CREATE (n:Function {
  project_id: $pid, file_path: $path, name: fn.name, start_line: fn.start_line,
  end_line: fn.end_line, lines: fn.lines, raw_code: fn.raw_code, calls: fn.calls
})
CREATE (n)-[:DEFINED_IN {project_id: $pid}]->(f)
"""

_LINK_CALLS = """
UNWIND $edges AS e
MATCH (a:Function {project_id: $pid, file_path: e.from_path, name: e.from_name, start_line: e.from_line})
MATCH (b:Function {project_id: $pid, file_path: e.to_path,   name: e.to_name,   start_line: e.to_line})
MERGE (a)-[r:CALLS]->(b)
  SET r.project_id = $pid
"""

_DETAILS = """
OPTIONAL MATCH (fn)-[:CALLS]->(callee:Function {project_id: $pid})
OPTIONAL MATCH (caller:Function {project_id: $pid})-[:CALLS]->(fn)
RETURN f.path AS file, fn.name AS name, fn.start_line AS start_line, fn.raw_code AS code,
       collect(DISTINCT callee.name) AS calls,
       collect(DISTINCT caller.name) AS called_by
ORDER BY file, start_line
LIMIT $limit
"""

# Functions named in the question plus their direct callers/callees.
_RELATED = """
MATCH (seed:Function {project_id: $pid}) WHERE seed.name IN $names
OPTIONAL MATCH (seed)-[:CALLS]-(nbr:Function {project_id: $pid})
WITH collect(DISTINCT seed) + collect(DISTINCT nbr) AS picked
UNWIND picked AS fn
WITH DISTINCT fn
MATCH (fn)-[:DEFINED_IN]->(f:File {project_id: $pid})
""" + _DETAILS

_ALL = "MATCH (fn:Function {project_id: $pid})-[:DEFINED_IN]->(f:File {project_id: $pid})\n" + _DETAILS

# Functions that match by name or by the file they live in (no neighbours: the
# calls / called_by lists already name them). Used for prompt-time retrieval.
_SEEDS = """
MATCH (fn:Function {project_id: $pid})
WHERE fn.name IN $names OR fn.file_path IN $paths
MATCH (fn)-[:DEFINED_IN]->(f:File {project_id: $pid})
""" + _DETAILS

# Names too generic to count as "the prompt mentions this function".
_COMMON_NAMES = frozenset(
    "main init test tests run get set add list load save read write open close name data "
    "self this call func function handle process update delete create build start stop "
    "help none true false".split()
)


class ProjectRequiredError(RuntimeError):
    """A project-scoped operation was attempted on a handle with no project id."""


class GraphDatabase:
    def __init__(self, uri: str, user: str, password: str, project_id: str | None = None,
                 *, _driver=None, **driver_options) -> None:
        self._owns_driver = _driver is None
        self._driver = _driver or Neo4jDriver.driver(uri, auth=(user, password), **driver_options)
        self.project_id = project_id

    def scoped(self, project_id: str) -> "GraphDatabase":
        """A handle on the same connection, restricted to one project."""
        if not project_id:
            raise ProjectRequiredError("A non-empty project id is required")
        return GraphDatabase("", "", "", project_id, _driver=self._driver)

    @property
    def _pid(self) -> str:
        if not self.project_id:
            raise ProjectRequiredError("No project id: use db.scoped(project_id) before reading or writing project data")
        return self.project_id

    def close(self) -> None:
        if self._owns_driver:
            self._driver.close()

    def __enter__(self) -> "GraphDatabase":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def verify(self) -> None:
        self._driver.verify_connectivity()

    def init_schema(self) -> None:
        for name in _LEGACY_CONSTRAINTS:
            self._driver.execute_query(f"DROP CONSTRAINT {name} IF EXISTS")
        for stmt in _CONSTRAINTS:
            self._driver.execute_query(stmt)

    def reset(self) -> None:
        """Delete this project's graph data. Other projects are untouched."""
        pid = self._pid
        for label in ("Function", "File"):
            self._driver.execute_query(f"MATCH (n:{label} {{project_id: $pid}}) DETACH DELETE n", pid=pid)

    # --- writes -------------------------------------------------------------

    def replace_file(self, path: str, functions: list[FunctionEntity]) -> None:
        """Atomically replace a file's functions, so re-ingesting never leaves
        stale nodes behind (e.g. a function that moved or was deleted)."""
        pid = self._pid
        payload = [
            {
                "name": f.name,
                "raw_code": f.raw_code,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "lines": f.lines,
                "calls": list(f.calls),
            }
            for f in functions
        ]

        def tx_fn(tx):
            tx.run(_DELETE_FILE_FUNCTIONS, pid=pid, path=path)
            tx.run(
                _CREATE_FILE_AND_FUNCTIONS,
                pid=pid, path=path, name=Path(path).name, functions=payload,
            )

        with self._driver.session() as session:
            session.execute_write(tx_fn)

    def link_calls(self, edges: list[dict]) -> None:
        if edges:
            self._driver.execute_query(_LINK_CALLS, pid=self._pid, edges=edges)

    # --- reads --------------------------------------------------------------

    def _function_names(self) -> list[str]:
        records, _, _ = self._driver.execute_query(
            "MATCH (fn:Function {project_id: $pid}) RETURN DISTINCT fn.name AS name", pid=self._pid)
        return [r["name"] for r in records]

    def fetch_context(self, question: str, limit: int = 50, fallback_all: bool = True) -> list[dict]:
        """Functions the question mentions (+ direct callers/callees). If it
        mentions none, fall back to every function up to `limit` (Q&A behaviour)
        unless `fallback_all` is False."""
        names = [n for n in self._function_names() if re.search(rf"\b{re.escape(n)}\b", question, re.IGNORECASE)]
        if names:
            records, _, _ = self._driver.execute_query(_RELATED, pid=self._pid, names=names, limit=limit)
        elif fallback_all:
            records, _, _ = self._driver.execute_query(_ALL, pid=self._pid, limit=limit)
        else:
            return []
        return [r.data() for r in records]

    def find_relevant(self, prompt: str, limit: int = 5) -> list[dict]:
        """Functions this project's prompt refers to by name, or that live in a
        file it names. Returns [] (never 'everything') when nothing matches."""
        pid = self._pid
        names = [n for n in self._function_names()
                 if len(n) >= 4 and n.lower() not in _COMMON_NAMES
                 and re.search(rf"\b{re.escape(n)}\b", prompt, re.IGNORECASE)]
        records, _, _ = self._driver.execute_query(
            "MATCH (f:File {project_id: $pid}) RETURN f.path AS path", pid=pid)
        lowered = prompt.lower()
        paths = [r["path"] for r in records
                 if r["path"].lower() in lowered
                 or (Path(r["path"]).name.lower() in lowered and "." in Path(r["path"]).name)]
        if not names and not paths:
            return []
        records, _, _ = self._driver.execute_query(_SEEDS, pid=pid, names=names, paths=paths, limit=limit)
        return [r.data() for r in records]

    def stats(self) -> dict:
        records, _, _ = self._driver.execute_query(
            "RETURN COUNT { (:File {project_id: $pid}) } AS files, "
            "COUNT { (:Function {project_id: $pid}) } AS functions, "
            "COUNT { (:Function {project_id: $pid})-[:CALLS]->(:Function {project_id: $pid}) } AS calls",
            pid=self._pid,
        )
        return records[0].data()

    def get_function(self, path: str, name: str, start_line: int | None = None) -> list[dict]:
        records, _, _ = self._driver.execute_query(
            """
            MATCH (fn:Function {project_id: $pid, file_path: $path, name: $name})
            WHERE $start IS NULL OR fn.start_line = $start
            RETURN fn.start_line AS start_line, fn.end_line AS end_line, fn.raw_code AS code
            ORDER BY start_line
            """,
            pid=self._pid, path=path, name=name, start=start_line,
        )
        return [r.data() for r in records]

    def functions_calling(self, names: set[str]) -> list[dict]:
        """Functions (in any file of this project) whose call list mentions one of `names`."""
        records, _, _ = self._driver.execute_query(
            """
            MATCH (fn:Function {project_id: $pid})
            WHERE any(c IN coalesce(fn.calls, []) WHERE c IN $names)
            RETURN fn.file_path AS path, fn.name AS name, fn.start_line AS start_line, fn.calls AS calls
            """,
            pid=self._pid, names=sorted(names),
        )
        return [r.data() for r in records]

    def functions_named(self, names: set[str]) -> list[dict]:
        records, _, _ = self._driver.execute_query(
            "MATCH (fn:Function {project_id: $pid}) WHERE fn.name IN $names "
            "RETURN fn.file_path AS path, fn.name AS name, fn.start_line AS start_line",
            pid=self._pid, names=sorted(names),
        )
        return [r.data() for r in records]

    # --- legacy (pre-project-scoping) data ------------------------------------
    # Nodes written before project ids existed have no `project_id`. Every query
    # above ignores them, so they are inert until adopted or purged explicitly.

    def legacy_functions(self) -> list[dict]:
        records, _, _ = self._driver.execute_query(
            "MATCH (fn:Function) WHERE fn.project_id IS NULL "
            "RETURN fn.file_path AS path, fn.name AS name, fn.raw_code AS code")
        return [r.data() for r in records]

    def legacy_file_paths(self) -> list[str]:
        records, _, _ = self._driver.execute_query(
            "MATCH (f:File) WHERE f.project_id IS NULL RETURN f.path AS path ORDER BY path")
        return [r["path"] for r in records]

    def adopt_legacy_files(self, paths: list[str]) -> dict:
        """Assign unscoped File/Function nodes for exactly `paths` to this project.
        Files this project already has are skipped to avoid clobbering them."""
        pid = self._pid
        existing = {r["path"] for r in self._driver.execute_query(
            "MATCH (f:File {project_id: $pid}) RETURN f.path AS path", pid=pid)[0]}
        take = [p for p in paths if p not in existing]
        if take:
            self._driver.execute_query(
                "MATCH (f:File) WHERE f.project_id IS NULL AND f.path IN $paths SET f.project_id = $pid",
                pid=pid, paths=take)
            self._driver.execute_query(
                "MATCH (fn:Function) WHERE fn.project_id IS NULL AND fn.file_path IN $paths SET fn.project_id = $pid",
                pid=pid, paths=take)
            self._driver.execute_query(
                "MATCH (a:Function {project_id: $pid})-[r:CALLS]->(b:Function {project_id: $pid}) SET r.project_id = $pid",
                pid=pid)
            self._driver.execute_query(
                "MATCH (:Function {project_id: $pid})-[r:DEFINED_IN]->(:File {project_id: $pid}) SET r.project_id = $pid",
                pid=pid)
        return {"adopted": len(take), "skipped_existing": len(paths) - len(take)}

    def purge_legacy(self) -> None:
        """Delete every node that has no project id."""
        for label in ("Function", "File"):
            self._driver.execute_query(f"MATCH (n:{label}) WHERE n.project_id IS NULL DETACH DELETE n")
