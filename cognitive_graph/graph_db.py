"""Neo4j access layer: schema, ingestion writes and retrieval reads."""
import re
from pathlib import Path

from neo4j import GraphDatabase as Neo4jDriver

from .code_parser import FunctionEntity

_CONSTRAINTS = [
    "CREATE CONSTRAINT file_path IF NOT EXISTS FOR (f:File) REQUIRE f.path IS UNIQUE",
    "CREATE CONSTRAINT function_key IF NOT EXISTS "
    "FOR (fn:Function) REQUIRE (fn.file_path, fn.name, fn.start_line) IS UNIQUE",
]

_DELETE_FILE_FUNCTIONS = "MATCH (fn:Function {file_path: $path}) DETACH DELETE fn"

_CREATE_FILE_AND_FUNCTIONS = """
MERGE (f:File {path: $path})
  SET f.name = $name
WITH f
UNWIND $functions AS fn
CREATE (n:Function {
  file_path: $path, name: fn.name, start_line: fn.start_line,
  end_line: fn.end_line, lines: fn.lines, raw_code: fn.raw_code, calls: fn.calls
})
CREATE (n)-[:DEFINED_IN]->(f)
"""

_LINK_CALLS = """
UNWIND $edges AS e
MATCH (a:Function {file_path: e.from_path, name: e.from_name, start_line: e.from_line})
MATCH (b:Function {file_path: e.to_path,   name: e.to_name,   start_line: e.to_line})
MERGE (a)-[:CALLS]->(b)
"""

_DETAILS = """
OPTIONAL MATCH (fn)-[:CALLS]->(callee:Function)
OPTIONAL MATCH (caller:Function)-[:CALLS]->(fn)
RETURN f.path AS file, fn.name AS name, fn.start_line AS start_line, fn.raw_code AS code,
       collect(DISTINCT callee.name) AS calls,
       collect(DISTINCT caller.name) AS called_by
ORDER BY file, start_line
LIMIT $limit
"""

# Functions named in the question plus their direct callers/callees.
_RELATED = """
MATCH (seed:Function) WHERE seed.name IN $names
OPTIONAL MATCH (seed)-[:CALLS]-(nbr:Function)
WITH collect(DISTINCT seed) + collect(DISTINCT nbr) AS picked
UNWIND picked AS fn
WITH DISTINCT fn
MATCH (fn)-[:DEFINED_IN]->(f:File)
""" + _DETAILS

_ALL = "MATCH (fn:Function)-[:DEFINED_IN]->(f:File)\n" + _DETAILS


class GraphDatabase:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self._driver = Neo4jDriver.driver(uri, auth=(user, password))

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> "GraphDatabase":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def verify(self) -> None:
        self._driver.verify_connectivity()

    def init_schema(self) -> None:
        for stmt in _CONSTRAINTS:
            self._driver.execute_query(stmt)

    def reset(self) -> None:
        """Delete EVERYTHING in the database."""
        self._driver.execute_query("MATCH (n) DETACH DELETE n")

    # --- writes -------------------------------------------------------------

    def replace_file(self, path: str, functions: list[FunctionEntity]) -> None:
        """Atomically replace a file's functions, so re-ingesting never leaves
        stale nodes behind (e.g. a function that moved or was deleted)."""
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
            tx.run(_DELETE_FILE_FUNCTIONS, path=path)
            tx.run(
                _CREATE_FILE_AND_FUNCTIONS,
                path=path, name=Path(path).name, functions=payload,
            )

        with self._driver.session() as session:
            session.execute_write(tx_fn)

    def link_calls(self, edges: list[dict]) -> None:
        if edges:
            self._driver.execute_query(_LINK_CALLS, edges=edges)

    # --- reads --------------------------------------------------------------

    def fetch_context(self, question: str, limit: int = 50) -> list[dict]:
        """Functions the question mentions (+ direct callers/callees); if it
        mentions none, fall back to every function up to `limit`."""
        records, _, _ = self._driver.execute_query("MATCH (fn:Function) RETURN DISTINCT fn.name AS name")
        names = [
            r["name"] for r in records
            if re.search(rf"\b{re.escape(r['name'])}\b", question, re.IGNORECASE)
        ]
        if names:
            records, _, _ = self._driver.execute_query(_RELATED, names=names, limit=limit)
        else:
            records, _, _ = self._driver.execute_query(_ALL, limit=limit)
        return [r.data() for r in records]

    def stats(self) -> dict:
        records, _, _ = self._driver.execute_query(
            "RETURN COUNT { (:File) } AS files, COUNT { (:Function) } AS functions, "
            "COUNT { ()-[:CALLS]->() } AS calls"
        )
        return records[0].data()

    def get_function(self, path: str, name: str, start_line: int | None = None) -> list[dict]:
        records, _, _ = self._driver.execute_query(
            """
            MATCH (fn:Function {file_path: $path, name: $name})
            WHERE $start IS NULL OR fn.start_line = $start
            RETURN fn.start_line AS start_line, fn.end_line AS end_line, fn.raw_code AS code
            ORDER BY start_line
            """,
            path=path, name=name, start=start_line,
        )
        return [r.data() for r in records]

    def functions_calling(self, names: set[str]) -> list[dict]:
        """Functions (in any file) whose call list mentions one of `names`."""
        records, _, _ = self._driver.execute_query(
            """
            MATCH (fn:Function)
            WHERE any(c IN coalesce(fn.calls, []) WHERE c IN $names)
            RETURN fn.file_path AS path, fn.name AS name, fn.start_line AS start_line, fn.calls AS calls
            """,
            names=sorted(names),
        )
        return [r.data() for r in records]

    def functions_named(self, names: set[str]) -> list[dict]:
        records, _, _ = self._driver.execute_query(
            "MATCH (fn:Function) WHERE fn.name IN $names "
            "RETURN fn.file_path AS path, fn.name AS name, fn.start_line AS start_line",
            names=sorted(names),
        )
        return [r.data() for r in records]
