"""Tests for the project-memory layer (no Neo4j / LLM needed).  Run: pytest tests"""
import pytest

from cognitive_graph.memory import MemoryStore, MemoryStoreError, build_brief
from cognitive_graph.memory_cli import run


@pytest.fixture
def store(tmp_path):
    return MemoryStore.init(tmp_path, "demo")


def test_init_is_idempotent_and_keeps_identity(tmp_path):
    a = MemoryStore.init(tmp_path, "demo")
    b = MemoryStore.init(tmp_path, "other-name")
    assert a.project_id == b.project_id and b.name == "demo"


def test_open_finds_parent_and_errors_without_project(tmp_path, store):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert MemoryStore.open(sub).project_id == store.project_id
    with pytest.raises(MemoryStoreError):
        MemoryStore.open(tmp_path.parent / "nowhere-ever")


def test_add_roundtrip_ids_and_files(store):
    d = store.add("decision", "Use SQLite: simple", "Because of \"reasons\"\nline2", tags=["db"], evidence=["src/db.py"])
    t = store.add("task", "Write tests")
    assert (d.id, t.id) == ("D-0001", "T-0001") and store.add("decision", "Second").id == "D-0002"
    back = store.get("d-0001")
    assert back.title == "Use SQLite: simple" and back.body.startswith('Because of "reasons"')
    assert back.tags == ["db"] and back.evidence == ["src/db.py"] and back.trust == "confirmed"
    assert d.path.read_text(encoding="utf-8").startswith("---\nid: \"D-0001\"")


def test_claude_source_is_proposed_until_confirmed(store):
    i = store.add("fact", "Uses Postgres", source="claude")
    assert i.trust == "proposed"
    assert store.confirm(i.id).trust == "confirmed"


def test_validation(store):
    with pytest.raises(MemoryStoreError):
        store.add("idea", "x")
    with pytest.raises(MemoryStoreError):
        store.add("task", "x", status="finished")
    with pytest.raises(MemoryStoreError):
        store.add("task", "   ")


def test_update_and_forget(store):
    t = store.add("task", "Ship it")
    assert store.update(t.id, status="done").status == "done"
    store.forget(t.id)
    with pytest.raises(MemoryStoreError):
        store.get(t.id)


def test_hand_edited_file_with_plain_values_is_readable(store):
    i = store.add("fact", "Original")
    text = i.path.read_text(encoding="utf-8").replace('title: "Original"', "title: Edited by hand")
    i.path.write_text(text, encoding="utf-8")
    assert store.get(i.id).title == "Edited by hand"


def test_other_projects_items_are_ignored(tmp_path, store):
    other = MemoryStore.init(tmp_path / "other", "other")
    foreign = other.add("fact", "Secret of other project")
    (store.dir / "memory" / "fact").mkdir(parents=True, exist_ok=True)
    (store.dir / "memory" / "fact" / "F-0099-copied.md").write_text(foreign.path.read_text(encoding="utf-8"), encoding="utf-8")
    assert store.items() == [] and any("another project" in w for w in store.warnings)
    assert "Secret" not in build_brief(store, "secret other project")


def test_handoff_creates_tasks_and_decisions_without_duplicates(store):
    store.add("task", "Add auth")
    made = store.save_handoff("Worked on login", done=["Parser"], open_tasks=["Add auth", "Write docs"],
                              decisions=["Use JWT :: stateless"])
    assert [i.type for i in made] == ["handoff", "task", "decision"]
    assert made[2].body == "stateless"
    assert len([i for i in store.items("task")]) == 2


def test_search_ranks_by_relevance(store):
    store.add("decision", "Use Redis for caching", "sessions cache")
    store.add("decision", "Use Postgres for storage")
    hits = store.search("redis cache")
    assert hits and hits[0][1].title.startswith("Use Redis")
    assert store.search("zzzzz") == []


def test_brief_is_selective_labelled_and_bounded(store, tmp_path):
    (tmp_path / "app.py").write_text("x=1")
    store.add("fact", "Flask API for invoices", tags=["overview"])
    store.add("decision", "Use Redis for caching", evidence=["app.py", "gone.py"])
    store.add("decision", "Adopt GraphQL", "unrelated topic")
    store.add("task", "Finish invoice export", source="claude")
    store.add("task", "Old thing", status="done")
    store.save_handoff("Stopped mid-way on export")
    brief = build_brief(store, "caching with redis")
    assert "Flask API" in brief and "Redis" in brief and "Stopped mid-way" in brief
    assert "Adopt GraphQL" not in brief and "Old thing" not in brief
    assert "UNCONFIRMED" in brief and "gone.py (file no longer exists)" in brief
    assert len(build_brief(store, "caching", max_chars=200)) < 300


def test_brief_graph_failure_is_graceful(store, monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")
    assert "code graph unavailable" in build_brief(store, "anything", with_graph=True)


def test_cli_end_to_end(tmp_path, capsys):
    p = str(tmp_path)
    assert run(["--project", p, "init", "--name", "demo"]) == 0
    assert run(["--project", p, "add", "decision", "Use Redis", "--body", "fast", "--tag", "cache"]) == 0
    assert run(["--project", p, "handoff", "--summary", "Did stuff", "--open", "Ship"]) == 0
    assert run(["--project", p, "brief", "redis"]) == 0
    out = capsys.readouterr().out
    assert "D-0001" in out and "T-0001" in out and "Use Redis" in out
    assert run(["--project", p, "forget", "D-9999"]) == 1
    assert run(["--project", str(tmp_path.parent / "no-memory-here"), "list"]) == 1
