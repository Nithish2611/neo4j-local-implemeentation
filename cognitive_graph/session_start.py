"""Claude Code `SessionStart` hook: the hands-free start of every session.

Registered as `cognitive-graph-session-start` (see `cognitive-graph hook install`). When Claude Code
opens in a Git project it:

  1. initialises the project automatically if it has no metadata yet (stable random id in
     `.cognitive-graph/project.json`, kept out of `git status`; see `auto_init_project`),
  2. starts the background graph sync (initial indexing the first time, changed files afterwards),
  3. for fresh sessions (`startup`, `clear`, `compact`) offers the newest session handoff, labelled
     proposed/unconfirmed, as one short line of context.

It never waits for indexing, never blocks, and always exits 0. Anything worth telling the user
(a project was initialised, Neo4j is unreachable, indexing is in progress) is returned as a
`systemMessage`, shown to the user by Claude Code.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from .memory import MemoryStore, auto_init_project
from .retrieval import _line

FRESH_SOURCES = ("startup", "clear", "compact")  # a resumed/forked session already has its context
DEFAULT_MAX_AGE_DAYS = 14
MAX_CHARS = 700


def _age_days(created: str, now: datetime) -> float:
    try:
        return (now - datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 86400
    except ValueError:
        return 0.0


def latest_handoff_context(root, name: str, env, now: datetime) -> str | None:
    if str(env.get("COGNITIVE_GRAPH_START_HANDOFF", "on")).strip().lower() in ("0", "off", "false", "no"):
        return None
    try:
        max_days = float(env.get("COGNITIVE_GRAPH_START_HANDOFF_DAYS", DEFAULT_MAX_AGE_DAYS))
    except (TypeError, ValueError):
        max_days = DEFAULT_MAX_AGE_DAYS
    store = MemoryStore(root)
    handoffs = sorted(store.items("handoff"), key=lambda i: (i.created, i.id))
    if not handoffs or _age_days(handoffs[-1].created, now) > max_days:
        return None
    text = (f'[cognitive-graph] Latest session handoff for project "{name}" (retrieved locally from this project only; '
            "UNCONFIRMED = not accepted by the user; it may not relate to today's task):\n" + _line(handoffs[-1], store.root))
    return text[:MAX_CHARS]


def handle(payload: dict, environ=None, sync=None, now: datetime | None = None) -> dict | None:
    """Pure core: hook input dict -> hook output dict (None = say nothing)."""
    now = now or datetime.now(timezone.utc)
    env = dict(os.environ if environ is None else environ)
    if str(env.get("COGNITIVE_GRAPH_HOOK", "on")).strip().lower() in ("0", "off", "false", "no"):
        return None
    res, created = auto_init_project(payload.get("cwd") or os.getcwd(), env)
    if not res.ok:
        if res.status == "invalid":
            return {"systemMessage": f"cognitive-graph: {res.message}. Continuing without project context."}
        return None  # not a Git project (or auto-init is off): nothing to do, nothing to say
    if environ is None:
        try:
            from .hook import _env_for

            env = _env_for(res.root)
        except Exception:
            pass

    notes: list[str] = []
    if created:
        notes.append(f"initialised project \"{res.name}\" (id {res.project_id}); memory is stored in "
                     ".cognitive-graph/ and kept out of git status")
    try:
        from .graph_sync import SyncConfig, maybe_spawn_sync

        note = (sync or maybe_spawn_sync)(res, SyncConfig.from_mapping(env), force=True)
        if note:
            notes.append(note)
    except Exception as exc:
        notes.append(f"graph sync could not start ({type(exc).__name__})")

    context = None
    if payload.get("source") in FRESH_SOURCES:
        try:
            context = latest_handoff_context(res.root, res.name, env, now)
        except Exception:
            context = None

    out: dict = {}
    if context:
        out["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": context}
    if notes:
        out["systemMessage"] = "cognitive-graph: " + "; ".join(notes)
    return out or None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
        out = handle(payload if isinstance(payload, dict) else {})
    except Exception as exc:  # any failure: the session starts normally
        out = {"systemMessage": f"cognitive-graph session-start hook error ({type(exc).__name__}); continuing normally."}
    if out:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
