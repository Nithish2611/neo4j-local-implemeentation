"""Warm graph-query helper for the prompt hook.

Why it exists: importing the Neo4j driver costs about 0.8 s on an idle machine and 12 to 14 s on a
saturated one, and the hook is a fresh Python process per prompt. This module runs ONE long-lived local
process that imports the driver once and answers prompt lookups over 127.0.0.1.

Lifecycle
  * started on demand by the hook (detached; the hook never waits for it);
  * shared by concurrent Claude Code sessions (threads, one driver);
  * exits after IDLE_SECONDS without requests, when it is replaced by a newer helper, or on `stop`;
  * a start lock stops concurrent sessions from spawning duplicates; a failed start (Neo4j down) backs
    off for FAILED_BACKOFF seconds, so an unreachable database is not retried on every prompt.

Safety
  * binds 127.0.0.1 only and requires a random per-start token (compared in constant time);
  * the state file (port, token, pid) lives in the user's folder (COGNITIVE_GRAPH_HOME overrides it);
  * read-only: the only operation is the same scoped `find_relevant` the direct path uses;
  * every query is scoped to the caller's graph id, and a helper is only used when its Neo4j
    configuration fingerprint matches the caller's (otherwise the caller uses its own direct path).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

PROTOCOL = 2  # 2 adds the "task" operation; a helper on an older protocol is replaced
IDLE_SECONDS = 600
START_STALE_SECONDS = 120   # a start lock older than this belonged to a process that died while starting
FAILED_BACKOFF_SECONDS = 60
RESPAWN_HOLDOFF_SECONDS = 60  # after a successful start, don't start another while the first is warming up
WATCH_INTERVAL = 2.0
MAX_MESSAGE = 65536
SCOPE_RE = re.compile(r"^[A-Za-z0-9_~.-]{1,80}$")


PROTOCOL_ERRORS = {"bad request", "unauthorised", "bad scope", "unknown operation"}


def describe_error(exc: BaseException) -> tuple[str, str]:
    """(kind, detail) for diagnostics. Only the Neo4j error CODE is used: messages can echo query text, and
    nothing here logs prompts, source code or credentials. Matches by class name, so the hook never has to
    import the Neo4j driver to classify an error."""
    names = {c.__name__ for c in type(exc).__mro__}
    if "ClientError" in names:
        return "query", str(getattr(exc, "code", "") or "ClientError")
    if "AuthError" in names:
        return "auth", "credentials rejected"
    if names & {"ServiceUnavailable", "SessionExpired", "DatabaseUnavailable"}:
        return "connection", type(exc).__name__
    if "Neo4jError" in names:
        return "neo4j", str(getattr(exc, "code", "") or type(exc).__name__)
    return "error", type(exc).__name__


class HelperStarting(RuntimeError):
    """The helper was just started; this prompt goes without graph context."""


class HelperUnavailable(RuntimeError):
    """The helper could not reach Neo4j, so retries are paused."""


class ConfigMismatch(RuntimeError):
    """A helper with a different Neo4j configuration is running; the caller should query directly."""


def home() -> Path:
    base = os.environ.get("COGNITIVE_GRAPH_HOME")
    return Path(base) if base else Path.home() / ".cognitive-graph"


def fingerprint(uri: str, user: str, password: str) -> str:
    return hashlib.sha256("\x00".join((uri, user, password)).encode("utf-8")).hexdigest()


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # best effort; Windows relies on the user profile's own permissions
    except OSError:
        pass
    os.replace(tmp, path)


def _exchange(port: int, message: dict, timeout: float) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n") and len(data) < MAX_MESSAGE:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode("utf-8") or "{}")


# --- server (runs inside the helper process) ----------------------------------------------------------


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(socketserver.StreamRequestHandler):
    def _send(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))

    def handle(self) -> None:
        srv = self.server
        try:
            request = json.loads(self.rfile.readline(MAX_MESSAGE).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send({"ok": False, "error": "bad request"})
        if not isinstance(request, dict) or not secrets.compare_digest(str(request.get("token", "")), srv.token):
            return self._send({"ok": False, "error": "unauthorised"})
        srv.last_seen = time.time()
        op = request.get("op")
        if op == "ping":
            return self._send({"ok": True, "pid": os.getpid()})
        if op == "stop":
            self._send({"ok": True})
            threading.Thread(target=srv.shutdown, daemon=True).start()
            return
        if op == "task":
            scope = str(request.get("scope", ""))
            if not SCOPE_RE.match(scope):
                return self._send({"ok": False, "error": "bad scope"})
            try:
                limit = max(1, min(int(request.get("limit", 5)), 20))
                expand = max(0, min(int(request.get("expand", 3)), 10))
                functions = srv.base.scoped(scope).find_by_task(str(request.get("task", ""))[:4000], limit, expand)
            except Exception as exc:
                kind, detail = describe_error(exc)
                return self._send({"ok": False, "error": kind, "detail": detail})
            return self._send({"ok": True, "functions": functions})
        if op == "find":
            scope = str(request.get("scope", ""))
            if not SCOPE_RE.match(scope):
                return self._send({"ok": False, "error": "bad scope"})
            try:
                limit = max(1, min(int(request.get("limit", 5)), 20))
                functions = srv.base.scoped(scope).find_relevant(str(request.get("prompt", ""))[:4000], limit)
            except Exception as exc:  # Neo4j went away, bad query...: report the kind and code, never the message
                kind, detail = describe_error(exc)
                return self._send({"ok": False, "error": kind, "detail": detail})
            return self._send({"ok": True, "functions": functions})
        self._send({"ok": False, "error": "unknown operation"})


def run_server(base, fp: str, state_dir: Path, idle_seconds: float = IDLE_SECONDS,
               watch_interval: float = WATCH_INTERVAL) -> None:
    """Serve until idle, replaced, or stopped. `base` is a GraphDatabase-like object (tests pass a fake)."""
    token = secrets.token_hex(16)
    server = _Server(("127.0.0.1", 0), _Handler)
    server.token, server.base, server.last_seen = token, base, time.time()
    state_file = state_dir / "helper.json"
    _write_atomic(state_file, {"pid": os.getpid(), "port": server.server_address[1], "token": token,
                               "fingerprint": fp, "started": time.time(), "protocol": PROTOCOL})
    (state_dir / "helper.lock").unlink(missing_ok=True)

    def watch() -> None:
        while True:
            time.sleep(watch_interval)
            current = _read(state_file)
            if not current or current.get("token") != token or time.time() - server.last_seen > idle_seconds:
                server.shutdown()  # idle, replaced by a newer helper, or state removed
                return

    threading.Thread(target=watch, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        current = _read(state_file)
        if current and current.get("token") == token:
            state_file.unlink(missing_ok=True)


def serve() -> int:
    """Entry point of the helper process. Configuration comes from NEO4J_* in its environment."""
    uri, user, password = os.environ["NEO4J_URI"], os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]
    fp, state_dir = fingerprint(uri, user, password), home()
    from .graph_db import GraphDatabase  # the slow import happens here, once, in the background

    try:
        base = GraphDatabase(uri, user, password, connection_timeout=5)
        base.verify()
    except Exception as exc:
        _write_atomic(state_dir / "helper-failed.json", {"fingerprint": fp, "at": time.time(),
                                                         "reason": f"Neo4j unavailable ({type(exc).__name__})"})
        (state_dir / "helper.lock").unlink(missing_ok=True)
        return 1
    try:
        run_server(base, fp, state_dir)
    finally:
        base.close()
    return 0


# --- client (runs inside the hook) ----------------------------------------------------------------------


def _alive(state: dict, timeout: float = 1.5) -> bool:
    try:
        return bool(_exchange(int(state["port"]), {"token": state["token"], "op": "ping"}, timeout).get("ok"))
    except (OSError, ValueError, KeyError):
        return False


def _spawn(uri: str, user: str, password: str) -> None:
    env = {**os.environ, "NEO4J_URI": uri, "NEO4J_USER": user, "NEO4J_PASSWORD": password}
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "cognitive_graph.graph_helper", "--serve"], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, **kwargs)


def _start_helper(uri: str, user: str, password: str, fp: str, replacing: bool = False) -> None:
    """Start a helper unless one is already starting, just started, or Neo4j recently refused us.
    `replacing` skips the warm-up holdoff: the running helper speaks an older protocol."""
    d = home()
    d.mkdir(parents=True, exist_ok=True)
    failed = _read(d / "helper-failed.json")
    if failed and failed.get("fingerprint") == fp and time.time() - failed.get("at", 0) < FAILED_BACKOFF_SECONDS:
        raise HelperUnavailable(failed.get("reason", "backing off after a failed start"))
    started = _read(d / "helper.json")
    if not replacing and started and time.time() - started.get("started", 0) < RESPAWN_HOLDOFF_SECONDS:
        raise HelperStarting("graph helper is warming up")
    lock = d / "helper.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > START_STALE_SECONDS:
            lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
    except FileExistsError:
        raise HelperStarting("graph helper is starting") from None
    try:
        _spawn(uri, user, password)
    except OSError:
        lock.unlink(missing_ok=True)
        raise
    raise HelperStarting("graph helper started in the background")


def _ask(scope: str, message: dict, uri: str, user: str, password: str, timeout: float) -> list[dict]:
    if not SCOPE_RE.match(scope):
        raise ValueError("bad graph scope")
    fp = fingerprint(uri, user, password)
    state = _read(home() / "helper.json")
    current = bool(state) and state.get("protocol") == PROTOCOL
    if state and current and _alive(state):
        if state.get("fingerprint") != fp:
            raise ConfigMismatch("a helper for another Neo4j configuration is running")
        reply = _exchange(int(state["port"]), {"token": state["token"], "scope": scope, **message}, timeout)
        if reply.get("ok"):
            return list(reply.get("functions", []))
        kind = str(reply.get("error", "unknown"))
        if kind in PROTOCOL_ERRORS:
            kind = "protocol"
        detail = reply.get("detail") or reply.get("error", "unknown")
        raise HelperUnavailable(f"helper {detail}" if kind == "error" else f"helper {kind} error: {detail}")
    if state and not current and _alive(state) and state.get("fingerprint") != fp:
        raise ConfigMismatch("a helper for another Neo4j configuration is running")
    # Not running, not answering, or an older protocol: start a helper (it replaces the old one).
    _start_helper(uri, user, password, fp, replacing=bool(state) and not current)
    raise HelperStarting("graph helper is starting")  # _start_helper always raises


def query(scope: str, prompt: str, limit: int, uri: str, user: str, password: str,
          timeout: float = 2.0) -> list[dict]:
    """Functions named in `prompt` (legacy operation). See query_task for plain-language tasks."""
    return _ask(scope, {"op": "find", "prompt": prompt[:4000], "limit": limit}, uri, user, password, timeout)


def query_task(scope: str, task: str, limit: int, expand: int, uri: str, user: str, password: str,
               timeout: float = 2.0) -> list[dict]:
    """Ranked, labelled functions for a plain-language task, with callers and callees for the top `expand`.
    Raises HelperStarting / HelperUnavailable / ConfigMismatch: the caller answers without graph context
    or queries directly."""
    return _ask(scope, {"op": "task", "task": task[:4000], "limit": limit, "expand": expand},
                uri, user, password, timeout)


def stop() -> str:
    state = _read(home() / "helper.json")
    if not state:
        return "no graph helper is running"
    try:
        _exchange(int(state["port"]), {"token": state["token"], "op": "stop"}, 2.0)
        return f"stopped graph helper (pid {state.get('pid')})"
    except (OSError, ValueError, KeyError):
        (home() / "helper.json").unlink(missing_ok=True)
        return "removed a stale helper record (it was not answering)"


def status() -> str:
    state = _read(home() / "helper.json")
    if not state:
        return "not running"
    return f"{'running' if _alive(state) else 'not answering'} (pid {state.get('pid')}, port {state.get('port')})"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cognitive_graph.graph_helper")
    p.add_argument("--serve", action="store_true", help="run the helper in this process (started by the hook)")
    args = p.parse_args(argv)
    if args.serve:
        return serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
