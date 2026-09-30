"""Project identity and graph isolation.

Two layers:
  * structural tests (always run): a recording fake driver proves every project-data query
    carries the project id, and an unscoped handle refuses to run any.
  * live tests (run only if Neo4j is reachable): two projects share a path, and neither
    can see, change or delete the other's data. They use random project ids and clean up
    only those, never touching other data in the database.
"""
import json
import os
import subprocess
import uuid

import pytest

from cognitive_graph.code_parser import FunctionEntity
from cognitive_graph.graph_db import GraphDatabase, ProjectRequiredError
from cognitive_graph.memory import MemoryStore, ensure_project, resolve_project


# --- project identity ---------------------------------------------------------------


def _git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_two_projects_get_distinct_stable_ids(tmp_path):
    axis, eros = tmp_path / "axis-ri", tmp_path / "eros-innovation"
    for d in (axis, eros):
        d.mkdir()
        _git_init(d)
    a, e = ensure_project(axis), ensure_project(eros)
    assert a.ok and e.ok and a.project_id != e.project_id
    assert ensure_project(axis).project_id == a.project_id  # stable across calls
    assert resolve_project(axis).root == axis.resolve()


def test_subfolder_resolves_to_git_root_project(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "src" / "deep").mkdir(parents=True)
    root = ensure_project(tmp_path)
    sub = resolve_project(tmp_path / "src" / "deep")
    assert sub.ok and sub.project_id == root.project_id and sub.root == tmp_path.resolve()


def test_git_repo_without_identity_is_not_attributed_to_a_parent_project(tmp_path):
    MemoryStore.init(tmp_path, "outer")
    inner = tmp_path / "inner-repo"
    inner.mkdir()
    _git_init(inner)
    res = resolve_project(inner)
    assert res.status == "uninitialized" and not res.project_id


def test_invalid_metadata_is_reported_not_overwritten(tmp_path):
    meta = tmp_path / ".cognitive-graph" / "project.json"
    meta.parent.mkdir()
    meta.write_text("{not json", encoding="utf-8")
    assert resolve_project(tmp_path).status == "invalid"
    assert ensure_project(tmp_path).status == "invalid"
    assert meta.read_text(encoding="utf-8") == "{not json"
    meta.write_text(json.dumps({"id": "../evil"}), encoding="utf-8")
    assert resolve_project(tmp_path).status == "invalid"


def test_ensure_project_does_not_create_missing_directories(tmp_path):
    res = ensure_project(tmp_path / "nope")
    assert not res.ok and not (tmp_path / "nope").exists()


# --- structural isolation (no Neo4j needed) -----------------------------------------


class Rec(dict):
    def data(self):
        return dict(self)


class FakeDriver:
    def __init__(self):
        self.calls = []

    def execute_query(self, query, **params):
        self.calls.append((query, params))
        if "RETURN DISTINCT fn.name" in query:
            return [Rec(name="login_user"), Rec(name="save_user")], None, None
        if "COUNT {" in query:
            return [Rec(files=0, functions=0, calls=0)], None, None
        if "RETURN f.path AS path" in query:
            return [Rec(path="app/auth.py")], None, None
        return [], None, None

    def session(self):
        outer = self

        class S:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def execute_write(s, fn):
                class Tx:
                    def run(t, query, **params):
                        outer.calls.append((query, params))
                fn(Tx())

        return S()


def _scoped(pid="proj-aaaaaa"):
    drv = FakeDriver()
    return GraphDatabase("", "", "", pid, _driver=drv), drv


def test_every_project_query_is_filtered_by_project_id():
    db, drv = _scoped()
    fn = FunctionEntity("login_user", "def login_user():\n  pass", 1, 2, ("save_user",))
    db.replace_file("app/auth.py", [fn])
    db.link_calls([{"from_path": "a", "from_name": "b", "from_line": 1, "to_path": "c", "to_name": "d", "to_line": 2}])
    db.fetch_context("how does login_user work")
    db.fetch_context("nothing here")
    db.find_relevant("explain login_user in app/auth.py")
    db.stats()
    db.get_function("app/auth.py", "login_user")
    db.functions_calling({"x"})
    db.functions_named({"x"})
    db.reset()
    assert len(drv.calls) >= 12
    for query, params in drv.calls:
        assert params.get("pid") == "proj-aaaaaa", query
        assert "project_id" in query and "$pid" in query, query


def test_unscoped_handle_cannot_read_or_write_project_data():
    drv = FakeDriver()
    base = GraphDatabase("", "", "", _driver=drv)
    for call in (lambda: base.stats(), lambda: base.fetch_context("x"), lambda: base.find_relevant("x"),
                 lambda: base.replace_file("a.py", []), lambda: base.reset(), lambda: base.get_function("a", "b"),
                 lambda: base.link_calls([{"x": 1}])):
        with pytest.raises(ProjectRequiredError):
            call()
    with pytest.raises(ProjectRequiredError):
        base.scoped("")
    assert drv.calls == []


