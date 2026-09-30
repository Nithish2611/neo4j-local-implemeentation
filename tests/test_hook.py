"""The UserPromptSubmit hook: input/output contract, project scoping, failure handling."""
import io
import json
import os
import subprocess
import sys
import time

import pytest

from cognitive_graph import hook, hook_cli
from cognitive_graph.hook import handle
from cognitive_graph.memory import MemoryStore, ensure_project
from cognitive_graph.retrieval import HookConfig, prompt_brief

ENV = {"COGNITIVE_GRAPH_SYNC": "off"}  # defaults only, no .env leakage; background sync off so tests never spawn processes


@pytest.fixture
def axis(tmp_path):
    root = tmp_path / "axis-ri"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "app").mkdir()
    (root / "app" / "billing.py").write_text("def charge_invoice():\n    pass\n", encoding="utf-8")
    store = MemoryStore.init(root, "Axis RI")
    store.add("decision", "Invoices are charged through Stripe", "Chosen for tax handling", evidence=["app/billing.py"])
    store.add("task", "Add retry to invoice charging", source="claude")
    store.add("fact", "Unrelated legacy report exporter")
    store.save_handoff("Finished the billing refactor, retry logic still open")
    return root


def no_graph(res, prompt, cfg):
    return []


def graph_hit(res, prompt, cfg):
    return [{"file": "app/billing.py", "name": "charge_invoice", "start_line": 1,
             "code": "def charge_invoice():\n    pass", "calls": ["stripe_charge"], "called_by": ["run_billing"]}]


def out_of(root, prompt, graph=no_graph, env=None):
    return handle({"cwd": str(root), "prompt": prompt, "hook_event_name": "UserPromptSubmit"}, {**ENV, **(env or {})}, graph)


# --- output contract -----------------------------------------------------------------


def test_relevant_prompt_injects_labelled_brief_in_supported_shape(axis):
    out = out_of(axis, "how are invoices charged with stripe?")
    assert set(out) == {"hookSpecificOutput"}
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "UserPromptSubmit"
    text = h["additionalContext"]
    assert "Axis RI" in text and "D-0001" in text and "confirmed" in text and "app/billing.py" in text
    assert "Unrelated legacy" not in text
    json.dumps(out)  # serialisable


def test_prompt_text_key_is_accepted_too(axis):
    out = handle({"cwd": str(axis), "prompt_text": "how are invoices charged with stripe?"}, ENV, no_graph)
    assert out and "D-0001" in out["hookSpecificOutput"]["additionalContext"]


def test_unconfirmed_items_are_labelled(axis):
    text = out_of(axis, "add retry to invoice charging")["hookSpecificOutput"]["additionalContext"]
    assert "UNCONFIRMED" in text and "T-0001" in text


def test_graph_references_are_included_with_source(axis):
    text = out_of(axis, "explain charge_invoice", graph_hit)["hookSpecificOutput"]["additionalContext"]
    assert "app/billing.py:1 `charge_invoice`" in text and "calls stripe_charge" in text and "called by run_billing" in text


def test_graph_hit_for_a_file_missing_on_disk_is_dropped(axis):
    (axis / "app" / "billing.py").unlink()
    assert out_of(axis, "explain charge_invoice", graph_hit) is None


def test_resume_prompt_brings_latest_handoff_and_open_work(axis):
    text = out_of(axis, "continue where we left off")["hookSpecificOutput"]["additionalContext"]
    assert "billing refactor" in text and "T-0001" in text


def test_brief_respects_size_limit(axis):
    store = MemoryStore.open(axis)
    for i in range(20):
        store.add("decision", f"Invoice decision number {i}", "long body " * 40)
    res = ensure_project(axis)
    brief, _ = prompt_brief(res, "invoice decision", HookConfig(max_chars=500), no_graph)
    assert brief and len(brief) <= 500


# --- no-match behaviour ---------------------------------------------------------------


def test_ordinary_prompt_adds_nothing(axis):
    assert out_of(axis, "please rename this variable to something clearer") is None


def test_short_prompts_and_slash_commands_add_nothing(axis):
    assert out_of(axis, "ok") is None
    assert out_of(axis, "/compact invoices stripe charged") is None


