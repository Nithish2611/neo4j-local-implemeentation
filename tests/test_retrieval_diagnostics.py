"""Diagnostics for graph lookup errors, and the full-text score used as a tie-break.

The error tests check that a diagnostic names the KIND of failure and the Neo4j error CODE, and never the
message text (messages can echo query text) or the prompt."""
import json
import threading
import time
from pathlib import Path

import pytest

from cognitive_graph import graph_helper as gh
from cognitive_graph.memory import MemoryStore, resolve_project
from cognitive_graph.retrieval import HookConfig, prompt_brief
from cognitive_graph.terms import rank, task_terms

SECRET = "SECRET-PROMPT-TEXT"
URI, USER, PASS = "bolt://127.0.0.1:7687", "neo4j", "pw"


# Named like the driver's classes: describe_error matches by class name, so no driver import is needed.
ClientError = type("ClientError", (Exception,), {"code": "Neo.ClientError.Schema.IndexNotFound"})
AuthError = type("AuthError", (Exception,), {})
ServiceUnavailable = type("ServiceUnavailable", (Exception,), {})


class FailingBase:
    def __init__(self, exc):
        self.exc = exc

    def scoped(self, scope):
        exc = self.exc

        class S:
            def find_by_task(self, task, limit, expand):
                raise exc(f"query echoes: {SECRET}")

            def find_relevant(self, prompt, limit):
                raise exc(f"query echoes: {SECRET}")

        return S()

    def close(self):
        pass


def test_a_query_error_is_described_by_its_code_only():
    kind, detail = gh.describe_error(ClientError(f"echoes {SECRET}"))
    assert (kind, detail) == ("query", "Neo.ClientError.Schema.IndexNotFound")
    assert SECRET not in detail


@pytest.mark.parametrize("exc, kind", [(AuthError("x"), "auth"), (ServiceUnavailable("x"), "connection"),
                                      (ConnectionError("x"), "error")])
def test_other_failures_are_classified(exc, kind):
    assert gh.describe_error(exc)[0] == kind


def test_the_helper_reports_the_kind_and_code_over_the_wire(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COGNITIVE_GRAPH_HOME", str(home))
    fp = gh.fingerprint(URI, USER, PASS)
    thread = threading.Thread(target=gh.run_server, args=(FailingBase(ClientError), fp, home, 600, 0.2), daemon=True)
    thread.start()
    for _ in range(100):
        if (home / "helper.json").exists():
            break
        time.sleep(0.05)
    try:
        with pytest.raises(gh.HelperUnavailable) as err:
            gh.query_task("axis", "retry invoice charging", 5, 3, URI, USER, PASS)
        message = str(err.value)
        assert "query error: Neo.ClientError.Schema.IndexNotFound" in message
        assert SECRET not in message
    finally:
        state = json.loads((home / "helper.json").read_text(encoding="utf-8"))
        gh._exchange(state["port"], {"token": state["token"], "op": "stop"}, 2)
        thread.join(timeout=5)


def test_the_hook_diagnostic_names_the_failure_without_the_prompt(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    MemoryStore.init(root, "p").add("decision", "Retry invoice charging with backoff", "payments")

    def failing(res, prompt, cfg):
        raise gh.HelperUnavailable("helper query error: Neo.ClientError.Schema.IndexNotFound")

    brief, diags = prompt_brief(resolve_project(root), "retry invoice charging fails sometimes", HookConfig(), failing)
    assert brief and "Retry invoice charging" in brief
    assert diags == ["code graph unavailable (helper query error: Neo.ClientError.Schema.IndexNotFound); "
                     "using memory only"]
    assert all("retry invoice" not in d for d in diags)


def test_the_direct_path_error_uses_the_same_wording(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    MemoryStore.init(root, "p").add("decision", "Retry invoice charging with backoff", "payments")

    def direct(res, prompt, cfg):
        raise ClientError(SECRET)

    _, diags = prompt_brief(resolve_project(root), "retry invoice charging fails sometimes", HookConfig(), direct)
    assert diags == ["code graph unavailable (query error: Neo.ClientError.Schema.IndexNotFound); using memory only"]


def test_the_full_text_score_breaks_ties_between_equal_matches():
    rows = [
        {"file": "app/a.py", "name": "charge_invoice", "start_line": 1, "code": "def charge_invoice():\n    retry()", "ft": 1.0},
        {"file": "app/b.py", "name": "charge_invoice", "start_line": 1, "code": "def charge_invoice():\n    retry()", "ft": 4.0},
    ]
    top = rank(task_terms("retry invoice charging"), rows, 5)
    assert [r["file"] for r in top] == ["app/b.py", "app/a.py"]  # same words; the stronger full-text match first


def test_without_full_text_scores_the_ranking_is_unchanged():
    rows = [{"file": "app/a.py", "name": "charge_invoice", "start_line": 1, "code": "def charge_invoice():\n    retry()"}]
    assert rank(task_terms("retry invoice charging"), rows, 5)[0]["name"] == "charge_invoice"


def test_the_full_text_query_returns_its_score():
    from cognitive_graph.graph_db import GraphDatabase

    seen = []

    class Drv:
        def execute_query(self, query, **params):
            seen.append(query)
            return [], None, None

    GraphDatabase("", "", "", "axis", _driver=Drv()).find_by_task("retry invoice charging", 5, 0)
    assert "YIELD node, score" in seen[0] and "score AS ft" in seen[0]  # Cypher needs the yield, or it fails
