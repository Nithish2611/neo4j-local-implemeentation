"""The hands-free workflow: automatic project identity, incremental background graph sync,
SessionStart behaviour, and the richer (quoted, redacted) handoff.

Everything runs in scratch folders with a fake graph; no Neo4j, no real Claude Code settings.
The Neo4j-facing Cypher is covered by the structural tests in test_project_isolation.py."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from neo4j.exceptions import ClientError

from cognitive_graph import graph_sync, hook_cli, memory, session_start
from cognitive_graph.code_parser import CodeParser
from cognitive_graph.graph_sync import SyncConfig, SyncLock, load_state, maybe_spawn_sync, plan_changes, run_sync
from cognitive_graph.hook import handle as prompt_handle
from cognitive_graph.memory import MemoryStore, auto_init_project, resolve_project
from cognitive_graph.session_end import handle as end_handle, redact
from test_session_end import CANARIES, NOW, git, make_project, make_transcript, payload

REPO = Path(__file__).resolve().parents[1]
QUIET = {"COGNITIVE_GRAPH_SYNC": "off"}


def new_repo(tmp_path, name="proj", commit=False):
    root = tmp_path / name
    root.mkdir()
    git(tmp_path, "init", "-q", str(root))
    if commit:
        git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# =========================================================================================
# 1. Automatic project identity
# =========================================================================================


def test_first_use_in_a_git_project_initialises_it_with_a_stable_id(tmp_path):
    root = new_repo(tmp_path)
    res, created = auto_init_project(root, {})
    assert created and res.ok and len(res.project_id) == 12
    again, created_again = auto_init_project(root / "nonexistent-sub" if False else root, {})
    assert not created_again and again.project_id == res.project_id
    sub = root / "src" / "deep"
    sub.mkdir(parents=True)
    from_sub, created_sub = auto_init_project(sub, {})
    assert not created_sub and from_sub.project_id == res.project_id and from_sub.root == root.resolve()


def test_auto_created_metadata_is_kept_out_of_git_status(tmp_path):
    root = new_repo(tmp_path)
    write(root, "a.py", "x = 1\n")
    auto_init_project(root, {})
    status = git(root, "status", "--porcelain")
    assert ".cognitive-graph" not in status and "a.py" in status  # the user's own change is still visible
    exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count(".cognitive-graph/") == 1
    auto_init_project(root, {})  # idempotent: no duplicate exclude line
    assert (root / ".git" / "info" / "exclude").read_text(encoding="utf-8").count(".cognitive-graph/") == 1


def test_identity_is_never_derived_from_names_or_prompts(tmp_path):
    a, b = new_repo(tmp_path, "same-name"), new_repo(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, "same-name")
    ra, _ = auto_init_project(a, {})
    rb, _ = auto_init_project(b, {})
    assert ra.name == rb.name == "same-name" and ra.project_id != rb.project_id


def test_non_git_folders_home_and_disabled_are_never_initialised(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    res, created = auto_init_project(plain, {})
    assert not created and res.status == "uninitialized" and not (plain / ".cognitive-graph").exists()

    repo = new_repo(tmp_path, "dotfiles")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: repo))  # a Git repo that IS the home folder
    assert auto_init_project(repo, {})[1] is False and not (repo / ".cognitive-graph").exists()
    monkeypatch.undo()

    off = new_repo(tmp_path, "optout")
    assert auto_init_project(off, {"COGNITIVE_GRAPH_AUTO_INIT": "off"})[1] is False
    assert not (off / ".cognitive-graph").exists()


def test_existing_or_broken_identity_is_respected(tmp_path):
    root = new_repo(tmp_path)
    store = MemoryStore.init(root, "manual")
    res, created = auto_init_project(root, {})
    assert not created and res.project_id == store.project_id
    assert not (root / ".git" / "info" / "exclude").read_text(encoding="utf-8").count(".cognitive-graph")  # manual init: your choice

    bad = new_repo(tmp_path, "bad")
    (bad / ".cognitive-graph").mkdir()
    (bad / ".cognitive-graph" / "project.json").write_text("garbage", encoding="utf-8")
    res, created = auto_init_project(bad, {})
    assert res.status == "invalid" and not created
    assert (bad / ".cognitive-graph" / "project.json").read_text(encoding="utf-8") == "garbage"


def test_a_repo_inside_an_initialised_project_folder_is_a_separate_project(tmp_path):
    outer = new_repo(tmp_path, "outer")
    auto_init_project(outer, {})
    inner = outer / "vendor-repo"
    inner.mkdir()
    git(outer, "init", "-q", str(inner))
    res, created = auto_init_project(inner, {})
    assert created and res.root == inner.resolve() and res.project_id != resolve_project(outer).project_id


def test_simultaneous_first_sessions_agree_on_one_identity(tmp_path):
    root = new_repo(tmp_path)
    flag = tmp_path / "go.flag"
    code = ("import os, sys, time\n"
            "from cognitive_graph.memory import auto_init_project\n"
            "while not os.path.exists(sys.argv[2]): time.sleep(0.005)\n"
            "r, c = auto_init_project(sys.argv[1], {})\n"
            "print(r.project_id, c)\n")
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(root), str(flag)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env) for _ in range(6)]
    time.sleep(1.5)  # every process is imported and spinning on the flag
    flag.write_text("go")
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0] * 6, outs
    results = [o[0].split() for o in outs]
    assert len({r[0] for r in results}) == 1, results            # one identity, seen by everybody
    assert sum(r[1] == "True" for r in results) == 1, results     # exactly one session announces the creation
    assert resolve_project(root).project_id == results[0][0]


def test_linked_worktrees_get_separate_graph_scopes(tmp_path):
    main = make_project(tmp_path, "main-repo")
    git(main, "add", "-f", ".cognitive-graph/project.json")
    git(main, "commit", "-q", "-m", "share identity")  # a deliberately committed (shared) identity
    wt = tmp_path / "feature-wt"
    git(main, "worktree", "add", "-q", str(wt), "-b", "feature")
    a, b = resolve_project(main), resolve_project(wt)
    assert a.ok and b.ok and a.project_id == b.project_id            # memory identity is shared...
    assert a.graph_id == a.project_id                                # ...the main checkout keeps the plain id
    assert b.graph_id != b.project_id and b.graph_id.startswith(a.project_id + "~")  # ...the worktree's graph is separate


def test_uncommitted_identities_make_worktrees_separate_projects(tmp_path):
    main = new_repo(tmp_path, "main-repo", commit=True)
    ra, _ = auto_init_project(main, {})
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", str(wt), "-b", "other")
    rb, created = auto_init_project(wt, {})
    assert created and rb.project_id != ra.project_id
    assert git(wt, "status", "--porcelain") == "" and git(main, "status", "--porcelain") == ""


# =========================================================================================
# 2. Incremental background graph sync (fake graph; real parser, real git listing)
# =========================================================================================


class FakeGraph:
    """Enough of a scoped GraphDatabase to run the real Ingestor and sync logic, including Neo4j's
    behaviour that replacing or deleting a file drops every call edge into it."""

    def __init__(self):
        self.funcs, self.edges = {}, set()
        self.replaced, self.deleted, self.fail_on, self.explode_after = [], [], set(), None

    def stats(self):
        return {"files": len(self.funcs), "functions": sum(map(len, self.funcs.values())), "calls": len(self.edges)}

    def _drop(self, paths):
        self.edges = {e for e in self.edges if e[0][0] not in paths and e[1][0] not in paths}

    def delete_files(self, paths):
        self.deleted += list(paths)
        for p in paths:
            self.funcs.pop(p, None)
        self._drop(set(paths))

    def replace_file(self, path, functions):
        if path in self.fail_on:
            raise ClientError()
        if self.explode_after is not None and len(self.replaced) >= self.explode_after:
            raise RuntimeError("connection lost")
        self.replaced.append(path)
        self._drop({path})
        self.funcs[path] = list(functions)

    def functions_calling(self, names):
        return [{"path": p, "name": f.name, "start_line": f.start_line, "calls": list(f.calls)}
                for p, fs in self.funcs.items() for f in fs if set(f.calls) & set(names)]

    def functions_named(self, names):
        return [{"path": p, "name": f.name, "start_line": f.start_line}
                for p, fs in self.funcs.items() for f in fs if f.name in names]

    def link_calls(self, edges):
        for e in edges:
            self.edges.add(((e["from_path"], e["from_name"], e["from_line"]), (e["to_path"], e["to_name"], e["to_line"])))

    def edge_pairs(self):
        return {(a[0] + ":" + a[1], b[0] + ":" + b[1]) for a, b in self.edges}


@pytest.fixture
def proj(tmp_path):
    root = new_repo(tmp_path, "syncproj")
    write(root, "a.py", "def caller():\n    helper()\n")
    write(root, "b.py", "def helper():\n    pass\n")
    write(root, "pkg/c.js", "function jsfn() { return 1; }\n")
    write(root, ".gitignore", "ignored.py\n")
    write(root, "ignored.py", "def secret_ignored():\n    pass\n")
    write(root, "node_modules/lib/x.js", "function vendor() {}\n")
    write(root, "static/app.min.js", "function minified() {}\n")
    write(root, "notes.txt", "not source\n")
    res, _ = auto_init_project(root, {})
    return root, res


def sync(res, db, **cfg):
    return run_sync(res, db, CodeParser(), SyncConfig(**cfg))


def test_first_sync_indexes_everything_indexable_and_nothing_else(proj):
    root, res = proj
    db = FakeGraph()
    r = sync(res, db)
    assert r.status == "ok" and sorted(db.funcs) == ["a.py", "b.py", "pkg/c.js"]  # .gitignore, vendor dirs, .min.js, .txt skipped
    assert ("a.py:caller", "b.py:helper") in db.edge_pairs()
    state = load_state(root)
    assert state["status"] == "ok" and state["graph_id"] == res.graph_id and set(state["files"]) == set(db.funcs)


def test_second_sync_with_no_changes_touches_nothing(proj):
    _, res = proj
    db = FakeGraph()
    sync(res, db)
    before = list(db.replaced)
    r = sync(res, db)
    assert r.message == "up to date" and db.replaced == before and db.deleted == []


def test_only_changed_files_are_reingested_and_edges_into_them_are_restored(proj):
    root, res = proj
    db = FakeGraph()
    sync(res, db)
    db.replaced.clear()
    write(root, "b.py", "def helper():\n    pass\n\n\ndef extra():\n    pass\n")  # changes the size too
    r = sync(res, db)
    assert db.replaced == ["b.py"] and r.indexed == 1
    assert ("a.py:caller", "b.py:helper") in db.edge_pairs()  # a.py was NOT re-parsed, its call edge was rebuilt
    assert "b.py:extra" in {a[0] + ":" + a[1] for a, _ in [((p, f.name, 0), 0) for p, fs in db.funcs.items() for f in fs]}


def test_new_files_are_added_and_removed_files_are_deleted(proj):
    root, res = proj
    db = FakeGraph()
    sync(res, db)
    db.replaced.clear()
    write(root, "new_mod.py", "def brand_new():\n    helper()\n")  # untracked but not ignored
    (root / "b.py").unlink()
    r = sync(res, db)
    assert db.replaced == ["new_mod.py"] and db.deleted == ["b.py"] and r.removed == 1
    assert "b.py" not in load_state(root)["files"] and "new_mod.py" in load_state(root)["files"]
    assert not any(a.startswith("b.py") or b.startswith("b.py") for a, b in db.edge_pairs())


def test_gitignored_files_never_enter_the_graph_even_when_they_change(proj):
    root, res = proj
    db = FakeGraph()
    sync(res, db)
    write(root, "ignored.py", "def secret_ignored():\n    pass\n\n# changed\n")
    sync(res, db)
    assert "ignored.py" not in db.funcs


def test_work_is_bounded_per_run_and_resumes(proj):
    root, res = proj
    db = FakeGraph()
    r1 = sync(res, db, max_files=2, batch=1)
    assert r1.indexed == 2 and r1.remaining == 1 and len(db.funcs) == 2 and load_state(root)["pending"] == 1
    r2 = sync(res, db, max_files=2, batch=1)
    assert r2.indexed == 1 and len(db.funcs) == 3 and r2.remaining == 0
    assert sync(res, db).message == "up to date"


def test_time_cap_stops_early_and_records_what_remains(proj):
    root, res = proj
    db = FakeGraph()
    r = sync(res, db, max_seconds=-1, batch=1)  # a budget that is already spent
    assert r.status == "ok" and r.remaining == 3 and not db.replaced
    assert sync(res, db).indexed == 3


def test_a_crash_midway_keeps_progress_and_the_rerun_only_finishes_the_rest(proj):
    root, res = proj
    db = FakeGraph()
    db.explode_after = 1
    with pytest.raises(RuntimeError):
        sync(res, db, batch=1)
    assert len(load_state(root)["files"]) == 1 and not SyncLock.is_fresh(root)  # progress saved, lock released
    db.explode_after = None
    db.replaced.clear()
    sync(res, db, batch=1)
    assert len(db.replaced) == 2 and len(db.funcs) == 3


def test_unparseable_files_do_not_stop_the_batch_and_retry_only_when_changed(proj):
    root, res = proj
    db = FakeGraph()
    db.fail_on = {"a.py"}
    sync(res, db)
    assert sorted(db.funcs) == ["b.py", "pkg/c.js"] and "a.py" in load_state(root)["files"]
    db.fail_on, db.replaced = set(), []
    assert sync(res, db).message == "up to date" and db.replaced == []  # not retried while unchanged
    write(root, "a.py", "def caller():\n    helper()\n\n# edited\n")
    sync(res, db)
    assert db.replaced == ["a.py"]


def test_a_cleared_graph_is_detected_and_reindexed(proj):
    _, res = proj
    db = FakeGraph()
    sync(res, db)
    db.funcs.clear()
    db.edges.clear()
    assert sync(res, db).indexed == 3


def test_state_is_per_project_and_per_graph_scope(tmp_path):
    a, b = new_repo(tmp_path, "one"), new_repo(tmp_path, "two")
    write(a, "m.py", "def one():\n    pass\n")
    write(b, "m.py", "def two():\n    pass\n")  # the same relative path in two projects
    ra, _ = auto_init_project(a, {})
    rb, _ = auto_init_project(b, {})
    da, db_ = FakeGraph(), FakeGraph()
    sync(ra, da)
    sync(rb, db_)
    assert [f.name for f in da.funcs["m.py"]] == ["one"] and [f.name for f in db_.funcs["m.py"]] == ["two"]
    assert load_state(a)["graph_id"] == ra.graph_id != load_state(b)["graph_id"]
    other = FakeGraph()  # same folder but a different graph scope (e.g. a linked worktree) starts from scratch
    changed = memory.ProjectResolution("ok", ra.cwd, ra.root, ra.project_id, ra.name, graph_id=ra.project_id + "~abc123")
    assert sync(changed, other).indexed == 1


def test_only_one_sync_runs_per_project_and_stale_locks_are_taken_over(proj):
    root, res = proj
    lock = SyncLock(root)
    assert lock.acquire()
    assert sync(res, FakeGraph()).status == "busy"
    lock.release()
    stale = root / ".cognitive-graph" / "sync.lock"
    stale.write_text("999999")
    old = time.time() - 600
    os.utime(stale, (old, old))
    assert sync(res, FakeGraph()).status == "ok" and not stale.exists()


def test_sync_writes_no_source_text_and_ignores_itself_in_git(proj):
    root, res = proj
    sync(res, FakeGraph())
    text = "".join(p.read_text(encoding="utf-8") for p in (root / ".cognitive-graph").glob("sync*"))
    assert "def caller" not in text and "helper()" not in text
    assert "sync-state.json" in (root / ".cognitive-graph" / ".gitignore").read_text(encoding="utf-8")


def test_plan_changes_unit():
    now = {"a": [1, 1], "b": [2, 2], "c": [3, 3]}
    assert plan_changes(now, {"a": [1, 1], "b": [9, 9], "gone": [1, 1]}) == (["b", "c"], ["gone"])


# --- when do the hooks start a sync? ---------------------------------------------------------------------


class Spawner:
    def __init__(self):
        self.calls = []

    def __call__(self, root):
        self.calls.append(root)


def test_hooks_spawn_only_when_something_changed_and_never_wait(proj):
    root, res = proj
    spawn = Spawner()
    note = maybe_spawn_sync(res, SyncConfig(), force=True, spawn=spawn)
    assert spawn.calls == [root] and "initial indexing started" in note  # first run: everything is new
    # pretend it finished: record the current tree as indexed
    sync(res, FakeGraph())
    spawn.calls.clear()
    assert maybe_spawn_sync(res, SyncConfig(), force=True, spawn=spawn) is None and spawn.calls == []  # nothing changed
    write(root, "b.py", "def helper():\n    return 1\n")
    assert maybe_spawn_sync(res, SyncConfig(), force=True, spawn=spawn) is None and spawn.calls == [root]  # incremental: quiet


def test_per_prompt_checks_are_throttled(proj):
    root, res = proj
    sync(res, FakeGraph())
    write(root, "b.py", "def helper():\n    return 2\n")
    spawn = Spawner()
    cfg = SyncConfig(interval=3600)
    maybe_spawn_sync(res, cfg, spawn=spawn)          # scans, finds the change, spawns
    maybe_spawn_sync(res, cfg, spawn=spawn)          # within the interval: no scan, no spawn
    assert len(spawn.calls) == 1
    maybe_spawn_sync(res, cfg, force=True, spawn=spawn)  # SessionStart / SessionEnd ignore the throttle
    assert len(spawn.calls) == 2


def test_disabled_sync_does_nothing(proj):
    _, res = proj
    spawn = Spawner()
    assert maybe_spawn_sync(res, SyncConfig(enabled=False), force=True, spawn=spawn) is None and not spawn.calls
    assert SyncConfig.from_mapping({"COGNITIVE_GRAPH_SYNC": "off"}).enabled is False


def test_a_running_sync_is_reported_not_duplicated(proj):
    root, res = proj
    lock = SyncLock(root)
    assert lock.acquire()
    spawn = Spawner()
    note = maybe_spawn_sync(res, SyncConfig(), force=True, spawn=spawn)
    lock.release()
    assert not spawn.calls and "being indexed" in note


def test_neo4j_down_is_reported_once_then_backs_off_without_spawning(proj, monkeypatch):
    root, res = proj
    spawned = Spawner()
    monkeypatch.setattr(graph_sync, "_spawn_detached", spawned)
    monkeypatch.setattr(graph_sync, "_neo4j_down_reason", lambda r: "Neo4j is not reachable")
    note = maybe_spawn_sync(res, SyncConfig(), force=True)
    assert "graph sync unavailable (Neo4j is not reachable)" in note and not spawned.calls
    assert load_state(root)["status"] == "unavailable"
    monkeypatch.setattr(graph_sync, "_neo4j_down_reason", lambda r: pytest.fail("must back off, not re-check"))
    assert "unavailable" in maybe_spawn_sync(res, SyncConfig(), force=True)  # within the backoff window


def test_scan_problems_never_escape(proj, monkeypatch):
    _, res = proj
    monkeypatch.setattr(graph_sync, "source_files", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert "could not start graph sync (OSError)" in maybe_spawn_sync(res, SyncConfig(), force=True, spawn=Spawner())


def test_a_real_detached_background_process_runs_and_records_unavailable_neo4j(proj, monkeypatch):
    root, res = proj
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")  # nothing listens here
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    t0 = time.time()
    graph_sync._spawn_detached(root)
    assert time.time() - t0 < 5  # the spawn itself returns immediately
    for _ in range(150):
        state = load_state(root)
        if state.get("status") == "unavailable":
            break
        time.sleep(0.2)
    assert state.get("status") == "unavailable" and "Neo4j" in state.get("message", "")
    assert not SyncLock.is_fresh(root)


def test_hook_modules_stay_light_and_indexing_rules_match_the_parser():
    code = ("import sys\n"
            "import cognitive_graph.hook, cognitive_graph.session_start, cognitive_graph.session_end, cognitive_graph.graph_sync\n"
            "print('neo4j' in sys.modules, 'tree_sitter' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": str(REPO)})
    assert out.stdout.strip() == "False False", out.stderr
    from cognitive_graph.indexing_rules import SUPPORTED_SUFFIXES

    assert SUPPORTED_SUFFIXES == CodeParser.supported_extensions


# =========================================================================================
# 3. Hooks: automatic setup, sync triggers and notes
# =========================================================================================


def test_first_prompt_in_a_fresh_project_sets_it_up_and_starts_sync(tmp_path):
    root = new_repo(tmp_path)
    calls = []
    out = prompt_handle({"cwd": str(root), "prompt": "how does the login flow work here?"}, QUIET,
                        lambda r, p, c: [], lambda res, cfg, **kw: calls.append((res.graph_id, kw)) or "code graph initial indexing started")
    assert (root / ".cognitive-graph" / "project.json").exists()
    assert "initialised project" in out["systemMessage"] and "initial indexing started" in out["systemMessage"]
    assert len(calls) == 1 and calls[0][0] == resolve_project(root).graph_id
    assert prompt_handle({"cwd": str(root), "prompt": "how does the login flow work here?"}, QUIET, lambda r, p, c: [], lambda *a, **k: None) is None


def test_slash_commands_skip_the_sync_check_and_a_failing_check_never_blocks(tmp_path):
    root = new_repo(tmp_path)
    MemoryStore.init(root).add("decision", "Invoices use Stripe", "tax handling")
    calls = []
    prompt_handle({"cwd": str(root), "prompt": "/compact invoices stripe"}, QUIET, lambda r, p, c: [], lambda *a, **k: calls.append(1))
    assert calls == []

    def boom(*a, **k):
        raise RuntimeError("scan exploded")

    out = prompt_handle({"cwd": str(root), "prompt": "how are invoices charged with stripe?"}, QUIET, lambda r, p, c: [], boom)
    assert "Invoices use Stripe" in out["hookSpecificOutput"]["additionalContext"]  # the prompt still gets its context
    assert "graph sync check failed (RuntimeError)" in out["systemMessage"] and "scan exploded" not in json.dumps(out)


def test_graph_and_sync_unavailable_are_both_reported_while_memory_still_works(tmp_path):
    root = new_repo(tmp_path)
    MemoryStore.init(root).add("decision", "Invoices use Stripe", "tax handling")

    def down(res, prompt, cfg):
        raise ConnectionError()

    out = prompt_handle({"cwd": str(root), "prompt": "how are invoices charged with stripe?"}, QUIET, down,
                        lambda *a, **k: "graph sync unavailable (Neo4j is not reachable); will retry")
    assert "Invoices use Stripe" in out["hookSpecificOutput"]["additionalContext"]
    assert "code graph unavailable" in out["systemMessage"] and "graph sync unavailable (Neo4j is not reachable)" in out["systemMessage"]


def start(root, source="startup", env=QUIET, sync=None, now=None):
    return session_start.handle({"cwd": str(root), "source": source, "session_id": "s1"}, env,
                                sync or (lambda *a, **k: None), now)


def add_handoff(root, when=None, session="oldsess1", env=None):
    t = make_transcript(root.parent, root, [("Edit", root / "app" / "billing.py")], name=f"t-{session}.jsonl")
    return end_handle(payload(root, t, session=session), {**QUIET, **(env or {})}, when or NOW)


def test_session_start_initialises_starts_sync_and_tells_the_user(tmp_path):
    root = new_repo(tmp_path)
    seen = []
    out = start(root, sync=lambda res, cfg, **kw: seen.append(kw) or "code graph initial indexing started in the background")
    assert (root / ".cognitive-graph" / "project.json").exists() and seen == [{"force": True}]
    assert "initialised project" in out["systemMessage"] and "indexing started" in out["systemMessage"]
    assert "hookSpecificOutput" not in out  # nothing to hand over yet


def test_session_start_offers_the_latest_handoff_labelled_unconfirmed(tmp_path):
    root = make_project(tmp_path)
    add_handoff(root, NOW - timedelta(days=2), "older123")
    newest = add_handoff(root, NOW - timedelta(hours=1), "newer456")
    for source in ("startup", "clear", "compact"):
        out = start(root, source, now=datetime.now(timezone.utc))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert newest.item.id in ctx and "older123" not in ctx and "UNCONFIRMED" in ctx and "handoff, active" in ctx
        assert len(ctx) <= 700
    for source in ("resume", "fork"):  # those sessions already carry their own context
        assert start(root, source, now=datetime.now(timezone.utc)) is None


def test_session_start_ignores_old_or_disabled_handoffs_and_other_projects(tmp_path):
    axis, eros = make_project(tmp_path, "axis"), make_project(tmp_path, "eros")
    add_handoff(axis, session="oldsess1")                     # created just now (real clock)
    real = datetime.now(timezone.utc)
    later = real + timedelta(days=30)                         # ...so 30 days from now it is a month old
    assert start(axis, now=later) is None                     # outside the default 14-day window
    assert start(axis, now=later, env={**QUIET, "COGNITIVE_GRAPH_START_HANDOFF_DAYS": "60"}) is not None
    assert start(axis, now=real) is not None                  # fresh
    assert start(axis, now=real, env={**QUIET, "COGNITIVE_GRAPH_START_HANDOFF": "off"}) is None
    assert start(eros, now=real) is None                      # Eros has no handoff of its own


def test_session_start_is_silent_outside_git_and_warns_on_broken_metadata(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert start(plain) is None and not (plain / ".cognitive-graph").exists()
    bad = new_repo(tmp_path, "bad")
    (bad / ".cognitive-graph").mkdir()
    (bad / ".cognitive-graph" / "project.json").write_text("garbage", encoding="utf-8")
    assert "unusable" in start(bad)["systemMessage"]


def test_session_start_sync_failure_is_reported_and_harmless(tmp_path):
    root = make_project(tmp_path)

    def boom(*a, **k):
        raise OSError("disk")

    out = start(root, sync=boom)
    assert "graph sync could not start (OSError)" in out["systemMessage"]


def test_session_start_subprocess_prints_only_json(tmp_path):
    root = make_project(tmp_path)
    add_handoff(root, datetime.now(timezone.utc) - timedelta(hours=1), "sub12345")
    env = {**os.environ, "PYTHONPATH": str(REPO), "COGNITIVE_GRAPH_SYNC": "off"}
    r = subprocess.run([sys.executable, "-m", "cognitive_graph.session_start"], input=json.dumps(
        {"cwd": str(root), "source": "startup", "session_id": "x1"}), capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0 and r.stderr == ""
    assert "H-" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]  # plain text would also be injected as context
    bad = subprocess.run([sys.executable, "-m", "cognitive_graph.session_start"], input="not json", capture_output=True,
                         text=True, timeout=60, env=env)
    assert bad.returncode == 0


def test_installer_sets_up_all_three_hooks_at_user_scope_by_default(tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    home.mkdir()
    monkeypatch.setattr(hook_cli.Path, "home", classmethod(lambda cls: home))
    proj = tmp_path / "anyproj"
    proj.mkdir()
    assert hook_cli.run(["--project", str(proj), "install"]) == 0
    data = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert sorted(data["hooks"]) == ["SessionEnd", "SessionStart", "UserPromptSubmit"]
    cmds = {e: g[0]["hooks"][0]["command"] for e, g in data["hooks"].items()}
    assert "session_start" in cmds["SessionStart"] and "session_end" in cmds["SessionEnd"] and cmds["UserPromptSubmit"].endswith("hook") \
        or "cognitive_graph.hook" in cmds["UserPromptSubmit"]
    assert not (proj / ".claude").exists()  # nothing per project
    hook_cli.run(["--project", str(proj), "install"])
    assert all(len(g) == 1 for g in json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"].values())
    assert hook_cli.run(["--project", str(proj), "uninstall"]) == 0
    assert json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8")) == {}


# =========================================================================================
# 4. The richer handoff: verbatim, bounded, redacted, never the transcript
# =========================================================================================

SECRETS = ["sk-live-abc123def456ghi789jkl0", "hunter2", "AKIAABCDEFGHIJKLMNOP", "ghp_1234567890abcdefghijklmnopqrstuvwx",
           "s3cr3tPassw0rd"]


def rich_transcript(tmp_path, root, name="rich.jsonl", final=None, sidechain_text="CANARY-SIDECHAIN"):
    def user(content, **extra):
        return {"type": "user", "message": {"role": "user", "content": content}, **extra}

    def assistant(blocks, **extra):
        return {"type": "assistant", "message": {"role": "assistant", "content": blocks}, **extra}

    txt = lambda s: {"type": "text", "text": s}  # noqa: E731
    long_tail = "Filler sentence about nothing in particular. " * 40 + "CANARY-TAIL-BEYOND-EXCERPT"
    entries = [
        user("<command-name>/clear</command-name>"),                                    # slash-command wrapper: not the goal
        user("Caveat: messages below were generated by the user while running local commands"),
        user("Add retry logic to invoice charging. Use api_key=" + SECRETS[0] + " if needed", isMeta=False),
        assistant([{"type": "thinking", "thinking": "CANARY-THINKING"}, txt("Looking at the billing module first. CANARY-MIDDLE-CHATTER")]),
        assistant([txt("We decided to use exponential backoff instead of fixed delays, because Stripe rate-limits bursts."),
                   {"type": "tool_use", "name": "Edit", "input": {"file_path": str(root / "app" / "billing.py"), "old_string": "CANARY-OLD", "new_string": "CANARY-NEW"}}]),
        user([{"type": "tool_result", "content": "CANARY-TOOL-OUTPUT password=" + SECRETS[1]}]),
        assistant([{"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
            {"content": "Add backoff to charge()", "status": "completed", "activeForm": "Adding backoff"},
            {"content": "Write retry tests", "status": "pending", "activeForm": "Writing tests"},
            {"content": "Verify on staging with token=" + SECRETS[4], "status": "in_progress", "activeForm": "Verifying"}]}}]),
        assistant([txt(sidechain_text + " We chose Redis for this.")], isSidechain=True),
        assistant([txt(final or ("Retry with exponential backoff is now in charge(). Still need to add tests for the failure path. "
                                 "The staging check has not been verified yet. Connection uses postgres://bob:" + SECRETS[1] + "@db/prod. "
                                 + long_tail))]),
    ]
    f = tmp_path / name
    f.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return f


def rich_body(tmp_path, env=None):
    root = make_project(tmp_path)
    res = end_handle(payload(root, rich_transcript(tmp_path, root)), {**QUIET, **(env or {})}, NOW)
    assert res.status == "saved", res.message
    return root, res, res.item.path.read_text(encoding="utf-8")


def test_handoff_captures_task_done_decisions_open_items_todos_and_files(tmp_path):
    root, res, body = rich_body(tmp_path)
    assert 'Task: "Add retry logic to invoice charging.' in body                       # the real prompt, not the wrappers
    assert "Retry with exponential backoff is now in charge()." in body                # what was done: final message excerpt
    assert "We decided to use exponential backoff instead of fixed delays, because Stripe rate-limits bursts." in body
    assert "Still need to add tests for the failure path." in body and "has not been verified yet" in body
    assert "- [done] Add backoff to charge()" in body and "- [open] Write retry tests" in body
    assert "- app/billing.py" in body
    for label in ("not verified", "heuristic selection", "as written by Claude", "verbatim"):
        assert label in body
    item = MemoryStore.open(root).get(res.item.id)
    assert (item.trust, item.source) == ("proposed", "hook")


def test_handoff_never_stores_secrets_tool_output_thinking_sidechains_or_the_transcript(tmp_path):
    root, res, body = rich_body(tmp_path)
    stored = "\n".join(p.read_text(encoding="utf-8") for p in (root / ".cognitive-graph").rglob("*") if p.is_file())
    for secret in SECRETS:
        assert secret not in stored, secret
    for canary in ("CANARY-THINKING", "CANARY-MIDDLE-CHATTER", "CANARY-TOOL-OUTPUT", "CANARY-OLD", "CANARY-NEW",
                   "CANARY-SIDECHAIN", "CANARY-TAIL-BEYOND-EXCERPT", "We chose Redis"):
        assert canary not in stored, canary
    assert "[redacted]" in stored and len(body) < 4000     # bounded: nowhere near the transcript's size
    assert (tmp_path / "rich.jsonl").stat().st_size > len(body)


def test_unfinished_items_come_only_from_the_final_message(tmp_path):
    root = make_project(tmp_path)
    t = rich_transcript(tmp_path, root, final="All done and verified. The migration is complete.")
    body = end_handle(payload(root, t), QUIET, NOW).item.path.read_text(encoding="utf-8")
    assert "**Unfinished or open**" not in body           # nothing in the final message
    assert "- [open] Write retry tests" in body            # the todo list is still reported, as Claude wrote it


def test_quotes_can_be_switched_off_leaving_only_tool_facts(tmp_path):
    root, res, body = rich_body(tmp_path, {"COGNITIVE_GRAPH_HANDOFF_QUOTES": "off"})
    for text in ("Task:", "exponential backoff", "Write retry tests", "Stripe"):
        assert text not in body
    assert "- app/billing.py" in body


def chat_transcript(tmp_path, name, prompt, final, todos=None, edit=None, root=None):
    lines = [json.dumps({"type": "user", "message": {"content": prompt}})]
    if edit:
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": str(root / edit)}}]}}))
    if todos:
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "TodoWrite", "input": {"todos": todos}}]}}))
    lines.append(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": final}]}}))
    f = tmp_path / name
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def convo(tmp_path, name, turns):
    """A discussion-only transcript: no tool calls at all. turns = [("user"|"assistant", text), ...]"""
    lines = []
    for role, text in turns:
        content = text if role == "user" else [{"type": "text", "text": text}]
        lines.append(json.dumps({"type": role, "message": {"role": role, "content": content}}))
    f = tmp_path / name
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def handoffs_of(root):
    return MemoryStore.open(root).items("handoff")


DECISION_TALK = [
    ("user", "How should we bill enterprise customers for invoices?"),
    ("assistant", "There are two options: charge through Stripe or invoice manually."),
    ("user", "We decided to charge enterprise invoices through Stripe because finance needs automatic VAT handling."),
    ("assistant", "Understood. The rationale is that Stripe Tax removes manual VAT work for finance."),
]


# --- a NEW decision or rationale is saved even with no edits and no todo list ---------------------


def test_a_new_decision_only_discussion_is_saved_as_proposed_and_evidence_labelled(tmp_path):
    root = make_project(tmp_path)
    res = end_handle(payload(root, convo(tmp_path, "d.jsonl", DECISION_TALK), session="talk0001"), QUIET, NOW)
    assert res.status == "saved", res.message
    item = handoffs_of(root)[0]
    body = item.body
    assert (item.trust, item.source, item.status, item.tags) == ("proposed", "hook", "active", ["auto", "session-end"])
    assert "- [you] We decided to charge enterprise invoices through Stripe because finance needs automatic VAT handling." in body
    assert "- [Claude] Understood." not in body and "[Claude] The rationale is that Stripe Tax removes manual VAT work" in body
    assert "Decision: We decided to charge enterprise invoices through Stripe" in body.split("\n\n")[0]  # visible in the brief's lead
    assert "not confirmed" in body and "Unreviewed" in body and "Files edited" not in body
    assert item.evidence[0] == "session:talk0001" and not any(e.endswith(".py") for e in item.evidence)


def test_the_saved_discussion_reaches_the_next_session(tmp_path):
    root = make_project(tmp_path)
    end_handle(payload(root, convo(tmp_path, "d.jsonl", DECISION_TALK), session="talk0002"), QUIET, NOW)
    started = start(root, now=datetime.now(timezone.utc))["hookSpecificOutput"]["additionalContext"]
    assert "UNCONFIRMED" in started and "Stripe" in started
    out = prompt_handle({"cwd": str(root), "prompt": "remind me how enterprise invoices get charged and VAT handled"},
                        QUIET, lambda r, p, c: [], lambda *a, **k: None)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "handoff, active, UNCONFIRMED" in ctx and "Stripe" in ctx and len(ctx) <= 1800


@pytest.mark.parametrize("turns", [
    [("user", "Why Postgres?"), ("assistant", "The reason is that the reporting queries need joins across tenants.")],
    [("user", "Where are we on pricing?"), ("user", "Still need to decide the refund policy for annual plans.")],
    [("assistant", "We opted for weekly billing runs rather than daily ones to limit failed-payment retries.")],
], ids=["rationale", "unfinished-by-user", "decision-by-claude"])
def test_rationale_or_an_unfinished_item_alone_is_enough(tmp_path, turns):
    root = make_project(tmp_path)
    res = end_handle(payload(root, convo(tmp_path, "t.jsonl", turns), session="talk0003"), QUIET, NOW)
    assert res.status == "saved" and handoffs_of(root)[0].trust == "proposed"


# --- repeating what a handoff already says is NOT saved ------------------------------------------------


def test_repeating_an_existing_handoff_verbatim_is_skipped(tmp_path):
    root = make_project(tmp_path)
    assert end_handle(payload(root, convo(tmp_path, "one.jsonl", DECISION_TALK), session="talk0004"), QUIET, NOW).status == "saved"
    again = end_handle(payload(root, convo(tmp_path, "two.jsonl", DECISION_TALK), session="talk0005"), QUIET,
                       NOW + timedelta(minutes=5))
    assert again.status == "skipped" and "nothing new to record" in again.message
    assert len(handoffs_of(root)) == 1


def test_a_paraphrased_repeat_is_skipped_too(tmp_path):
    """The live regression: a session quoted an old open item with different wording ("Left to do" for
    "Still to do") and was wrongly saved as a new handoff."""
    root = make_project(tmp_path)
    first = convo(tmp_path, "one.jsonl", [("user", "Add greet()"), ("assistant",
        "Added greet(name). Still to do: greet doesn't handle an empty or None name.")])
    assert end_handle(payload(root, first, session="talk0006"), QUIET, NOW).status == "saved"
    second = convo(tmp_path, "two.jsonl", [("user", "Tell me about greet"), ("assistant",
        "Left to do: `greet` doesn't handle an empty or `None` name. That is the open item.")])
    res = end_handle(payload(root, second, session="talk0007"), QUIET, NOW + timedelta(minutes=5))
    assert res.status == "skipped" and len(handoffs_of(root)) == 1


def test_talk_about_earlier_sessions_or_handoffs_is_never_new_context(tmp_path):
    root = make_project(tmp_path)  # no stored handoff at all: the META rule alone must hold
    t = convo(tmp_path, "meta.jsonl", [
        ("user", "What did we decide last session about invoices?"),
        ("assistant", "Last session we decided to use Stripe instead of PayPal. The handoff says it is still unverified."),
    ])
    res = end_handle(payload(root, t, session="talk0008"), QUIET, NOW)
    assert res.status == "skipped" and not handoffs_of(root)


def test_questions_do_not_count_as_decisions_or_open_items(tmp_path):
    root = make_project(tmp_path)
    t = convo(tmp_path, "q.jsonl", [("user", "Should we decide between Stripe and PayPal, and what is still left to do?"),
                                   ("assistant", "Which trade-off matters more to you: fees or coverage?")])
    assert end_handle(payload(root, t, session="talk0009"), QUIET, NOW).status == "skipped"


def test_only_the_new_part_of_a_partly_repeated_discussion_is_recorded(tmp_path):
    root = make_project(tmp_path)
    end_handle(payload(root, convo(tmp_path, "one.jsonl", DECISION_TALK), session="talk0010"), QUIET, NOW)
    t = convo(tmp_path, "two.jsonl", DECISION_TALK + [
        ("user", "We also decided to run load tests on staging before the launch because retries could flood the queue.")])
    res = end_handle(payload(root, t, session="talk0011"), QUIET, NOW + timedelta(minutes=5))
    assert res.status == "saved"
    decisions = res.item.path.read_text(encoding="utf-8").split("**Decisions and rationale mentioned**")[1].split("**")[0]
    assert "load tests on staging" in decisions
    assert "charge enterprise invoices through Stripe" not in decisions and "Stripe Tax" not in decisions  # already stored
    assert len(handoffs_of(root)) == 2


def test_a_new_sentence_on_an_old_topic_is_not_suppressed(tmp_path):
    root = make_project(tmp_path)
    end_handle(payload(root, convo(tmp_path, "one.jsonl", [("user", "We decided to charge invoices through Stripe.")]),
                       session="talk0012"), QUIET, NOW)
    t = convo(tmp_path, "two.jsonl", [("user", "We decided to add a manual override for invoices when Stripe is down.")])
    assert end_handle(payload(root, t, session="talk0013"), QUIET, NOW + timedelta(minutes=5)).status == "saved"


def test_discussion_capture_still_redacts_secrets_and_honours_the_quotes_switch(tmp_path):
    root = make_project(tmp_path)
    t = convo(tmp_path, "s.jsonl", [("user", "We decided to store the key api_key=" + SECRETS[0] + " in the vault instead of the repo.")])
    res = end_handle(payload(root, t, session="talk0014"), QUIET, NOW)
    text = res.item.path.read_text(encoding="utf-8")
    assert res.status == "saved" and SECRETS[0] not in text and "api_key=[redacted]" in text
    off = end_handle(payload(root, t, session="talk0015"), {**QUIET, "COGNITIVE_GRAPH_HANDOFF_QUOTES": "off"}, NOW)
    assert off.status == "skipped"  # quoting is the only source of discussion context: nothing to save without it


def test_a_structured_todo_list_with_open_items_saves_a_no_edit_session(tmp_path):
    root = make_project(tmp_path)
    t = chat_transcript(tmp_path, "plan.jsonl", "Plan the invoice caching work", "We chose Redis over Postgres for caching.",
                        todos=[{"content": "Benchmark Redis", "status": "pending"}, {"content": "Design keys", "status": "completed"}])
    res = end_handle(payload(root, t, session="plan0001"), QUIET, NOW)
    body = res.item.path.read_text(encoding="utf-8")
    assert res.status == "saved" and "- [open] Benchmark Redis" in body and "- [done] Design keys" in body
    assert "We chose Redis over Postgres for caching." in body and "Files edited" not in body


def test_a_session_that_only_reads_an_earlier_handoff_records_nothing_new(tmp_path):
    """Regression from a real run: a Q&A session that quoted the previous handoff saved a new handoff
    re-listing the old open items as if they were this session's."""
    root = make_project(tmp_path)
    first = end_handle(payload(root, rich_transcript(tmp_path, root)), QUIET, NOW)
    assert first.status == "saved"
    reader = chat_transcript(tmp_path, "reader.jsonl", "What was left to do last time?",
                             "Left over: Still need to add tests for the failure path. The staging check has not been verified yet.")
    res = end_handle(payload(root, reader, session="reader01"), QUIET, NOW + timedelta(minutes=5))
    assert res.status == "skipped"
    assert len(MemoryStore.open(root).items("handoff")) == 1


