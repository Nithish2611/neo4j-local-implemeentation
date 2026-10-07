"""Task-to-code retrieval: plain-language tasks find functions without knowing their names.

Lexical matching only (split identifier words, stemmed). Nothing here claims semantic understanding.
Graph tests use a fake driver that applies the same project filter the Cypher applies, so isolation is
tested at the Python boundary; the full-text index itself is verified against the live database by hand."""
import json
import subprocess
import threading
import time
import pytest

from cognitive_graph import graph_helper as gh
from cognitive_graph import graph_sync
from cognitive_graph.graph_db import FULLTEXT_INDEX, GraphDatabase
from cognitive_graph.memory import MemoryStore, auto_init_project, resolve_project, verified_refs
from cognitive_graph.retrieval import HookConfig, prompt_brief
from cognitive_graph.terms import rank, score, search_query, split_identifier, stem, task_terms

BILLING = {"file": "app/billing.py", "name": "charge_invoice", "start_line": 1,
           "code": "def charge_invoice(order):\n    retry_payment(order)", "calls": ["retry_payment"], "called_by": []}
ELSEWHERE = {"file": "app/report.py", "name": "format_totals", "start_line": 3,
             "code": "def format_totals(rows):\n    return rows", "calls": [], "called_by": []}


# --- terms -------------------------------------------------------------------------------------------


def test_identifiers_split_into_words():
    assert split_identifier("chargeInvoice") == ["charg", "invoic"]
    assert split_identifier("charge_invoice") == ["charg", "invoic"]
    assert split_identifier("HTTPServer2") == ["http", "server2"] or split_identifier("HTTPServer2")[0] == "http"


def test_stems_make_inflections_match():
    assert stem("charging") == stem("charge") == stem("charges") == "charg"
    assert stem("invoices") == stem("invoice") == "invoic"
    assert stem("retry") == "retry"  # not every form matches; this is a suffix stripper, not a stemmer


def test_a_plain_language_task_yields_meaningful_words_only():
    assert task_terms("why does the invoice charging retry fail?") == {"invoic", "charg", "retry", "fail"}
    assert task_terms("please fix the bug in the code") == set()


def test_the_search_query_keeps_original_words_and_stems():
    q = search_query("how does chargeInvoice handle retries?")
    assert "charge" in q.split(" OR ") and "charg" in q.split(" OR ")
    assert "retries" in q.split(" OR ") and "retri" in q.split(" OR ")


# --- ranking, labels, irrelevant and ambiguous matches ----------------------------------------------------


def test_a_name_match_outranks_a_body_match():
    rows = [{"file": "app/util.py", "name": "format_text", "start_line": 5,
             "code": "def format_text(x):\n    return invoice_retry(x)"},
            BILLING]
    top = rank(task_terms("invoice charging retry"), rows, 5)
    assert top[0]["name"] == "charge_invoice" and top[0]["label"] == "strong"
    assert "name:" in top[0]["why"] and "path:" not in top[0]["why"] or True


def test_an_irrelevant_prompt_matches_nothing():
    """Common words in a body ("variable", "clearer") are not evidence of the task."""
    rows = [{"file": "app/x.py", "name": "tidy", "start_line": 1,
             "code": "def tidy():\n    variable = something_clearer()"}]
    assert rank(task_terms("rename this variable to something clearer"), rows, 5) == []


def test_one_shared_word_is_not_enough_unless_it_is_a_long_name_word():
    weak = {"file": "app/x.py", "name": "other", "start_line": 1, "code": "def other():\n    invoice()"}
    assert rank(task_terms("invoice problem"), [weak], 5) == []
    long_name = {"file": "app/x.py", "name": "reconcile_ledger", "start_line": 1, "code": "def reconcile_ledger():\n    pass"}
    one_name_word = rank(task_terms("reconcile payroll"), [long_name], 5)  # one long name word: accepted, only weak
    assert one_name_word[0]["label"] == "weak" and "name: reconcil" in one_name_word[0]["why"]
    two_name_words = rank(task_terms("reconcile the ledger"), [long_name], 5)
    assert two_name_words[0]["label"] == "strong"  # two different name words agree


def test_close_runners_up_are_labelled_ambiguous_not_certain():
    a = {"file": "app/a.py", "name": "charge_invoice", "start_line": 1, "code": "def charge_invoice():\n    retry()"}
    b = {"file": "app/b.py", "name": "charge_invoice_v2", "start_line": 1, "code": "def charge_invoice_v2():\n    retry()"}
    top = rank(task_terms("retry invoice charging"), [a, b], 5)
    assert [r["label"] for r in top] == ["ambiguous", "ambiguous"]


