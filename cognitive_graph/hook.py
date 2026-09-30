"""Claude Code `UserPromptSubmit` hook: adds a small project-scoped brief before each prompt.

Registered as the `cognitive-graph-hook` command (see `cognitive-graph hook install`).
Reads the hook JSON from stdin, prints hook JSON to stdout, and ALWAYS exits 0: it never
blocks or rewrites the prompt. Nothing is logged to disk.
"""
import json
import os
import sys

from .memory import auto_init_project
from .retrieval import GraphFetch, HookConfig, prompt_brief


def _env_for(root) -> dict:
    env: dict = {}
    try:
        from dotenv import dotenv_values

        env.update({k: v for k, v in dotenv_values(root / ".env").items() if v is not None})
    except Exception:
        pass
    env.update(os.environ)
    return env


def handle(payload: dict, environ=None, graph_fetch: GraphFetch | None = None, sync=None) -> dict | None:
    """Pure core: hook input dict -> hook output dict (None = add nothing). `sync` defaults to the
    real background-sync trigger (tests pass a stub)."""
    prompt = payload.get("prompt") or payload.get("prompt_text") or ""
    cwd = payload.get("cwd") or os.getcwd()
    env = dict(os.environ if environ is None else environ)
    cfg = HookConfig.from_mapping(env)
    if not cfg.enabled:
        return None

    res, created = auto_init_project(cwd, env)  # first prompt in a fresh Git project: set it up automatically
    if not res.ok:
        if res.status == "invalid":
            return {"systemMessage": f"cognitive-graph: {res.message}. Continuing without project context."}
        return {"systemMessage": f"cognitive-graph: {res.message}"} if cfg.verbose else None

    if environ is None:  # a project's .env may tune the hook; the real environment still wins
        env = _env_for(res.root)
        cfg = HookConfig.from_mapping(env)
        if not cfg.enabled:
            return None
    notes: list[str] = []
    if created:
        notes.append(f"initialised project \"{res.name}\" (id {res.project_id}); memory is stored in "
                     ".cognitive-graph/ and kept out of git status")
    if not prompt.lstrip().startswith("/"):
        try:  # keep the code graph current; never waits for indexing
            from .graph_sync import SyncConfig, maybe_spawn_sync

            note = (sync or maybe_spawn_sync)(res, SyncConfig.from_mapping(env))
            if note:
                notes.append(note)
        except Exception as exc:
            notes.append(f"graph sync check failed ({type(exc).__name__})")
    if len(prompt.strip()) < 8 or prompt.lstrip().startswith("/"):
        return {"systemMessage": "cognitive-graph: " + "; ".join(notes)} if notes else None

    brief, diags = prompt_brief(res, prompt, cfg, graph_fetch)
    diags += notes
    out: dict = {}
    if brief:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": brief}
    elif cfg.verbose:
        diags.append(f"no relevant project memory found for {res.name}")
    if diags:
        out["systemMessage"] = "cognitive-graph: " + "; ".join(diags)
    return out or None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
        out = handle(payload if isinstance(payload, dict) else {})
    except Exception as exc:  # any failure: let the prompt through untouched
        out = {"systemMessage": f"cognitive-graph hook error ({type(exc).__name__}); continuing without project context."}
    if out:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