def test_verbose_reports_no_match_status(axis):
    out = out_of(axis, "please rename this variable to something clearer", env={"COGNITIVE_GRAPH_HOOK_VERBOSE": "1"})
    assert "hookSpecificOutput" not in out and "no relevant project memory" in out["systemMessage"]


def test_hook_can_be_switched_off(axis):
    assert out_of(axis, "how are invoices charged with stripe?", env={"COGNITIVE_GRAPH_HOOK": "off"}) is None


# --- project isolation ------------------------------------------------------------------


def test_only_the_active_project_memory_is_used(axis, tmp_path):
    eros = tmp_path / "eros-innovation"
    eros.mkdir()
    subprocess.run(["git", "init", "-q", str(eros)], check=True)
    MemoryStore.init(eros, "Eros Innovation").add("decision", "Flights are booked through Amadeus", "GDS choice")
    e = out_of(eros, "how are flights booked through amadeus?")["hookSpecificOutput"]["additionalContext"]
    assert "Eros Innovation" in e and "Amadeus" in e and "Stripe" not in e
    assert out_of(axis, "how are flights booked through amadeus?") is None
    a = out_of(axis, "how are invoices charged with stripe?")["hookSpecificOutput"]["additionalContext"]
    assert "Amadeus" not in a and "Eros" not in a


def test_graph_fetch_receives_the_resolved_project_only(axis):
    seen = []

    def spy(res, prompt, cfg):
        seen.append((res.project_id, res.root))
        return []

    out_of(axis, "how are invoices charged with stripe?", spy)
    assert seen == [(MemoryStore.open(axis).project_id, axis.resolve())]


def test_subdirectory_cwd_resolves_to_the_repo_project(axis):
    out = out_of(axis / "app", "how are invoices charged with stripe?")
    assert out and "Axis RI" in out["hookSpecificOutput"]["additionalContext"]


def test_uninitialised_project_gets_no_context_and_no_fallback(tmp_path, axis):
    other = tmp_path / "fresh-repo"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    assert out_of(other, "how are invoices charged with stripe?", env={"COGNITIVE_GRAPH_AUTO_INIT": "off"}) is None
    msg = out_of(other, "how are invoices charged with stripe?", env={"COGNITIVE_GRAPH_HOOK_VERBOSE": "1", "COGNITIVE_GRAPH_AUTO_INIT": "off"})
    assert "hookSpecificOutput" not in msg and "memory init" in msg["systemMessage"]


def test_invalid_project_metadata_warns_and_adds_nothing(tmp_path):
    meta = tmp_path / ".cognitive-graph" / "project.json"
    meta.parent.mkdir()
    meta.write_text("garbage", encoding="utf-8")
    out = out_of(tmp_path, "how are invoices charged with stripe?")
    assert "hookSpecificOutput" not in out and "unusable" in out["systemMessage"]


# --- graceful failures ------------------------------------------------------------------


def test_graph_error_falls_back_to_memory_with_diagnostic(axis):
    def boom(res, prompt, cfg):
        raise ConnectionError("neo4j down")

    out = out_of(axis, "how are invoices charged with stripe?", boom)
    assert "D-0001" in out["hookSpecificOutput"]["additionalContext"]
    assert "code graph unavailable (ConnectionError)" in out["systemMessage"]
    assert "neo4j down" not in json.dumps(out)


def test_graph_timeout_does_not_block(axis):
    def slow(res, prompt, cfg):
        time.sleep(2)
        return []

    t0 = time.time()
    out = out_of(axis, "how are invoices charged with stripe?", slow, env={"COGNITIVE_GRAPH_HOOK_TIMEOUT": "0.2"})
    assert time.time() - t0 < 1.5
    assert "D-0001" in out["hookSpecificOutput"]["additionalContext"] and "TimeoutError" in out["systemMessage"]


def test_quiet_mode_suppresses_graph_warning(axis):
    def boom(res, prompt, cfg):
        raise ConnectionError()

    out = out_of(axis, "how are invoices charged with stripe?", boom, env={"COGNITIVE_GRAPH_HOOK_QUIET": "1"})
    assert "systemMessage" not in out


def test_corrupt_memory_file_is_skipped_not_fatal(axis):
    bad = axis / ".cognitive-graph" / "memory" / "fact" / "F-0099-bad.md"
    bad.write_text("no front matter", encoding="utf-8")
    assert out_of(axis, "how are invoices charged with stripe?")