def test_reset_only_targets_the_active_project():
    db, drv = _scoped("proj-axis1")
    db.reset()
    assert len(drv.calls) == 2
    for query, params in drv.calls:
        assert "DETACH DELETE" in query and "{project_id: $pid}" in query and params == {"pid": "proj-axis1"}
        assert "MATCH (n)" not in query  # never an unfiltered delete


def test_full_ingest_and_single_file_resync_only_touch_the_project(tmp_path):
    from cognitive_graph.code_parser import CodeParser
    from cognitive_graph.ingestor import Ingestor

    source = ["def a():", "    b()", "", "", "def b():", "    pass", ""]
    (tmp_path / "main.py").write_text("\n".join(source), encoding="utf-8")
    db, drv = _scoped("proj-axis1")
    ing = Ingestor(CodeParser(), db)
    ing.ingest_path(tmp_path, root=tmp_path)
    ing.ingest_file(tmp_path, tmp_path / "main.py")
    assert any("MERGE (a)-[r:CALLS]->(b)" in q for q, _ in drv.calls)  # call edges were rebuilt...
    assert len(drv.calls) >= 5
    for query, params in drv.calls:                                    # ...and every statement was scoped
        assert params.get("pid") == "proj-axis1" and "$pid" in query, query


def test_schema_drops_only_the_old_global_constraints():
    drv = FakeDriver()
    GraphDatabase("", "", "", _driver=drv).init_schema()
    stmts = [q for q, _ in drv.calls]
    assert stmts[:2] == ["DROP CONSTRAINT file_path IF EXISTS", "DROP CONSTRAINT function_key IF EXISTS"]
    assert all("project_id" in q for q in stmts[2:]) and len(stmts) == 4


def test_find_relevant_returns_nothing_for_unrelated_prompt():
    db, drv = _scoped()
    assert db.find_relevant("please tidy the readme wording") == []
    assert not any("MATCH (fn:Function {project_id: $pid})\nWHERE fn.name IN" in q for q, _ in drv.calls)


def test_fetch_context_without_fallback_never_returns_everything():
    db, drv = _scoped()
    assert db.fetch_context("nothing relevant", fallback_all=False) == []


# --- live isolation (needs a running Neo4j) -------------------------------------------


LIVE = os.environ.get("COGNITIVE_GRAPH_LIVE_TESTS") == "1"


@pytest.fixture
def live():
    """Opt-in: set COGNITIVE_GRAPH_LIVE_TESTS=1. Connects using .env / NEO4J_* settings, creates the
    per-project schema (dropping only the old global constraints) and writes/deletes nodes whose
    project ids start with 'test-'. Off by default so a plain `pytest` never touches your database."""
    if not LIVE:
        pytest.skip("live Neo4j tests are opt-in: set COGNITIVE_GRAPH_LIVE_TESTS=1")
    try:
        from cognitive_graph.config import Settings

        s = Settings.from_env()
        base = GraphDatabase(s.neo4j_uri, s.neo4j_user, s.neo4j_password, connection_timeout=2)
        base.verify()
        base.init_schema()
    except Exception as exc:
        pytest.skip(f"Neo4j not reachable ({type(exc).__name__})")
    axis, eros = f"test-axis-{uuid.uuid4().hex[:8]}", f"test-eros-{uuid.uuid4().hex[:8]}"
    yield base.scoped(axis), base.scoped(eros)
    base.scoped(axis).reset()
    base.scoped(eros).reset()
    base.close()


def test_live_projects_sharing_a_path_stay_isolated(live):
    axis, eros = live
    axis.replace_file("main.py", [FunctionEntity("charge_invoice", "def charge_invoice():\n    pass", 1, 2)])
    eros.replace_file("main.py", [FunctionEntity("book_flight", "def book_flight():\n    pass", 1, 2)])
    assert [f["name"] for f in axis.fetch_context("charge_invoice")] == ["charge_invoice"]
    assert axis.find_relevant("look at book_flight") == []
    assert [f["name"] for f in eros.find_relevant("look at book_flight")] == ["book_flight"]
    assert axis.stats()["functions"] == 1 and eros.stats()["functions"] == 1
    assert axis.get_function("main.py", "book_flight") == []
    assert axis.functions_named({"book_flight"}) == []
    eros.reset()
    assert eros.stats()["functions"] == 0 and axis.stats()["functions"] == 1


def test_live_unscoped_legacy_nodes_are_invisible(live):
    axis, _ = live
    base_driver = axis._driver
    base_driver.execute_query(
        "CREATE (f:File {path: 'legacy_only.py'}) "
        "CREATE (n:Function {file_path: 'legacy_only.py', name: 'legacy_marker_fn', start_line: 1, "
        "end_line: 2, raw_code: 'def legacy_marker_fn(): pass', calls: []})-[:DEFINED_IN]->(f)")
    try:
        assert axis.fetch_context("legacy_marker_fn", fallback_all=False) == []
        assert axis.functions_named({"legacy_marker_fn"}) == []
        assert axis.stats()["functions"] == 0
    finally:
        base_driver.execute_query("MATCH (n) WHERE n.file_path = 'legacy_only.py' OR n.path = 'legacy_only.py' DETACH DELETE n")