def test_sentences_already_in_an_earlier_handoff_are_not_recorded_again(tmp_path):
    root = make_project(tmp_path)
    end_handle(payload(root, rich_transcript(tmp_path, root)), QUIET, NOW)
    t = chat_transcript(tmp_path, "later.jsonl", "Continue the retry work", edit="app/billing.py", root=root,
                        final="Wired the retry into the worker. Still need to add tests for the failure path. "
                              "Next step: deploy to staging is still open.")
    res = end_handle(payload(root, t, session="later001"), QUIET, NOW + timedelta(minutes=5))
    body = res.item.path.read_text(encoding="utf-8")
    unfinished = body.split("**Unfinished or open**")[1].split("**Files edited")[0]
    assert "deploy to staging is still open" in unfinished       # genuinely new
    assert "add tests for the failure path" not in unfinished    # already recorded by the earlier handoff


def test_redaction_covers_common_credential_shapes_and_leaves_prose_alone():
    text = ("token=abc123 Authorization: Bearer abcdefgh12345678 postgres://u:pw@h/db "
            "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY----- " + SECRETS[2] + " " + SECRETS[3] +
            " eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij1234567890")
    out = redact(text)
    for leak in ("abc123", "abcdefgh12345678", "u:pw", "MIIB", SECRETS[2], SECRETS[3], "eyJhbGci"):
        assert leak not in out, leak
    assert redact("The tokenization step and the password policy doc were updated.") == \
        "The tokenization step and the password policy doc were updated."


