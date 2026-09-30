"""`cognitive-graph hook install|uninstall|status|test` - manage the Claude Code hook."""
import argparse
import json
import shutil
import sys
from pathlib import Path

from .hook import handle
from .memory import git_root, resolve_project

MARKERS = ("cognitive-graph-hook", "cognitive_graph.hook")
TIMEOUT_SECONDS = 10


def settings_path(scope: str, base: Path) -> Path:
    """Where Claude Code reads hooks for `scope`. Project-level settings are read only from
    the folder Claude Code is LAUNCHED in (verified: a hook in the Git root's .claude/ does not
    fire when Claude Code is started in a subfolder), so local/project use `base` exactly."""
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    return base / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")


def hook_command(force_python: bool = False) -> tuple[str, str]:
    """(command, note). Prefers the bare packaged command; otherwise `python -m`."""
    if not force_python and shutil.which("cognitive-graph-hook"):
        return "cognitive-graph-hook", ""
    note = ("cognitive-graph-hook is not on PATH, so the command uses this Python interpreter "
            "(machine-specific; fine for local/user scope, avoid committing it).")
    return f'"{Path(sys.executable).as_posix()}" -m cognitive_graph.hook', note


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError("top-level JSON is not an object")
    return data


def _ours(command: str) -> bool:
    return any(m in command for m in MARKERS)


def _is_ours(group: dict) -> bool:
    return any(_ours(h.get("command", "")) for h in group.get("hooks", []) if isinstance(h, dict))


def _strip(data: dict) -> int:
    """Remove our entries from data in place; returns how many were removed."""
    hooks = data.get("hooks")
    groups = hooks.get("UserPromptSubmit", []) if isinstance(hooks, dict) else []
    kept = [g for g in groups if not (isinstance(g, dict) and _is_ours(g))]
    removed = len(groups) - len(kept)
    if removed:
        if kept:
            hooks["UserPromptSubmit"] = kept
        else:
            del hooks["UserPromptSubmit"]
            if not hooks:
                del data["hooks"]
    return removed


def install(path: Path, command: str) -> str:
    data = _load(path)  # raises on invalid JSON: a file we cannot parse is never overwritten
    _strip(data)
    data.setdefault("hooks", {}).setdefault("UserPromptSubmit", []).append(
        {"hooks": [{"type": "command", "command": command, "timeout": TIMEOUT_SECONDS}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"Installed in {path}"


def uninstall(path: Path) -> str:
    if not path.exists():
        return f"Nothing to remove ({path} does not exist)"
    data = _load(path)
    removed = _strip(data)
    if removed:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"Removed {removed} cognitive-graph hook entr{'y' if removed == 1 else 'ies'} from {path}"


def installed_command(path: Path) -> str | None:
    try:
        data = _load(path)
    except (OSError, ValueError):
        return None
    for g in data.get("hooks", {}).get("UserPromptSubmit", []):
        if isinstance(g, dict):
            for h in g.get("hooks", []):
                if isinstance(h, dict) and _ours(h.get("command", "")):
                    return h["command"]
    return None


def run(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="cognitive-graph hook", description="Claude Code UserPromptSubmit hook")
    p.add_argument("--project", default=".", help="project folder (default: current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("install", "uninstall"):
        s = sub.add_parser(name)
        s.add_argument("--scope", choices=["local", "project", "user"], default="local",
                       help="local: <project>/.claude/settings.local.json (default, git-ignored); "
                            "project: <project>/.claude/settings.json (shared); "
                            "user: ~/.claude/settings.json (every project)")
        if name == "install":
            s.add_argument("--python", action="store_true",
                           help="use `python -m cognitive_graph.hook` even if the command is on PATH")
    sub.add_parser("status", help="show install state, the resolved project and graph reachability")
    t = sub.add_parser("test", help="run the hook on a sample prompt, as Claude Code would")
    t.add_argument("prompt")
    a = p.parse_args(argv)
    base = Path(a.project).resolve()

    try:
        if a.cmd == "install":
            cmd, note = hook_command(a.python)
            print(install(settings_path(a.scope, base), cmd))
            print(f"Command: {cmd}")
            root = git_root(base)
            if a.scope != "user" and root and root != base:
                print(f"Warning: {base} is not the Git root ({root}). Claude Code only reads this file when "
                      "launched from this exact folder; use --scope user to cover every folder.")
            if note:
                print(f"Note: {note}")
            print("Restart Claude Code (or open /hooks) so it picks up the change.")
        elif a.cmd == "uninstall":
            print(uninstall(settings_path(a.scope, base)))
        elif a.cmd == "status":
            for scope in ("local", "project", "user"):
                path = settings_path(scope, base)
                print(f"{scope:8} {path}: {installed_command(path) or 'not installed'}")
            print(f"command on PATH: {shutil.which('cognitive-graph-hook') or 'no'}")
            res = resolve_project(base)
            print(f"project: {res.status} - "
                  + (f"{res.name} (id {res.project_id}) at {res.root}" if res.ok else res.message))
            if res.ok:
                from .retrieval import HookConfig, neo4j_fetch
                try:
                    neo4j_fetch(res, "", HookConfig(timeout=2.0))
                    print("Neo4j: reachable")
                except Exception as exc:
                    print(f"Neo4j: unavailable ({type(exc).__name__}); the hook will use memory only")
        elif a.cmd == "test":
            out = handle({"cwd": str(base), "prompt": a.prompt}) or {}
            print(out.get("hookSpecificOutput", {}).get("additionalContext", "(no context would be injected)"))
            if "systemMessage" in out:
                print(f"\n[shown to user] {out['systemMessage']}")
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}. Nothing was changed.", file=sys.stderr)
        return 1
    return 0