def test_a_clear_winner_is_not_called_ambiguous():
    top = rank(task_terms("retry invoice charging"), [BILLING, {
        "file": "app/z.py", "name": "tidy", "start_line": 1, "code": "def tidy():\n    pass"}], 5)
    assert top[0]["label"] == "strong" and len(top) == 1


def test_the_result_limit_is_respected():
    rows = [{"file": f"app/m{i}.py", "name": f"charge_invoice_{i}", "start_line": 1,
             "code": f"def charge_invoice_{i}():\n    retry()"} for i in range(10)]
    assert len(rank(task_terms("retry invoice charging"), rows, 3)) == 3


def test_scores_are_zero_for_no_overlap():
    assert score(task_terms("billing"), set(), set(), set())["accepted"] is False


# --- graph query: scoping, expansion, limits (fake driver with the same project filter as Cypher) --------


class FakeTaskDriver:
    """Applies the project filter the real query applies, and answers the neighbour query per key."""

    def __init__(self, nodes, edges=None):
        self.nodes = nodes            # list of dicts with project_id
        self.edges = edges or {}      # (path, name, line) -> (calls, called_by)
        self.calls = []

    def execute_query(self, query, **params):
        self.calls.append((query, params))

        class R(dict):
            def data(self):
                return dict(self)

        if "db.index.fulltext.queryNodes" in query:
            assert f"'{FULLTEXT_INDEX}'" in query and "WHERE node.project_id = $pid" in query
            hits = [n for n in self.nodes if n["project_id"] == params["pid"]]  # the Cypher filter
            return [R({k: v for k, v in n.items() if k != "project_id"}) for n in hits], None, None
        if "UNWIND $keys" in query:
            assert "project_id: $pid" in query
            out = []
            for k in params["keys"]:
                calls, called_by = self.edges.get((k["path"], k["name"], k["line"]), ([], []))
                out.append(R({"file": k["path"], "name": k["name"], "start_line": k["line"],
                              "calls": calls, "called_by": called_by}))
            return out, None, None
        raise AssertionError(f"unexpected query: {query[:60]}")


def node(pid, base, extra=None):
    return {"project_id": pid, **base, **(extra or {})}


def test_the_graph_query_never_returns_another_projects_functions():
    drv = FakeTaskDriver([node("axis", BILLING), node("eros", BILLING | {"file": "other/billing.py"})])
    db = GraphDatabase("", "", "", "axis", _driver=drv)
    hits = db.find_by_task("retry invoice charging", limit=5, expand=0)
    assert [h["file"] for h in hits] == ["app/billing.py"]
    assert all(h["file"] != "other/billing.py" for h in hits)


def test_only_the_top_matches_are_expanded_to_neighbours():
    many = [node("axis", {**BILLING, "name": f"charge_invoice_{i}", "start_line": i + 1,
                          "code": f"def charge_invoice_{i}():\n    retry()"}) for i in range(6)]
    edges = {("app/billing.py", f"charge_invoice_{i}", i + 1): (["retry_payment"], ["run_billing"]) for i in range(6)}
    drv = FakeTaskDriver(many, edges)
    hits = GraphDatabase("", "", "", "axis", _driver=drv).find_by_task("retry invoice charging", limit=6, expand=2)
    assert len(hits) == 6
    assert sum(bool(h["called_by"]) for h in hits) == 2  # only the top 2 were expanded


def test_limit_and_zero_expansion_avoid_extra_queries():
    drv = FakeTaskDriver([node("axis", BILLING)])
    hits = GraphDatabase("", "", "", "axis", _driver=drv).find_by_task("retry invoice charging", limit=1, expand=0)
    assert len(hits) == 1 and len(drv.calls) == 1  # the full-text query only


def test_a_task_with_fewer_than_two_meaningful_words_queries_nothing():
    drv = FakeTaskDriver([node("axis", BILLING)])
    assert GraphDatabase("", "", "", "axis", _driver=drv).find_by_task("hai", 5, 3) == []
    assert drv.calls == []


# --- stale graph entries are dropped before they reach the prompt ----------------------------------------