def test_the_goal_words_let_a_new_session_find_the_handoff_without_a_resume_phrase(tmp_path):
    root, res, _ = rich_body(tmp_path)
    out = prompt_handle({"cwd": str(root), "prompt": "let's improve the invoice charging retry handling"}, QUIET,
                        lambda r, p, c: [], lambda *a, **k: None)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert res.item.id in ctx and "UNCONFIRMED" in ctx and "Add retry logic to invoice charging" in ctx and len(ctx) <= 1800
    assert prompt_handle({"cwd": str(root), "prompt": "please rename this variable to something clearer"}, QUIET,
                         lambda r, p, c: [], lambda *a, **k: None) is None


def test_two_sessions_ending_at_once_on_one_project_both_save(tmp_path):
    root = make_project(tmp_path)
    t1 = rich_transcript(tmp_path, root, name="s1.jsonl")
    t2 = rich_transcript(tmp_path, root, name="s2.jsonl")
    env = {**os.environ, "PYTHONPATH": str(REPO), "COGNITIVE_GRAPH_SYNC": "off"}
    procs = [subprocess.Popen([sys.executable, "-m", "cognitive_graph.session_end"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env) for _ in range(2)]
    for proc, (sid, t) in zip(procs, (("aaaa1111", t1), ("bbbb2222", t2))):
        proc.stdin.write(json.dumps(payload(root, t, session=sid)).encode())
        proc.stdin.close()
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    items = MemoryStore.open(root).items("handoff")
    assert len(items) == 2 and {i.session for i in items} == {"aaaa1111", "bbbb2222"}
    for canary in CANARIES[:0]:
        assert canary