def test_main_never_blocks_and_ignores_garbage_input(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(b"\xff not json")})())
    assert hook.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert "decision" not in out and "hook error" in out["systemMessage"]


def test_main_end_to_end_as_a_subprocess(axis):
    payload = json.dumps({"cwd": str(axis), "prompt": "how are invoices charged with stripe?"})
    env = {**os.environ, "COGNITIVE_GRAPH_HOOK_GRAPH": "off", "COGNITIVE_GRAPH_SYNC": "off"}
    r = subprocess.run([sys.executable, "-m", "cognitive_graph.hook"], input=payload, capture_output=True,
                       text=True, timeout=30, env=env)
    assert r.returncode == 0
    assert "D-0001" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


# --- install / uninstall ------------------------------------------------------------------


def test_install_is_idempotent_and_preserves_other_settings(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-tool"}]}],
                  "Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}), encoding="utf-8")
    hook_cli.install(path, "cognitive-graph-hook")
    hook_cli.install(path, "cognitive-graph-hook")
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data["hooks"]["UserPromptSubmit"]
    assert len(groups) == 2 and data["permissions"] == {"allow": ["Bash(ls)"]} and "Stop" in data["hooks"]
    assert groups[1]["hooks"][0] == {"type": "command", "command": "cognitive-graph-hook", "timeout": 10}
    assert hook_cli.uninstall(path).startswith("Removed 1 cognitive-graph hook entry")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"]["UserPromptSubmit"] == [{"hooks": [{"type": "command", "command": "other-tool"}]}]
    assert data["permissions"] and "Stop" in data["hooks"]


def test_uninstall_removes_empty_containers(tmp_path):
    path = tmp_path / "settings.json"
    hook_cli.install(path, "cognitive-graph-hook")
    hook_cli.uninstall(path)
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_install_refuses_to_overwrite_unparseable_settings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ValueError):
        hook_cli.install(path, "cognitive-graph-hook")
    assert path.read_text(encoding="utf-8") == "{ broken"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at a scratch folder so user-scope tests can never touch ~/.claude."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(hook_cli.Path, "home", classmethod(lambda cls: home))
    assert hook_cli.settings_path("user", tmp_path) == home / ".claude" / "settings.json"
    return home


def test_settings_paths_by_scope_use_the_launch_folder(tmp_path, fake_home):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sub = tmp_path / "pkg"
    sub.mkdir()
    # Claude Code reads project settings only from the folder it is launched in, not the Git root
    assert hook_cli.settings_path("local", sub) == sub / ".claude" / "settings.local.json"
    assert hook_cli.settings_path("project", sub) == sub / ".claude" / "settings.json"
    assert hook_cli.settings_path("user", sub) == fake_home / ".claude" / "settings.json"


@pytest.mark.parametrize("scope,relative", [("local", ".claude/settings.local.json"),
                                            ("project", ".claude/settings.json"),
                                            ("user", "HOME/.claude/settings.json")])
def test_cli_install_status_uninstall_for_every_scope(scope, relative, tmp_path, fake_home, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    target = (fake_home / ".claude" / "settings.json") if scope == "user" else proj / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"model": "keep-me", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}),
                      encoding="utf-8")
    assert hook_cli.run(["--project", str(proj), "install", "--scope", scope]) == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["model"] == "keep-me" and "Stop" in data["hooks"]
    assert any("cognitive" in h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"])
    capsys.readouterr()
    assert hook_cli.run(["--project", str(proj), "status"]) == 0
    assert f"{scope:8}" in capsys.readouterr().out
    assert hook_cli.run(["--project", str(proj), "uninstall", "--scope", scope]) == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"model": "keep-me", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}


def test_installing_below_the_git_root_warns(tmp_path, fake_home, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sub = tmp_path / "pkg"
    sub.mkdir()
    assert hook_cli.run(["--project", str(sub), "install", "--scope", "local"]) == 0
    assert "not the Git root" in capsys.readouterr().out
    assert (sub / ".claude" / "settings.local.json").exists() and not (tmp_path / ".claude").exists()


def test_real_home_settings_are_never_touched_by_these_tests(fake_home):
    assert "home" in str(fake_home) and str(fake_home) != str(hook_cli.Path.home().parent)
