"""The warm graph helper: protocol, isolation, lifecycle, and failure paths.

Most tests run the real server and client over 127.0.0.1 with a fake graph and a scratch state folder
(COGNITIVE_GRAPH_HOME), so nothing touches your real ~/.cognitive-graph or any Neo4j database. One test
starts the real helper process against a closed port to check the Neo4j-down path."""
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cognitive_graph import graph_helper as gh
from cognitive_graph.retrieval import HookConfig, prompt_brief
from cognitive_graph.memory import MemoryStore, resolve_project

REPO = Path(__file__).resolve().parents[1]
URI, USER, PASS = "bolt://127.0.0.1:7687", "neo4j", "pw"
FP = gh.fingerprint(URI, USER, PASS)


class FakeScoped:
    def __init__(self, scope):
        self.scope = scope

    def find_relevant(self, prompt, limit):
        if "boom" in prompt:
            raise ConnectionError("db went away")
        return [{"file": f"{self.scope}/app.py", "name": f"fn_in_{self.scope}", "start_line": 1,
                 "code": "def f():", "calls": [], "called_by": []}]


class FakeBase:
    def scoped(self, scope):
        return FakeScoped(scope)

    def close(self):
        pass


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("COGNITIVE_GRAPH_HOME", str(d))
    return d


@pytest.fixture
def server(home):
    """A real server on localhost, shut down after the test."""
    thread = threading.Thread(target=gh.run_server, args=(FakeBase(), FP, home, 600, 0.2), daemon=True)
    thread.start()
    for _ in range(100):
        if (home / "helper.json").exists():
            break
        time.sleep(0.05)
    state = json.loads((home / "helper.json").read_text(encoding="utf-8"))
    yield state
    try:
        gh._exchange(state["port"], {"token": state["token"], "op": "stop"}, 2)
    except OSError:
        pass
    thread.join(timeout=5)


def test_ping_requires_the_token(server):
    assert gh._exchange(server["port"], {"token": server["token"], "op": "ping"}, 2)["ok"] is True
    assert gh._exchange(server["port"], {"token": "wrong", "op": "ping"}, 2) == {"ok": False, "error": "unauthorised"}
    assert gh._exchange(server["port"], {"op": "ping"}, 2) == {"ok": False, "error": "unauthorised"}


def test_the_listener_is_bound_to_localhost_only():
    srv = gh._Server(("127.0.0.1", 0), gh._Handler)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


def test_queries_are_scoped_to_the_requested_project(server, home, monkeypatch):
    monkeypatch.setattr(gh, "_start_helper", lambda *a: pytest.fail("must reuse the running helper"))
    a = gh.query("projA-aaaa", "explain fn", 5, URI, USER, PASS)
    b = gh.query("projB-bbbb", "explain fn", 5, URI, USER, PASS)
    assert [f["name"] for f in a] == ["fn_in_projA-aaaa"]
    assert [f["name"] for f in b] == ["fn_in_projB-bbbb"]


def test_a_bad_scope_is_rejected_before_it_reaches_the_graph(server, home):
    with pytest.raises(ValueError):
        gh.query("../escape", "x", 5, URI, USER, PASS)
    reply = gh._exchange(server["port"], {"token": server["token"], "op": "find", "scope": "a b", "prompt": "x"}, 2)
    assert reply == {"ok": False, "error": "bad scope"}


def test_graph_errors_come_back_as_a_kind_not_a_detail(server, home):
    with pytest.raises(gh.HelperUnavailable) as err:
        gh.query("projA-aaaa", "boom", 5, URI, USER, PASS)
    assert "ConnectionError" in str(err.value) and "db went away" not in str(err.value)


def test_a_helper_for_another_neo4j_configuration_is_not_used(server, home, monkeypatch):
    other = dict(server, fingerprint=gh.fingerprint("bolt://other:7687", USER, PASS))
    (home / "helper.json").write_text(json.dumps(other), encoding="utf-8")
    with pytest.raises(gh.ConfigMismatch):
        gh.query("projA-aaaa", "explain fn", 5, URI, USER, PASS)


def test_concurrent_sessions_share_one_helper(server, home, monkeypatch):
    monkeypatch.setattr(gh, "_start_helper", lambda *a: pytest.fail("no second helper"))
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: gh.query(f"proj{i}-cccc", "explain fn", 5, URI, USER, PASS), range(24)))
    assert all(r[0]["name"] == f"fn_in_proj{i}-cccc" for i, r in enumerate(results))


def test_stop_shuts_the_helper_down_and_cleans_up(home):
    thread = threading.Thread(target=gh.run_server, args=(FakeBase(), FP, home, 600, 0.2), daemon=True)
    thread.start()
    for _ in range(100):
        if (home / "helper.json").exists():
            break
        time.sleep(0.05)
    assert "stopped graph helper" in gh.stop()
    thread.join(timeout=5)
    assert not thread.is_alive() and not (home / "helper.json").exists()