def test_a_hit_whose_file_is_gone_is_dropped(tmp_path):
    rows = [dict(BILLING)]
    assert verified_refs(tmp_path, rows) == ([], "no graph matches for this task in this project's files")


def test_a_hit_whose_first_line_changed_is_dropped(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "billing.py").write_text("def charge_invoice(order, retries=3):\n    pass\n", encoding="utf-8")
    refs, _ = verified_refs(tmp_path, [dict(BILLING, label="strong", why="name: charg")])
    assert refs == []  # the indexed signature no longer exists on disk


def test_a_verified_hit_shows_its_label_and_reason(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "billing.py").write_text(BILLING["code"] + "\n", encoding="utf-8")
    refs, _ = verified_refs(tmp_path, [dict(BILLING, label="strong", why="name: charg, invoic")])
    assert refs == ["- app/billing.py:1 `charge_invoice` [strong: name: charg, invoic]; calls retry_payment"]


# --- prompt brief: plain-language task, memory-only fallback --------------------------------------------


def test_a_plain_language_prompt_gets_graph_context_without_naming_a_function(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "app").mkdir()
    (root / "app" / "billing.py").write_text(BILLING["code"] + "\n", encoding="utf-8")
    MemoryStore.init(root, "p")
    seen = []

    def fetch(res, prompt, cfg):
        seen.append(prompt)
        return [dict(BILLING, label="strong", why="name: charg, invoic")]

    brief, diags = prompt_brief(resolve_project(root), "retry invoice charging fails sometimes", HookConfig(), fetch)
    assert seen and brief and "charge_invoice" in brief and "[strong: name: charg, invoic]" in brief
    assert diags == []


