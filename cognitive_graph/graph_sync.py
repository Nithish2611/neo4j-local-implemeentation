"""Automatic, bounded, incremental code-graph synchronisation for one project.

Nothing here runs on your behalf unless a hook triggers it (SessionStart, UserPromptSubmit,
SessionEnd). The hooks never wait for indexing: they do a cheap change scan and, if something
changed, start `python -m cognitive_graph.graph_sync --root <project>` as a detached background
process (verified to survive Claude Code exiting). That process:

  * lists indexable source files (`git ls-files --cached --others --exclude-standard`, so
    .gitignore is respected; a directory walk outside Git) with (size, mtime) signatures,
  * compares them with `.cognitive-graph/sync-state.json` (what this checkout last indexed),
  * re-ingests ONLY new/changed files, deletes removed ones, in resumable batches,
  * is capped per run (files and seconds), holds a per-project lock, and records its status.

The first run for a project simply finds every file "new", so initial indexing is automatic.
The graph itself stays scoped by the project's graph id; state, lock and marker files are
local to the checkout and never contain source text.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .memory import MEMORY_DIR, ProjectResolution, resolve_project

STATE_NAME, CHECK_NAME, LOCK_NAME = "sync-state.json", "sync-check", "sync.lock"
LOCAL_FILES = (STATE_NAME, LOCK_NAME, CHECK_NAME)
LOCK_STALE_SECONDS = 120     # a lock nobody has touched for this long belongs to a dead process
UNAVAILABLE_BACKOFF = 120    # after a failed attempt (Neo4j down) wait this long before spawning again
SCAN_BUDGET = 0.5            # seconds the hooks may spend scanning inline before deferring to the background


@dataclass
class SyncConfig:
    enabled: bool = True
    interval: float = 30.0    # minimum seconds between per-prompt change scans
    max_files: int = 3000     # files re-indexed per run; the rest continue on the next run
    max_seconds: float = 480  # wall-clock cap for one background run
    batch: int = 100          # files per Neo4j batch (state is saved after each)

    @classmethod
    def from_mapping(cls, env: Mapping[str, str]) -> "SyncConfig":
        def num(key, default, cast):
            try:
                return max(cast(env.get(key, default)), 1)
            except (TypeError, ValueError):
                return default

        on = str(env.get("COGNITIVE_GRAPH_SYNC", "on")).strip().lower() not in ("0", "off", "false", "no", "")
        return cls(on, num("COGNITIVE_GRAPH_SYNC_INTERVAL", cls.interval, float),
                   num("COGNITIVE_GRAPH_SYNC_MAX_FILES", cls.max_files, int),
                   num("COGNITIVE_GRAPH_SYNC_MAX_SECONDS", cls.max_seconds, float), cls.batch)


@dataclass
class SyncResult:
    status: str        # ok | busy | unavailable | error | disabled
    message: str = ""
    indexed: int = 0
    removed: int = 0
    remaining: int = 0


# --- local state (atomic, never holds source text) ---------------------------------------------


def _dir(root: Path) -> Path:
    return Path(root) / MEMORY_DIR


def load_state(root: Path) -> dict:
    try:
        data = json.loads((_dir(root) / STATE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(root: Path, state: dict) -> None:
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    ignore = d / ".gitignore"  # if the folder is committed, the local sync files must not be
    if not ignore.exists():
        try:
            ignore.write_text("\n".join(LOCAL_FILES) + "\n*.tmp\n", encoding="utf-8")
        except OSError:
            pass
    tmp = d / f".{STATE_NAME}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, d / STATE_NAME)


def record_status(root: Path, status: str, message: str = "") -> None:
    state = load_state(root)
    state.update(status=status, message=message[:200], finished=time.time())
    save_state(root, state)


class SyncLock:
    """One sync per project checkout. Stale locks (no heartbeat for 2 minutes) are taken over."""

    def __init__(self, root: Path) -> None:
        self.path = _dir(root) / LOCK_NAME
        self._held = False

    @staticmethod
    def is_fresh(root: Path) -> bool:
        try:
            return time.time() - (_dir(root) / LOCK_NAME).stat().st_mtime < LOCK_STALE_SECONDS
        except OSError:
            return False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self._held = True
                return True
            except FileExistsError:
                if SyncLock.is_fresh(self.path.parent.parent):
                    return False
                self.path.unlink(missing_ok=True)  # stale: previous run died
        return False

    def heartbeat(self) -> None:
        try:
            os.utime(self.path)
        except OSError:
            pass

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False


# --- change detection ------------------------------------------------------------------------------


def _git_listing(root: Path) -> list[str] | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _walk_listing(root: Path) -> list[str]:
    from .indexing_rules import SKIP_DIRS

    found = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d != MEMORY_DIR]
        rel = Path(dirpath).relative_to(root).as_posix()
        found += [name if rel == "." else f"{rel}/{name}" for name in names]
    return found


def source_files(root: Path, suffixes, deadline: float | None = None) -> tuple[dict[str, list[int]], bool]:
    """{relative posix path: [size, mtime_ns]} for indexable files, and whether the scan finished
    before `deadline` (a time.monotonic() value)."""
    from .indexing_rules import is_indexable

    root = Path(root)
    names = _git_listing(root)
    if names is None:
        names = _walk_listing(root)
    found: dict[str, list[int]] = {}
    for n, rel in enumerate(names):
        if deadline is not None and n % 200 == 0 and time.monotonic() > deadline:
            return found, False
        if not is_indexable(tuple(rel.split("/")), suffixes):
            continue
        try:
            st = os.stat(root / rel)
        except OSError:
            continue  # listed by Git but gone from disk
        if st.st_mode & 0o170000 == 0o100000:
            found[rel] = [st.st_size, st.st_mtime_ns]
    return found, True


def plan_changes(current: dict[str, list[int]], indexed: dict[str, list[int]]) -> tuple[list[str], list[str]]:
    todo = sorted(rel for rel, sig in current.items() if indexed.get(rel) != sig)
    gone = sorted(set(indexed) - set(current))
    return todo, gone


# --- the sync itself -------------------------------------------------------------------------------------


def run_sync(res: ProjectResolution, db, parser, cfg: SyncConfig, full: bool = False) -> SyncResult:
    """Bring `db` (a graph handle scoped to res.graph_id) up to date with the working tree."""
    from .ingestor import Ingestor

    root = res.root
    lock = SyncLock(root)
    if not lock.acquire():
        return SyncResult("busy", "another sync is already running for this project")
    try:
        state = load_state(root)
        if full or state.get("graph_id") != res.graph_id:
            state = {"schema": 1, "graph_id": res.graph_id, "files": {}}
        indexed: dict[str, list[int]] = state.setdefault("files", {})
        if indexed and db.stats()["files"] == 0:  # the graph was cleared behind our back
            indexed.clear()
        current, _ = source_files(root, parser.supported_extensions)
        todo, gone = plan_changes(current, indexed)
        started = time.time()
        state.update(status="running", started=started, message="", graph_id=res.graph_id)
        if not todo and not gone:
            state.update(status="ok", finished=time.time(), message="up to date", pending=0)
            save_state(root, state)
            return SyncResult("ok", "up to date")
        save_state(root, state)

        remaining = max(len(todo) - cfg.max_files, 0)
        todo = todo[: cfg.max_files]
        if gone:
            db.delete_files(gone)
            for rel in gone:
                indexed.pop(rel, None)
        deadline = time.monotonic() + cfg.max_seconds
        ingestor, done = Ingestor(parser, db), 0
        for i in range(0, len(todo), cfg.batch):
            if time.monotonic() > deadline:
                remaining += len(todo) - i
                break
            chunk = todo[i:i + cfg.batch]
            lock.heartbeat()
            ingestor.ingest_files(root, chunk)
            for rel in chunk:  # unparseable files are recorded too, so they retry only when they change
                indexed[rel] = current[rel]
            done += len(chunk)
            state.update(files=indexed, pending=remaining + len(todo) - i - len(chunk))
            save_state(root, state)
        state.update(status="ok", finished=time.time(), pending=remaining,
                     message=f"indexed {done} file(s), removed {len(gone)}" + (f", {remaining} still pending" if remaining else ""))
        save_state(root, state)
        return SyncResult("ok", state["message"], done, len(gone), remaining)
    finally:
        lock.release()


def _spawn_detached(root: Path) -> None:
    cmd = [sys.executable, "-m", "cognitive_graph.graph_sync", "--root", str(root)]
    kwargs: dict = {}
    if os.name == "nt":  # survives Claude Code exiting (verified); no console window
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, cwd=str(root), **kwargs)


def _neo4j_down_reason(root: Path) -> str | None:
    """A fast (<0.5 s) TCP check of the configured Neo4j address; None when it is listening."""
    try:
        from dotenv import dotenv_values

        env = {**{k: v for k, v in dotenv_values(Path(root) / ".env").items() if v is not None}, **os.environ}
    except Exception:
        env = dict(os.environ)
    from .retrieval import GraphUnavailable, _require_listening

    try:
        _require_listening(env.get("NEO4J_URI", "bolt://127.0.0.1:7687"))
    except GraphUnavailable:
        return "Neo4j is not reachable"
    return None


def maybe_spawn_sync(res: ProjectResolution, cfg: SyncConfig, force: bool = False, spawn=None) -> str | None:
    """Called by hooks. Never waits for indexing. Returns a short user-facing note when there is
    something worth reporting (initial indexing, indexing in progress, sync unavailable), else None."""
    if not cfg.enabled or not res.ok:
        return None
    root = res.root
    state = load_state(root)
    first = not state.get("files") or state.get("graph_id") != res.graph_id
    if SyncLock.is_fresh(root):
        return "code graph is being indexed in the background; graph results may be incomplete" if first else None
    now = time.time()
    if state.get("status") == "unavailable" and now - state.get("finished", 0) < UNAVAILABLE_BACKOFF:
        return f"graph sync unavailable ({state.get('message', 'unknown reason')}); will retry"
    marker = _dir(root) / CHECK_NAME
    if not force:
        try:
            if now - marker.stat().st_mtime < cfg.interval:
                return None
        except OSError:
            pass
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        from .indexing_rules import SUPPORTED_SUFFIXES

        current, complete = source_files(root, SUPPORTED_SUFFIXES, deadline=time.monotonic() + SCAN_BUDGET)
        indexed = {} if state.get("graph_id") != res.graph_id else state.get("files", {})
        todo, gone = plan_changes(current, indexed)
        if complete and not todo and not gone:
            return None
        if spawn is None:  # real run: don't start a background process just to find Neo4j down
            reason = _neo4j_down_reason(root)
            if reason:
                record_status(root, "unavailable", reason)
                return f"graph sync unavailable ({reason}); will retry"
        (spawn or _spawn_detached)(root)
    except Exception as exc:  # a scan problem must never disturb the hook
        return f"could not start graph sync ({type(exc).__name__})"
    return "code graph initial indexing started in the background; graph results may be incomplete" if first else None


# --- entry points ----------------------------------------------------------------------------------------


def _run_for(root: Path, full: bool, quiet_when_down: bool = True) -> SyncResult:
    res = resolve_project(root)
    if not res.ok:
        return SyncResult("error", res.message)
    try:
        from dotenv import dotenv_values

        env = {**{k: v for k, v in dotenv_values(res.root / ".env").items() if v is not None}, **os.environ}
    except Exception:
        env = dict(os.environ)
    cfg = SyncConfig.from_mapping(env)
    if not cfg.enabled:
        return SyncResult("disabled", "graph sync is switched off (COGNITIVE_GRAPH_SYNC)")
    try:
        from .config import Settings
        from .retrieval import _require_listening

        settings = Settings.from_env(res.root / ".env")
        _require_listening(settings.neo4j_uri)
        from .code_parser import CodeParser
        from .graph_db import GraphDatabase

        with GraphDatabase(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password,
                           connection_timeout=5) as base:
            base.verify()
            base.init_schema()
            return run_sync(res, base.scoped(res.graph_id), CodeParser(), cfg, full)
    except Exception as exc:
        kind = type(exc).__name__
        reason = "Neo4j is not reachable" if kind == "GraphUnavailable" else "Neo4j connection failed" if kind == "ServiceUnavailable" else (
            "Neo4j rejected the credentials" if kind == "AuthError" else f"{kind}")
        status = "unavailable" if kind in ("GraphUnavailable", "ServiceUnavailable", "AuthError") else "error"
        try:
            record_status(res.root, status, reason)
        except OSError:
            pass
        return SyncResult(status, reason)


def run_cli(argv: list[str]) -> int:
    """`cognitive-graph sync [--project DIR] [--full] [--status]`: run or inspect a sync in the foreground."""
    p = argparse.ArgumentParser(prog="cognitive-graph sync", description="Synchronise the code graph now")
    p.add_argument("--project", default=".")
    p.add_argument("--full", action="store_true", help="re-index every file, not just changes")
    p.add_argument("--status", action="store_true", help="show the last sync state; change nothing")
    a = p.parse_args(argv)
    res = resolve_project(a.project)
    if not res.ok:
        print(f"Error: {res.message}", file=sys.stderr)
        return 1
    if a.status:
        st = load_state(res.root)
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st["finished"])) if st.get("finished") else "never"
        print(f"project {res.name} (graph scope {res.graph_id})\nlast sync: {st.get('status', 'never run')} at {when}"
              f" - {st.get('message', '')}\nindexed files: {len(st.get('files', {}))}, pending: {st.get('pending', 0)}"
              f"\nrunning now: {'yes' if SyncLock.is_fresh(res.root) else 'no'}")
        return 0
    r = _run_for(res.root, a.full)
    print(f"{r.status}: {r.message}")
    return 0 if r.status in ("ok", "busy") else 1


def main() -> int:
    p = argparse.ArgumentParser(description="cognitive-graph background sync (started by the hooks)")
    p.add_argument("--root", required=True)
    p.add_argument("--full", action="store_true")
    a = p.parse_args()
    try:
        _run_for(Path(a.root), a.full)
    except Exception:  # background job: never leave a traceback anywhere
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
