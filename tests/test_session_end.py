"""Session-end handoff: event parsing, safe contents, isolation, atomic/unique/concurrent writes,
failure handling, retrieval in a new session, and the enable/disable commands.

Everything runs in scratch folders. Claude Code settings are never touched (user scope uses a
redirected home), and no real project data is read or written."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cognitive_graph import hook_cli, session_end
from cognitive_graph.hook import handle as prompt_handle
from cognitive_graph.memory import MemoryStore
from cognitive_graph.session_end import handle

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 30, 10, 30, 15, tzinfo=timezone.utc)
CANARIES = ("CANARY-USER-SECRET", "CANARY-ASSISTANT-TEXT", "CANARY-FILE-CONTENT", "CANARY-ENV-VALUE", "CANARY-ENV-KEY")


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def make_project(tmp_path, name="axis-ri"):
    root = tmp_path / name
    (root / "app").mkdir(parents=True)
    git(root.parent, "init", "-q", str(root))
    git(root, "commit", "-q", "--allow-empty", "-m", "init")
    (root / "app" / "billing.py").write_text("def charge():\n    pass\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=CANARY-ENV-VALUE\n", encoding="utf-8")
    MemoryStore.init(root, name)
    return root


def make_transcript(tmp_path, root, edits, name="t.jsonl", garbage=False):
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "fix billing CANARY-USER-SECRET sk-live-abc"}})]
    if garbage:
        lines.append("{ this is not json but mentions tool_use")
    for tool, path in edits:
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "CANARY-ASSISTANT-TEXT"},
            {"type": "tool_use", "name": tool, "input": {key: str(path), "content": "CANARY-FILE-CONTENT"}}]}}))
    f = tmp_path / name
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def payload(root, transcript, session="4f1c9a7e-1111-2222-3333-444455556666", reason="other", **extra):
    return {"session_id": session, "transcript_path": str(transcript), "cwd": str(root), "reason": reason,
            "hook_event_name": "SessionEnd", **extra}


ENV = {"ANTHROPIC_API_KEY": "CANARY-ENV-KEY", "COGNITIVE_GRAPH_SYNC": "off"}  # hostile env value must never reach the handoff
NOQ = {**ENV, "COGNITIVE_GRAPH_HANDOFF_QUOTES": "off"}  # tool facts only
HOOK_ENV = {"COGNITIVE_GRAPH_SYNC": "off"}


@pytest.fixture
def axis(tmp_path):
    return make_project(tmp_path)


@pytest.fixture
def edited(axis, tmp_path):
    (axis / "app" / "new_feature.py").write_text("x = 1\n", encoding="utf-8")  # untracked -> shows in git status
    return make_transcript(tmp_path, axis, [("Write", axis / "app" / "new_feature.py"),
                                            ("Edit", axis / "app" / "billing.py"),
                                            ("Edit", axis / "app" / "billing.py")])


def handoff_files(root):
    d = root / ".cognitive-graph" / "memory" / "handoff"
    return sorted(d.glob("*.md")) if d.exists() else []


# --- event parsing and contents -------------------------------------------------------------


def test_saves_a_proposed_handoff_with_expected_fields(axis, edited):
    res = handle(payload(axis, edited), ENV, NOW)
    assert res.status == "saved" and res.item.id == "H-20260930T103015Z-4f1c9a7e"
    item = MemoryStore.open(axis).get(res.item.id)
    assert (item.type, item.source, item.trust, item.status) == ("handoff", "hook", "proposed", "active")
    assert item.session == "4f1c9a7e-1111-2222-3333-444455556666" and item.project == MemoryStore.open(axis).project_id
    assert item.tags == ["auto", "session-end"]
    branch = git(axis, "rev-parse", "--abbrev-ref", "HEAD")
    assert branch in item.title and f"branch {branch}" in item.body and git(axis, "rev-parse", "--short", "HEAD") in item.body
    assert "app/new_feature.py" in item.body and "app/billing.py" in item.body
    assert item.body.count("- app/billing.py") == 1  # de-duplicated
    assert "session:4f1c9a7e-1111-2222-3333-444455556666" in item.evidence and "app/billing.py" in item.evidence
    assert any(e.startswith("commit:") for e in item.evidence)
    assert "session ended (other)" in item.body and "Unreviewed" in item.body
    assert "new app/new_feature.py" in item.body  # uncommitted section from git status


def test_no_conversation_text_secrets_or_env_values_are_saved(axis, edited):
    (axis / ".aws").mkdir()
    (axis / "credentials.json").write_text("{}", encoding="utf-8")
    t = make_transcript(edited.parent, axis, [("Write", axis / ".env"), ("Write", axis / "credentials.json"),
                                              ("Edit", axis / "app" / "billing.py")], name="t2.jsonl")
    res = handle(payload(axis, t), NOQ, NOW)
    assert res.status == "saved"
    stored = "\n".join(p.read_text(encoding="utf-8") for p in (axis / ".cognitive-graph").rglob("*") if p.is_file())
    for canary in CANARIES:
        assert canary not in stored, canary
    assert "2 sensitive-looking path(s) omitted" in stored
    assert "credentials.json" not in stored


def test_only_tool_metadata_is_read_and_paths_outside_the_project_are_ignored(axis, tmp_path):
    outside = tmp_path / "elsewhere" / "x.py"
    t = make_transcript(tmp_path, axis, [("Write", outside), ("NotebookEdit", axis / "n.ipynb"),
                                         ("Write", axis / ".cognitive-graph" / "memory" / "fact" / "F-1.md"),
                                         ("Write", "relative/thing.py")])
    res = handle(payload(axis, t), ENV, NOW)
    assert res.status == "saved"
    assert "elsewhere" not in res.body and "F-1.md" not in res.body
    assert "n.ipynb" in res.body and "relative/thing.py" in res.body


def test_garbage_lines_and_odd_entries_are_skipped_not_fatal(axis, tmp_path):
    t = make_transcript(tmp_path, axis, [("Edit", axis / "app" / "billing.py")], garbage=True)
    with t.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": "tool_use as a string"}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": []}]}}) + "\n")
        f.write(json.dumps([1, 2, 3]) + " tool_use\n")
    assert handle(payload(axis, t), ENV, NOW).status == "saved"


@pytest.mark.parametrize("bad", ["", "missing.jsonl", None, 5])
def test_missing_or_unusable_transcript_skips_with_a_reason(axis, bad):
    p = payload(axis, bad if bad is not None else "")
    if bad is None:
        p.pop("transcript_path")
    if bad == 5:
        p["transcript_path"] = 5
    res = handle(p, ENV, NOW)
    assert res.status == "skipped" and "no file edits" in res.message and not handoff_files(axis)


def test_transcript_with_wrong_suffix_or_too_large_is_not_read(axis, tmp_path, monkeypatch):
    t = make_transcript(tmp_path, axis, [("Edit", axis / "app" / "billing.py")])
    renamed = t.rename(t.with_suffix(".txt"))
    assert handle(payload(axis, renamed), ENV, NOW).status == "skipped"
    monkeypatch.setattr(session_end, "MAX_TRANSCRIPT_BYTES", 10)
    big = make_transcript(tmp_path, axis, [("Edit", axis / "app" / "billing.py")], name="big.jsonl")
    res = handle(payload(axis, big), ENV, NOW)
    assert res.status == "skipped" and "too large" in res.message


def test_session_without_edits_writes_nothing_even_in_a_dirty_repo(axis, tmp_path):
    (axis / "preexisting_change.py").write_text("y = 2\n", encoding="utf-8")  # dirty tree from before the session
    t = make_transcript(tmp_path, axis, [])
    res = handle(payload(axis, t), ENV, NOW)
    assert res.status == "skipped" and not handoff_files(axis)


def test_bad_or_missing_session_id_is_handled(axis, edited):
    p = payload(axis, edited)
    del p["session_id"]
    assert handle(p, ENV, NOW).status == "skipped"
    assert handle(payload(axis, edited, session="///"), ENV, NOW).status == "skipped"
    res = handle(payload(axis, edited, session="../../evil"), ENV, NOW)  # traversal attempt
    assert res.status == "saved" and res.item.id.endswith("-evil")
    assert res.item.path.parent == axis / ".cognitive-graph" / "memory" / "handoff"
    assert not (axis / "evil.md").exists() and not (axis.parent / "evil.md").exists()


# --- project isolation -----------------------------------------------------------------------


def test_handoff_goes_only_to_the_resolved_project(tmp_path):
    axis, eros = make_project(tmp_path, "axis-ri"), make_project(tmp_path, "eros-innovation")
    t = make_transcript(tmp_path, axis, [("Edit", axis / "app" / "billing.py")])
    assert handle(payload(eros, t), ENV, NOW).status == "skipped"  # edits belong to Axis, not the active Eros
    assert not handoff_files(eros) and not handoff_files(axis)
    assert handle(payload(axis, t), ENV, NOW).status == "saved"
    assert len(handoff_files(axis)) == 1 and not handoff_files(eros)
    assert MemoryStore.open(axis).project_id != MemoryStore.open(eros).project_id


def test_subfolder_cwd_resolves_to_the_git_root_project(axis, edited):
    res = handle(payload(axis / "app", edited), ENV, NOW)
    assert res.status == "saved" and len(handoff_files(axis)) == 1


def test_no_project_means_no_write_and_no_fallback(tmp_path):
    other = tmp_path / "fresh"
    other.mkdir()
    git(tmp_path, "init", "-q", str(other))
    MemoryStore.init(tmp_path / "sibling", "sibling")  # some other project exists nearby
    (other / "a.py").write_text("x\n", encoding="utf-8")
    t = make_transcript(tmp_path, other, [("Write", other / "a.py")])
    res = handle(payload(other, t), {**ENV, "COGNITIVE_GRAPH_AUTO_INIT": "off"}, NOW)
    assert res.status == "skipped" and "memory init" in res.message
    assert not (other / ".cognitive-graph").exists() and not list((tmp_path / "sibling").rglob("H-*"))


def test_invalid_project_metadata_is_never_overwritten(tmp_path):
    root = tmp_path / "broken"
    (root / ".cognitive-graph").mkdir(parents=True)
    (root / ".cognitive-graph" / "project.json").write_text("garbage", encoding="utf-8")
    t = make_transcript(tmp_path, root, [("Write", root / "a.py")])
    assert handle(payload(root, t), ENV, NOW).status == "skipped"
    assert (root / ".cognitive-graph" / "project.json").read_text(encoding="utf-8") == "garbage"


# --- enable / disable ------------------------------------------------------------------------------


def test_environment_switch_disables_capture(axis, edited):
    assert handle(payload(axis, edited), {"COGNITIVE_GRAPH_HANDOFF": "off"}, NOW).status == "skipped"
    assert not handoff_files(axis)


def test_project_dotenv_switch_disables_capture(axis, edited, monkeypatch):
    monkeypatch.delenv("COGNITIVE_GRAPH_HANDOFF", raising=False)
    (axis / ".env").write_text("COGNITIVE_GRAPH_HANDOFF=off\n", encoding="utf-8")
    res = handle(payload(axis, edited), None, NOW)
    assert res.status == "skipped" and "switched off" in res.message and not handoff_files(axis)


# --- atomic and unique writes ---------------------------------------------------------------------------


def test_same_session_ending_twice_never_overwrites(axis, edited):
    first = handle(payload(axis, edited), ENV, NOW)
    original = first.item.path.read_text(encoding="utf-8")
    second = handle(payload(axis, edited), ENV, NOW)
    third = handle(payload(axis, edited), ENV, NOW)
    assert [first.item.id, second.item.id, third.item.id] == [
        "H-20260930T103015Z-4f1c9a7e", "H-20260930T103015Z-4f1c9a7e-2", "H-20260930T103015Z-4f1c9a7e-3"]
    assert first.item.path.read_text(encoding="utf-8") == original


def test_add_exclusive_refuses_duplicates_and_leaves_no_temp_files(axis):
    store = MemoryStore.open(axis)
    store.add_exclusive("handoff", "H-x-1", "t", "body")
    with pytest.raises(FileExistsError):
        store.add_exclusive("handoff", "H-x-1", "other", "other body")
    assert store.get("H-x-1").body == "body"
    assert not list((axis / ".cognitive-graph").rglob("*.tmp"))
    with pytest.raises(Exception):
        store.add_exclusive("handoff", "../escape", "t", "b")


def test_temp_files_are_invisible_to_readers(axis):
    store = MemoryStore.open(axis)
    d = axis / ".cognitive-graph" / "memory" / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".H-half.md.123.tmp").write_text("---\nid: partial", encoding="utf-8")
    assert store.items("handoff") == [] and store.warnings == []


def test_filesystem_without_hard_links_falls_back_safely(axis, edited, monkeypatch):
    def no_link(*a, **k):
        raise PermissionError("hard links unsupported")

    monkeypatch.setattr(os, "link", no_link)
    res = handle(payload(axis, edited), ENV, NOW)
    assert res.status == "saved" and MemoryStore.open(axis).get(res.item.id).trust == "proposed"
    assert not list((axis / ".cognitive-graph").rglob("*.tmp"))
    again = handle(payload(axis, edited), ENV, NOW)  # the fallback also refuses to overwrite
    assert again.item.id.endswith("-2")


def test_sequential_numbering_ignores_session_handoffs(axis, edited):
    handle(payload(axis, edited), ENV, NOW)
    assert MemoryStore.open(axis).add("handoff", "manual one").id == "H-0001"


def test_concurrent_writer_processes_never_clobber_each_other(axis, edited):
    """12 real processes, released together: 8 distinct sessions + 4 that share one session id."""
    env = {**os.environ, "PYTHONPATH": str(REPO), "COGNITIVE_GRAPH_HANDOFF": "on", "COGNITIVE_GRAPH_SYNC": "off"}
    sessions = [f"sess{i:04d}-aaaa" for i in range(8)] + ["shared00-bbbb"] * 4
    procs = [subprocess.Popen([sys.executable, "-m", "cognitive_graph.session_end"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) for _ in sessions]
    data = [json.dumps(payload(axis, edited, session=s)).encode() for s in sessions]
    for proc, d in zip(procs, data):  # every process is already imported and blocked on stdin; send together
        proc.stdin.write(d)
        proc.stdin.close()
    codes = [proc.wait(timeout=60) for proc in procs]
    errs = [proc.stderr.read().decode() for proc in procs]
    assert codes == [0] * 12, errs
    assert all("saved proposed handoff" in e for e in errs), errs
    store = MemoryStore.open(axis)
    items = store.items("handoff")
    assert store.warnings == [] and len(items) == 12 and len({i.id for i in items}) == 12
    assert sum(i.session == "shared00-bbbb" for i in items) == 4
    assert all(i.trust == "proposed" and "app/billing.py" in i.body for i in items)
    assert not list((axis / ".cognitive-graph").rglob("*.tmp"))


# --- failure handling --------------------------------------------------------------------------------


def test_write_failure_is_reported_without_partial_files(axis, edited, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(MemoryStore, "add_exclusive", boom)
    res = handle(payload(axis, edited), ENV, NOW)
    assert res.status == "error" and "OSError" in res.message and "disk full" not in res.message
    assert not handoff_files(axis)


def test_existing_memory_survives_a_failed_capture(axis, edited, monkeypatch):
    store = MemoryStore.open(axis)
    fact = store.add("fact", "Important existing fact", "keep me")
    before = fact.path.read_text(encoding="utf-8")
    monkeypatch.setattr(session_end, "git_snapshot", lambda root: (_ for _ in ()).throw(RuntimeError("git exploded")))
    with pytest.raises(RuntimeError):  # handle() itself propagates unexpected bugs...
        handle(payload(axis, edited), ENV, NOW)
    assert fact.path.read_text(encoding="utf-8") == before and not handoff_files(axis)


def test_main_never_fails_on_bad_input_or_bugs(monkeypatch, capsys):
    import io

    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(b"\xff\xfe not json")})())
    assert session_end.main() == 0
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(b'{"session_id": "abc12345"}')})())
    monkeypatch.setattr(session_end, "handle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    assert session_end.main() == 0
    assert "session-end hook error (RuntimeError)" in capsys.readouterr().err


def test_subprocess_entry_point_exit_code_and_stderr(axis, edited):
    env = {**os.environ, "PYTHONPATH": str(REPO), "COGNITIVE_GRAPH_SYNC": "off"}
    r = subprocess.run([sys.executable, "-m", "cognitive_graph.session_end"], input=json.dumps(payload(axis, edited)),
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0 and r.stdout == "" and "saved proposed handoff" in r.stderr
    bad = subprocess.run([sys.executable, "-m", "cognitive_graph.session_end"], input="not json",
                         capture_output=True, text=True, timeout=60, env=env)
    assert bad.returncode == 0


# --- retrieval in a new session --------------------------------------------------------------------------


def no_graph(res, prompt, cfg):
    return []


def test_new_session_retrieves_the_latest_handoff_labelled_unconfirmed(axis, edited):
    handle(payload(axis, edited, session="oldsession-1"), NOQ, datetime(2026, 9, 29, 9, 0, 0, tzinfo=timezone.utc))
    newest = handle(payload(axis, edited, session="newsession-2"), NOQ, NOW)
    out = prompt_handle({"cwd": str(axis), "prompt": "continue where we left off"}, HOOK_ENV, no_graph)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert newest.item.id in text and "UNCONFIRMED" in text and "handoff, active" in text
    assert text.count("] handoff,") == 1 and "oldsession" not in text  # only the newest handoff
    assert "app/billing.py" in text and "session ended (other)" in text
    assert len(text) <= 1800
    for canary in CANARIES:
        assert canary not in text


def test_handoff_is_found_by_naming_a_file_it_recorded(axis, edited):
    res = handle(payload(axis, edited), ENV, NOW)
    out = prompt_handle({"cwd": str(axis), "prompt": "why did the change to app/new_feature.py break the build?"}, HOOK_ENV, no_graph)
    assert res.item.id in out["hookSpecificOutput"]["additionalContext"]
    assert prompt_handle({"cwd": str(axis), "prompt": "please rename this variable to something clearer"}, HOOK_ENV, no_graph) is None


def test_brief_stays_within_the_size_limit_with_a_huge_handoff(axis, tmp_path):
    edits = [("Write", axis / "app" / f"module_number_{i}.py") for i in range(60)]
    res = handle(payload(axis, make_transcript(tmp_path, axis, edits)), ENV, NOW)
    assert res.status == "saved" and "(+45 more)" in res.body or "... and" in res.body
    out = prompt_handle({"cwd": str(axis), "prompt": "continue where we left off"}, {**HOOK_ENV, "COGNITIVE_GRAPH_HOOK_MAX_CHARS": "700"}, no_graph)
    assert len(out["hookSpecificOutput"]["additionalContext"]) <= 700


def test_other_projects_handoffs_never_appear(tmp_path):
    axis, eros = make_project(tmp_path, "axis-ri"), make_project(tmp_path, "eros-innovation")
    handle(payload(axis, make_transcript(tmp_path, axis, [("Edit", axis / "app" / "billing.py")])), ENV, NOW)
    assert prompt_handle({"cwd": str(eros), "prompt": "continue where we left off"}, HOOK_ENV, no_graph) is None


def test_confirming_a_handoff_changes_its_label(axis, edited):
    res = handle(payload(axis, edited), ENV, NOW)
    MemoryStore.open(axis).confirm(res.item.id)
    text = prompt_handle({"cwd": str(axis), "prompt": "continue where we left off"}, HOOK_ENV, no_graph)["hookSpecificOutput"]["additionalContext"]
    assert "handoff, active, confirmed" in text and "handoff, active, UNCONFIRMED" not in text


# --- installer -------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    home.mkdir()
    monkeypatch.setattr(hook_cli.Path, "home", classmethod(lambda cls: home))
    return home


def events_in(path):
    return sorted(json.loads(path.read_text(encoding="utf-8")).get("hooks", {}))


@pytest.mark.parametrize("scope", ["local", "project", "user"])
def test_install_adds_both_hooks_and_handoff_can_be_toggled(scope, tmp_path, fake_home):
    proj = tmp_path / "proj"
    proj.mkdir()
    path = hook_cli.settings_path(scope, proj)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": "keep", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}), encoding="utf-8")
    assert hook_cli.run(["--project", str(proj), "install", "--scope", scope]) == 0
    assert events_in(path) == ["SessionEnd", "SessionStart", "Stop", "UserPromptSubmit"]
    end = json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionEnd"][0]["hooks"][0]
    assert end["type"] == "command" and end["timeout"] == 15 and "session" in end["command"]  # above Claude Code's 1.5 s default
    hook_cli.run(["--project", str(proj), "install", "--scope", scope])  # idempotent
    assert len(json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionEnd"]) == 1
    assert hook_cli.run(["--project", str(proj), "handoff", "disable", "--scope", scope]) == 0
    assert events_in(path) == ["SessionStart", "Stop", "UserPromptSubmit"]
    assert hook_cli.run(["--project", str(proj), "handoff", "enable", "--scope", scope]) == 0
    assert events_in(path) == ["SessionEnd", "SessionStart", "Stop", "UserPromptSubmit"]
    assert hook_cli.run(["--project", str(proj), "uninstall", "--scope", scope]) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {"model": "keep", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}


def test_no_handoff_flag_installs_only_the_prompt_hook(tmp_path, fake_home):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert hook_cli.run(["--project", str(proj), "install", "--no-handoff"]) == 0
    assert events_in(hook_cli.settings_path("user", proj)) == ["SessionStart", "UserPromptSubmit"]  # default scope is user


def test_status_reports_both_events(tmp_path, fake_home, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    hook_cli.run(["--project", str(proj), "install"])
    capsys.readouterr()
    hook_cli.run(["--project", str(proj), "status"])
    out = capsys.readouterr().out
    assert out.count("SessionEnd") == 3 and out.count("UserPromptSubmit") == 3 and "session_end" in out or "session-end" in out


def test_preview_prints_and_writes_nothing(axis, edited, capsys):
    assert hook_cli.run(["--project", str(axis), "handoff", "preview", "--transcript", str(edited)]) == 0
    out = capsys.readouterr().out
    assert "preview only" in out and "app/billing.py" in out and not handoff_files(axis)
    assert hook_cli.run(["--project", str(axis), "handoff", "preview"]) == 0
    assert "No handoff would be saved" in capsys.readouterr().out


def test_real_home_is_never_used_by_these_tests(fake_home):
    assert hook_cli.settings_path("user", Path("x")) == fake_home / ".claude" / "settings.json"