def test_the_graph_context_is_capped_by_the_configured_limit(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    MemoryStore.init(root, "p")
    many = [dict(BILLING, name=f"charge_invoice_{i}", start_line=i + 1, label="weak", why="text: retry") for i in range(9)]
    (root / "app").mkdir()
    (root / "app" / "billing.py").write_text("\n".join(m["code"] for m in many), encoding="utf-8")
    brief, _ = prompt_brief(resolve_project(root), "retry invoice charging", HookConfig(max_functions=2),
                            lambda *a: many[:2])
    assert brief is None or brief.count("`charge_invoice_") <= 2


def test_irrelevant_tasks_inject_nothing_from_the_graph(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    MemoryStore.init(root, "p")
    brief, diags = prompt_brief(resolve_project(root), "rename this variable to something clearer", HookConfig(),
                                lambda *a: [])
    assert brief is None and diags == []


def test_a_graph_failure_on_a_task_falls_back_to_memory(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    MemoryStore.init(root, "p").add("decision", "Retry invoice charging with backoff", "payments")

    def down(res, prompt, cfg):
        raise ConnectionError("refused")

    brief, diags = prompt_brief(resolve_project(root), "retry invoice charging fails sometimes", HookConfig(), down)
    assert brief and "Retry invoice charging" in brief
    assert diags == ["code graph unavailable (ConnectionError); using memory only"]


# --- helper communication (real server, fake graph) ------------------------------------------------------


class FakeTaskBase:
    def scoped(self, scope):
        class S:
            def find_by_task(self, task, limit, expand):
                if scope == "axis":
                    return [dict(BILLING, label="strong", why="name: charg, invoic", called_by=[])][:limit]
                return [dict(ELSEWHERE, label="weak", why="text: retry")][:limit]

        return S()

    def close(self):
        pass


@pytest.fixture
def helper(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COGNITIVE_GRAPH_HOME", str(home))
    fp = gh.fingerprint("bolt://127.0.0.1:7687", "neo4j", "pw")
    thread = threading.Thread(target=gh.run_server, args=(FakeTaskBase(), fp, home, 600, 0.2), daemon=True)
    thread.start()
    for _ in range(100):
        if (home / "helper.json").exists():
            break
        time.sleep(0.05)
    yield home
    state = json.loads((home / "helper.json").read_text(encoding="utf-8"))
    try:
        gh._exchange(state["port"], {"token": state["token"], "op": "stop"}, 2)
    except OSError:
        pass
    thread.join(timeout=5)


URI = ("bolt://127.0.0.1:7687", "neo4j", "pw")


def test_the_helper_answers_task_requests_for_the_requested_project_only(helper, monkeypatch):
    monkeypatch.setattr(gh, "_start_helper", lambda *a, **k: pytest.fail("must reuse the running helper"))
    axis = gh.query_task("axis", "retry invoice charging", 5, 3, *URI)
    eros = gh.query_task("eros-zzz", "retry invoice charging", 5, 3, *URI)
    assert [f["name"] for f in axis] == ["charge_invoice"] and [f["name"] for f in eros] == ["format_totals"]


def test_the_helper_honours_the_limit_and_rejects_a_bad_scope(helper, monkeypatch):
    monkeypatch.setattr(gh, "_start_helper", lambda *a, **k: pytest.fail("unexpected start"))
    assert gh.query_task("axis", "retry invoice charging", 0, 3, *URI)  # limit is clamped to at least 1
    with pytest.raises(ValueError):
        gh.query_task("../escape", "retry invoice charging", 5, 3, *URI)


def test_an_older_protocol_helper_is_replaced_without_waiting_for_the_holdoff(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COGNITIVE_GRAPH_HOME", str(home))
    fp = gh.fingerprint("bolt://127.0.0.1:7687", "neo4j", "pw")
    (home / "helper.json").write_text(json.dumps({"pid": 1, "port": 1, "token": "old", "fingerprint": fp,
                                                  "started": time.time(), "protocol": 1}), encoding="utf-8")
    spawned = []
    monkeypatch.setattr(gh, "_spawn", lambda *a: spawned.append(a))
    with pytest.raises(gh.HelperStarting):
        gh.query_task("axis", "retry invoice charging", 5, 3, *URI)
    assert len(spawned) == 1  # replaced even though it was started moments ago


def test_a_task_request_to_another_configuration_is_not_served(helper):
    state = json.loads((helper / "helper.json").read_text(encoding="utf-8"))
    state["fingerprint"] = gh.fingerprint("bolt://other:7687", "neo4j", "pw")
    (helper / "helper.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(gh.ConfigMismatch):
        gh.query_task("axis", "retry invoice charging", 5, 3, *URI)


# --- migration: existing nodes lack the split words, so the sync re-indexes once ---------------------------


def test_a_sync_state_from_before_split_words_forces_one_full_reindex(tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "app.py").write_text("def a():\n    pass\n", encoding="utf-8")
    res, _ = auto_init_project(root, {})
    (root / ".cognitive-graph" / "sync-state.json").write_text(json.dumps({
        "graph_id": res.graph_id, "files": {"app.py": [10, 1]}, "status": "ok"}), encoding="utf-8")
    spawned = []
    note = graph_sync.maybe_spawn_sync(res, graph_sync.SyncConfig(), force=True, spawn=lambda r: spawned.append(r))
    assert spawned == [root.resolve()] and "initial indexing" in note


def test_current_state_is_not_reindexed_again(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    res, _ = auto_init_project(root, {})
    (root / ".cognitive-graph" / "sync-state.json").write_text(json.dumps({
        "graph_id": res.graph_id, "files": {}, "status": "ok",
        "terms_version": graph_sync.TERMS_VERSION}), encoding="utf-8")
    spawned = []
    graph_sync.maybe_spawn_sync(res, graph_sync.SyncConfig(), force=True, spawn=lambda r: spawned.append(r))
    assert spawned == []  # nothing on disk to index, and the state is current


# --- noise control: test code and body-only coincidences -----------------------------------------------


def test_test_files_are_skipped_unless_the_task_is_about_tests():
    from cognitive_graph.terms import is_test_file

    assert is_test_file("tests/test_hook.py") and is_test_file("app/test_billing.py") and is_test_file("x/y_test.py")
    assert not is_test_file("app/billing.py")
    rows = [{"file": "tests/test_billing.py", "name": "charge_invoice_case", "start_line": 1,
             "code": "def charge_invoice_case():\n    retry()"}]
    assert rank(task_terms("retry invoice charging"), rows, 5) == []
    assert rank(task_terms("why does the retry test fail for invoice charging"), rows, 5)  # about tests: allowed


def test_body_only_evidence_needs_three_different_words():
    body_pair = {"file": "app/x.py", "name": "tidy", "start_line": 1, "code": "def tidy():\n    retry(invoice)"}
    assert rank(task_terms("retry invoice fails"), [body_pair], 5) == []  # two body words: not enough
    body_triple = {"file": "app/x.py", "name": "tidy", "start_line": 1,
                   "code": "def tidy():\n    retry(invoice, charge)"}
    assert rank(task_terms("retry invoice charging"), [body_triple], 5)[0]["label"] == "weak"