def test_an_idle_helper_exits_and_removes_its_record(home):
    thread = threading.Thread(target=gh.run_server, args=(FakeBase(), FP, home, 0.5, 0.2), daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive() and not (home / "helper.json").exists()


def test_a_replaced_helper_exits_and_does_not_delete_its_successor(home):
    thread = threading.Thread(target=gh.run_server, args=(FakeBase(), FP, home, 600, 0.2), daemon=True)
    thread.start()
    for _ in range(100):
        if (home / "helper.json").exists():
            break
        time.sleep(0.05)
    successor = {"pid": 1, "port": 1, "token": "newer-token", "fingerprint": FP, "started": time.time()}
    (home / "helper.json").write_text(json.dumps(successor), encoding="utf-8")
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert json.loads((home / "helper.json").read_text(encoding="utf-8"))["token"] == "newer-token"


def test_a_cold_start_returns_immediately_and_spawns_once(home, monkeypatch):
    spawned = []
    monkeypatch.setattr(gh, "_spawn", lambda *a: spawned.append(a))
    with pytest.raises(gh.HelperStarting):
        gh.query("projA-aaaa", "explain fn", 5, URI, USER, PASS)
    with pytest.raises(gh.HelperStarting):  # another session, same moment: no duplicate spawn
        gh.query("projA-aaaa", "explain fn", 5, URI, USER, PASS)
    assert len(spawned) == 1


def test_a_start_lock_left_by_a_dead_process_is_taken_over(home, monkeypatch):
    spawned = []
    monkeypatch.setattr(gh, "_spawn", lambda *a: spawned.append(a))
    lock = home / "helper.lock"
    lock.write_text("999999")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    with pytest.raises(gh.HelperStarting):
        gh.query("projA-aaaa", "explain fn", 5, URI, USER, PASS)
    assert len(spawned) == 1


def test_a_recent_failed_start_backs_off_without_spawning(home, monkeypatch):
    monkeypatch.setattr(gh, "_spawn", lambda *a: pytest.fail("must not retry a refused database every prompt"))
    (home / "helper-failed.json").write_text(json.dumps({"fingerprint": FP, "at": time.time(),
                                                         "reason": "Neo4j unavailable (ServiceUnavailable)"}),
                                             encoding="utf-8")
    with pytest.raises(gh.HelperUnavailable) as err:
        gh.query("projA-aaaa", "explain fn", 5, URI, USER, PASS)
    assert "Neo4j unavailable" in str(err.value)


def test_a_stale_record_is_replaced_and_a_stop_removes_it(home, monkeypatch):
    (home / "helper.json").write_text(json.dumps({"pid": 1, "port": 1, "token": "x", "fingerprint": FP,
                                                  "started": time.time() - 10_000}), encoding="utf-8")
    spawned = []
    monkeypatch.setattr(gh, "_spawn", lambda *a: spawned.append(a))
    with pytest.raises(gh.HelperStarting):
        gh.query("projA-aaaa", "explain fn", 5, URI, USER, PASS)
    assert len(spawned) == 1
    assert "removed a stale helper record" in gh.stop()
    assert not (home / "helper.json").exists()


def test_status_reports_not_running(home):
    assert gh.status() == "not running"


def test_the_real_helper_process_reports_an_unreachable_database(home, tmp_path):
    """Real process, closed port: it must record the failure and clean up, so the hook backs off."""
    env = {**os.environ, "PYTHONPATH": str(REPO), "COGNITIVE_GRAPH_HOME": str(home),
           "NEO4J_URI": "bolt://127.0.0.1:1", "NEO4J_USER": "neo4j", "NEO4J_PASSWORD": "pw"}
    (home / "helper.lock").write_text("starter")
    proc = subprocess.run([sys.executable, "-m", "cognitive_graph.graph_helper", "--serve"], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    failed = json.loads((home / "helper-failed.json").read_text(encoding="utf-8"))
    assert failed["reason"].startswith("Neo4j unavailable")
    assert failed["fingerprint"] == gh.fingerprint("bolt://127.0.0.1:1", "neo4j", "pw")
    assert not (home / "helper.lock").exists() and not (home / "helper.json").exists()


def test_the_hook_reports_a_warming_helper_and_still_answers_from_memory(tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    MemoryStore.init(root, "p").add("decision", "Charge invoices with Stripe", "tax handling")

    def starting(res, prompt, cfg):
        raise gh.HelperStarting("graph helper is starting")

    brief, diags = prompt_brief(resolve_project(root), "why does charge_invoice use Stripe for invoices?",
                                HookConfig(), starting)
    assert brief and "Charge invoices with Stripe" in brief
    assert diags == ["code graph helper is starting in the background; graph context from the next prompt"]


def test_the_helper_can_be_switched_off(monkeypatch):
    from cognitive_graph.retrieval import helper_enabled

    assert helper_enabled({}) is True
    assert helper_enabled({"COGNITIVE_GRAPH_HELPER": "off"}) is False
