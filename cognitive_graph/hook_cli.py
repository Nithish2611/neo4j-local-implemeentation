"""`cognitive-graph hook ...` - manage the Claude Code hooks (session start, prompt context, session-end handoff).

    hook install [--scope user|project|local] [--no-handoff]   one-time setup; user scope covers every project
    hook uninstall [--scope ...]                               remove all
    hook handoff enable|disable [--scope ...]                  add / remove only the SessionEnd hook
    hook handoff preview [--transcript FILE]                   show what a handoff would contain
    hook status | hook test "prompt"
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

from .hook import handle
from .memory import git_root, resolve_project

PROMPT_EVENT, END_EVENT, START_EVENT = "UserPromptSubmit", "SessionEnd", "SessionStart"
EVENTS = {
    START_EVENT: ("cognitive-graph-session-start", "cognitive_graph.session_start", 10),
    # event: (console command, python module, timeout seconds written into settings)
    PROMPT_EVENT: ("cognitive-graph-hook", "cognitive_graph.hook", 10),
    END_EVENT: ("cognitive-graph-session-end", "cognitive_graph.session_end", 15),  # Claude Code's default is 1.5 s
}


def settings_path(scope: str, base: Path) -> Path:
    """Where Claude Code reads hooks for `scope`. Project-level settings are read only from
    the folder Claude Code is LAUNCHED in (verified: a hook in the Git root's .claude/ does not
    fire when Claude Code is started in a subfolder), so local/project use `base` exactly."""
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    return base / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")


def hook_command(event: str = PROMPT_EVENT, force_python: bool = False) -> tuple[str, str]:
    """(command, note). Prefers the bare packaged command; otherwise `python -m <module>`."""
    exe, module, _ = EVENTS[event]
    if not force_python and shutil.which(exe):
        return exe, ""
    note = (f"{exe} is not on PATH, so the command uses this Python interpreter "
            "(machine-specific; fine for local/user scope, avoid committing it).")
    return f'"{Path(sys.executable).as_posix()}" -m {module}', note


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError("top-level JSON is not an object")
    return data


def _ours(command: str, event: str | None = None) -> bool:
    events = [event] if event else list(EVENTS)
    return any(m in command for e in events for m in EVENTS[e][:2])


def _is_ours(group: dict, event: str | None = None) -> bool:
    return any(_ours(h.get("command", ""), event) for h in group.get("hooks", []) if isinstance(h, dict))


def _strip(data: dict, events=None) -> int:
    """Remove our entries (for `events`, default all) from data in place; returns how many."""
    removed = 0
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in events or list(EVENTS):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            continue
        kept = [g for g in groups if not (isinstance(g, dict) and _is_ours(g, event))]
        removed += len(groups) - len(kept)
        if len(kept) != len(groups):
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not hooks and removed:
        del data["hooks"]
    return removed


def install(path: Path, command: str, event: str = PROMPT_EVENT) -> str:
    data = _load(path)  # raises on invalid JSON: a file we cannot parse is never overwritten
    _strip(data, [event])
    data.setdefault("hooks", {}).setdefault(event, []).append(
        {"hooks": [{"type": "command", "command": command, "timeout": EVENTS[event][2]}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"Installed {event} hook in {path}"


def uninstall(path: Path, events=None) -> str:
    if not path.exists():
        return f"Nothing to remove ({path} does not exist)"
    data = _load(path)
    removed = _strip(data, events)
    if removed:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"Removed {removed} cognitive-graph hook entr{'y' if removed == 1 else 'ies'} from {path}"


def installed_command(path: Path, event: str = PROMPT_EVENT) -> str | None:
    try:
        data = _load(path)
    except (OSError, ValueError):
        return None
    groups = data.get("hooks", {}).get(event, [])
    for g in groups if isinstance(groups, list) else []:
        if isinstance(g, dict):
            for h in g.get("hooks", []):
                if isinstance(h, dict) and _ours(h.get("command", ""), event):
                    return h["command"]
    return None


def _install_event(a, base: Path, event: str) -> None:
    cmd, note = hook_command(event, getattr(a, "python", False))
    print(install(settings_path(a.scope, base), cmd, event))
    print(f"  Command: {cmd}")
    if note:
        print(f"  Note: {note}")


def run(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="cognitive-graph hook", description="Claude Code hooks for cognitive-graph")
    p.add_argument("--project", default=".", help="project folder (default: current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)
    scope_help = ("user: ~/.claude/settings.json, every project (default; install once); "
                  "local: <folder>/.claude/settings.local.json (git-ignored); "
                  "project: <folder>/.claude/settings.json (shared)")
    for name in ("install", "uninstall"):
        s = sub.add_parser(name)
        s.add_argument("--scope", choices=["local", "project", "user"], default="user", help=scope_help)
        if name == "install":
            s.add_argument("--python", action="store_true",
                           help="use `python -m ...` even if the commands are on PATH")
            s.add_argument("--no-handoff", action="store_true", help="install only the prompt-context hook")
    h = sub.add_parser("handoff", help="session-end handoff: enable | disable | preview")
    hs = h.add_subparsers(dest="action", required=True)
    for name in ("enable", "disable"):
        s = hs.add_parser(name)
        s.add_argument("--scope", choices=["local", "project", "user"], default="user", help=scope_help)
        if name == "enable":
            s.add_argument("--python", action="store_true")
    pv = hs.add_parser("preview", help="print the handoff that would be saved now; writes nothing")
    pv.add_argument("--transcript", help="a Claude Code transcript .jsonl to read edited-file paths from")
    sub.add_parser("status", help="show install state, the resolved project and graph reachability")
    t = sub.add_parser("test", help="run the prompt hook on a sample prompt, as Claude Code would")
    t.add_argument("prompt")
    a = p.parse_args(argv)
    base = Path(a.project).resolve()

    try:
        if a.cmd == "install":
            _install_event(a, base, START_EVENT)
            _install_event(a, base, PROMPT_EVENT)
            if not a.no_handoff:
                _install_event(a, base, END_EVENT)
            root = git_root(base)
            if a.scope != "user" and root and root != base:
                print(f"Warning: {base} is not the Git root ({root}). Claude Code only reads this file when "
                      "launched from this exact folder; use --scope user to cover every folder.")
            print("Restart Claude Code (or open /hooks) so it picks up the change.")
        elif a.cmd == "uninstall":
            print(uninstall(settings_path(a.scope, base)))
        elif a.cmd == "handoff":
            if a.action == "enable":
                _install_event(a, base, END_EVENT)
                print("Restart Claude Code (or open /hooks) so it picks up the change.")
            elif a.action == "disable":
                print(uninstall(settings_path(a.scope, base), [END_EVENT]))
            else:
                from .session_end import handle as end_handle

                res = end_handle({"session_id": "preview", "cwd": str(base), "reason": "preview",
                                  "transcript_path": a.transcript or ""}, write=False)
                if res.status == "saved":
                    print(f"{res.title}\n\n{res.body}\n\nevidence: {'; '.join(res.evidence)}\n(preview only; nothing was written)")
                else:
                    print(f"No handoff would be saved: {res.message}")
        elif a.cmd == "status":
            for scope in ("local", "project", "user"):
                path = settings_path(scope, base)
                print(f"{scope:8} {path}")
                for event in EVENTS:
                    print(f"           {event:16} {installed_command(path, event) or 'not installed'}")
            print("commands on PATH: " + ", ".join(f"{e[0]}={'yes' if shutil.which(e[0]) else 'no'}" for e in EVENTS.values()))
            res = resolve_project(base)
            print(f"project: {res.status} - "
                  + (f"{res.name} (id {res.project_id}, graph scope {res.graph_id}) at {res.root}" if res.ok else res.message))
            if res.ok:
                from .graph_sync import SyncLock, load_state

                st = load_state(res.root)
                print(f"graph sync: {st.get('status', 'never run')} - {st.get('message', '')}; "
                      f"{len(st.get('files', {}))} file(s) indexed, {st.get('pending', 0)} pending"
                      + ("; running now" if SyncLock.is_fresh(res.root) else ""))
                from .retrieval import HookConfig, neo4j_fetch
                try:
                    neo4j_fetch(res, "", HookConfig(timeout=2.0))
                    print("Neo4j: reachable")
                except Exception as exc:
                    print(f"Neo4j: unavailable ({type(exc).__name__}); the prompt hook will use memory only")
        elif a.cmd == "test":
            out = handle({"cwd": str(base), "prompt": a.prompt}) or {}
            print(out.get("hookSpecificOutput", {}).get("additionalContext", "(no context would be injected)"))
            if "systemMessage" in out:
                print(f"\n[shown to user] {out['systemMessage']}")
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}. Nothing was changed.", file=sys.stderr)
        return 1
    return 0
