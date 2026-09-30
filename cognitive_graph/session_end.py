"""Claude Code `SessionEnd` hook: save a concise PROPOSED handoff for the active project.

Registered as `cognitive-graph-session-end` (see `cognitive-graph hook install`). It reads the
hook JSON from stdin (`session_id`, `transcript_path`, `cwd`, `reason`, `hook_event_name`),
resolves the project from `cwd` exactly like the other hooks (initialising a Git project on first
use, never guessing, never falling back to another project), and writes one file per session under
`.cognitive-graph/memory/handoff/`. It then nudges the background graph sync.

What is saved (everything is capped, redacted and labelled unverified):
  * facts read from tools: time, branch, commit, files Claude edited (tool-call paths), git status;
  * short VERBATIM quotes from the transcript: the first prompt (the task), an excerpt of Claude's
    final message (what was done), sentences that state a decision or rationale, sentences that
    mention unfinished work, and Claude's todo list when it kept one.
The full transcript, file contents, tool output and environment values are never stored, and no
text is paraphrased or invented. `COGNITIVE_GRAPH_HANDOFF_QUOTES=off` keeps only the tool facts.

The hook never blocks and always exits 0. Its stdout is ignored by Claude Code; stderr is shown.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .memory import (Item, MemoryStore, MemoryStoreError, ProjectResolution, _tokens, auto_init_project,
                     resolve_project)

MAX_TRANSCRIPT_BYTES = 40 * 1024 * 1024
MAX_LISTED = 15          # paths listed in the body per section
MAX_EVIDENCE_FILES = 8   # paths recorded as evidence
GOAL_CHARS, DONE_CHARS, SENTENCE_CHARS, MAX_SENTENCES = 240, 700, 300, 5
EDIT_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
HIDDEN_PREFIXES = (".cognitive-graph/", ".claude/")
SENSITIVE_RE = re.compile(
    r"(^|/)(\.env(\..*)?|\.netrc|\.npmrc|\.pypirc|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?"
    r"|[^/]*\.(pem|key|p12|pfx|jks|keystore|kdbx)|[^/]*(secret|credential|passw(or)?d)[^/]*)$", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# --- redaction ------------------------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"\b(?=[A-Za-z0-9+/_=-]*[A-Za-z])(?=[A-Za-z0-9+/_=-]*\d)[A-Za-z0-9+/_=-]{32,}\b"),
]
_KEY_VALUE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|credential|auth)[A-Za-z0-9_.-]*)"
    r"(\s*[:=]\s*)[\"']?[^\s\"',;]+")


def redact(text: str) -> str:
    """Best-effort removal of credentials from text that is about to be stored."""
    for pat in _SECRET_PATTERNS:  # specific token shapes first, so a scheme word is never mistaken for the value
        text = pat.sub("[redacted]", text)
    return _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)


# --- quote extraction (verbatim sentences only; nothing is paraphrased) ----------------------------------

_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.S)
_LEAD_RE = re.compile(r"^\s*(?:[-*+>]+|\d+[.)]|#{1,6})\s+")
DECISION_RE = re.compile(
    r"\b(decided|decision|chose|chosen|opted|instead of|rather than|trade-?offs?|rationale|went with|going with|"
    r"settled on|the reason (?:is|was)|design choice)\b", re.I)
OPEN_RE = re.compile(
    r"\b(todo|not yet|still (?:need|needs|to do|open|missing|unverified)|remaining|next steps?|follow-?ups?|"
    r"left to do|unverified|not (?:been )?(?:tested|verified|implemented|run)|hasn't been|haven't|"
    r"out of scope|limitations?|blocked)\b", re.I)


# Talk about earlier sessions, handoffs or this tool is not a business decision: it is a session reading
# (or Claude describing) stored memory, so it must never be recorded as new context.
META_RE = re.compile(r"hand-?off|cognitive-graph|unconfirmed|memory item|\b(?:last|previous|earlier|prior) sessions?\b", re.I)


def _flat(text: str) -> str:
    return " ".join(redact(_FENCE_RE.sub(" ", text)).split())


def _sentences(text: str) -> list[str]:
    found = []
    for line in _FENCE_RE.sub("\n", text).splitlines():
        line = _LEAD_RE.sub("", line).strip()
        if not line or line.startswith("|"):  # markdown tables are not prose
            continue
        for sent in re.split(r"(?<=[.!?])\s+", line):
            sent = " ".join(redact(sent).replace("**", "").replace("`", "").split())
            if 20 <= len(sent) <= 2 * SENTENCE_CHARS:
                found.append(sent[:SENTENCE_CHARS])
    return found


def _pick(sentences: list[str], pattern: re.Pattern, last: bool = False) -> list[str]:
    # A question states nothing ("What was left to do?" is not an unfinished item), and talk about the
    # memory system is not business context.
    hits = list(dict.fromkeys(s for s in sentences if pattern.search(s) and not META_RE.search(s)
                              and not s.rstrip().endswith("?")))
    return hits[-MAX_SENTENCES:] if last else hits[:MAX_SENTENCES]


def _excerpt(text: str, limit: int) -> str:
    flat = _flat(text)
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > limit // 2 else cut.rstrip()) + " ..."


@dataclass
class Digest:
    edited: list[str] = field(default_factory=list)
    goal: str = ""
    done: str = ""
    decisions: list[tuple[str, str]] = field(default_factory=list)   # ("you" | "Claude", verbatim sentence)
    unfinished: list[tuple[str, str]] = field(default_factory=list)
    todos_open: list[str] = field(default_factory=list)
    todos_done: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Result:
    status: str                 # "saved" | "skipped" | "error"
    message: str
    item: Item | None = None
    title: str = ""
    body: str = ""
    evidence: list[str] = field(default_factory=list)


# --- gathering (each part is best effort and never raises) ---------------------------------------------


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def git_snapshot(root: Path) -> dict:
    branch = (_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    commit = (_git(root, "rev-parse", "--short", "HEAD") or "").strip()
    changed: list[tuple[str, str]] = []
    for line in (_git(root, "status", "--porcelain=v1", "--untracked-files=all") or "").splitlines()[:200]:
        if len(line) < 4:
            continue
        code, path = line[:2].strip() or "?", line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed.append(("new" if code == "??" else code, path.strip('"')))
    return {"branch": branch, "commit": commit, "changed": changed}


def _clean_path(raw: str) -> str:
    return _CONTROL_RE.sub("?", raw.replace("\\", "/"))[:200]


def _relative(raw, root: Path) -> str | None:
    """`raw` as a posix path relative to the project root, or None if it is outside it."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        p = Path(raw)
        p = (p if p.is_absolute() else root / p).resolve()
        return p.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def _user_prompt(entry: dict) -> str | None:
    """The text of a prompt the user typed (not a tool result, hook context or slash-command wrapper)."""
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return None
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, str):
        return None
    text = content.strip()
    return None if not text or text.startswith(("<", "Caveat:")) else text


def _assistant_texts(entry: dict) -> list[str]:
    msg = entry.get("message") if entry.get("type") == "assistant" and not entry.get("isSidechain") else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    return [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]


def read_transcript(transcript_path, root: Path, quotes: bool = True) -> Digest:
    """One pass over the session transcript (JSONL). The format is not documented by Claude Code, so
    every access is defensive and anything unexpected is skipped rather than guessed at."""
    d = Digest()
    try:
        p = Path(transcript_path)
        if p.suffix != ".jsonl" or not p.is_file():
            d.note = "transcript not available"
            return d
        if p.stat().st_size > MAX_TRANSCRIPT_BYTES:
            d.note = "transcript too large to scan"
            return d
        assistant_texts: list[str] = []
        user_texts: list[str] = []
        todo_input = None
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                is_tool = '"tool_use"' in line
                if not (is_tool or (quotes and ('"user"' in line or '"assistant"' in line))):
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if quotes:
                    if prompt := _user_prompt(entry):
                        d.goal = d.goal or _excerpt(prompt, GOAL_CHARS)
                        user_texts.append(prompt)
                    assistant_texts += _assistant_texts(entry)
                msg = entry.get("message") if entry.get("type") == "assistant" else None
                content = msg.get("content") if isinstance(msg, dict) else None
                for block in content if is_tool and isinstance(content, list) else []:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    args = block.get("input")
                    if block.get("name") in EDIT_TOOLS:
                        rel = _relative(args.get(EDIT_TOOLS[block["name"]]) if isinstance(args, dict) else None, root)
                        if rel and rel not in d.edited:
                            d.edited.append(rel)
                    elif block.get("name") == "TodoWrite" and isinstance(args, dict):
                        todo_input = args.get("todos")
        if quotes and (assistant_texts or user_texts):
            if assistant_texts:
                d.done = _excerpt(assistant_texts[-1], DONE_CHARS)
            # Business decisions are often stated by the user and only acknowledged by Claude: read both.
            yours = _pick([s for t in user_texts[-20:] for s in _sentences(t)], DECISION_RE, last=True)[-3:]
            claudes = [s for s in _pick([s for t in assistant_texts[-20:] for s in _sentences(t)], DECISION_RE, last=True)
                       if s not in yours]
            d.decisions = ([("you", s) for s in yours] + [("Claude", s) for s in claudes])[:MAX_SENTENCES]
            open_yours = _pick(_sentences(user_texts[-1]) if user_texts else [], OPEN_RE)
            open_claude = [s for s in _pick(_sentences(assistant_texts[-1]) if assistant_texts else [], OPEN_RE)
                           if s not in open_yours]
            d.unfinished = ([("you", s) for s in open_yours] + [("Claude", s) for s in open_claude])[:MAX_SENTENCES]
        if quotes and isinstance(todo_input, list):
            for todo in todo_input[:40]:
                if isinstance(todo, dict) and isinstance(todo.get("content"), str):
                    text = _excerpt(todo["content"], 160)
                    (d.todos_done if todo.get("status") == "completed" else d.todos_open).append(text)
            d.todos_open, d.todos_done = d.todos_open[:8], d.todos_done[:8]
    except (OSError, TypeError, ValueError) as exc:
        d.note = f"transcript unreadable ({type(exc).__name__})"
    return d


def transcript_edited_files(transcript_path, root: Path) -> tuple[list[str], str]:
    """Paths Claude edited in this session (kept for callers that only need the file list)."""
    d = read_transcript(transcript_path, root, quotes=False)
    return d.edited, d.note


def _visible(paths) -> tuple[list[str], set[str]]:
    """Drop this tool's own files and sensitive-looking paths; report which sensitive ones were dropped."""
    keep, hidden = [], set()
    for raw in paths:
        p = _clean_path(raw)
        if p.startswith(HIDDEN_PREFIXES):
            continue
        if SENSITIVE_RE.search(p):
            hidden.add(p)
        else:
            keep.append(p)
    return keep, hidden


# --- building -----------------------------------------------------------------------------------------------


def _more(items: list[str], limit: int) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def known_sentences(root: Path) -> list[frozenset[str]]:
    """Meaningful-word sets of every sentence already stored in this project's handoffs. A session that
    merely reads or discusses an earlier handoff repeats it, often paraphrased ("Left to do" for "Still to
    do"); such repeats must not be recorded again as new context."""
    try:
        return [t for item in MemoryStore(root).items("handoff") for sent in _sentences(item.body)
                if (t := _tokens(sent))]
    except (OSError, MemoryStoreError):
        return []


REPEAT_OVERLAP = 0.8  # share of a sentence's meaningful words that an earlier handoff already contains


def is_repeat(sentence: str, known) -> bool:
    words = _tokens(sentence)
    return not words or any(len(words & k) / len(words) >= REPEAT_OVERLAP for k in known)


def build_handoff(res: ProjectResolution, payload: dict, now: datetime, quotes: bool = True,
                  seen=()) -> Result:
    """Assemble the handoff text, or a 'skipped' Result explaining why there is nothing to save.

    A handoff is saved when the session has NEW context: edited files (tool-call evidence), an open todo
    item (structured evidence), or a decision / rationale / unfinished item that the user or Claude stated
    (quoted verbatim, heuristic selection) and that no earlier handoff already contains. A session that only
    reads or discusses earlier handoffs records nothing: repeats are dropped by word overlap (`seen`),
    sentences about handoffs or earlier sessions are ignored, and a no-edit session whose opening prompt asks
    about earlier sessions is skipped."""
    digest = read_transcript(payload.get("transcript_path"), res.root, quotes)
    digest.decisions = [(w, s) for w, s in digest.decisions if not is_repeat(s, seen)]
    digest.unfinished = [(w, s) for w, s in digest.unfinished if not is_repeat(s, seen)]
    edited, hidden_edited = _visible(digest.edited)
    has_notes = quotes and bool(digest.todos_open or digest.decisions or digest.unfinished)
    reviewing_memory = bool(digest.goal and META_RE.search(digest.goal))
    if not edited and not hidden_edited and (not has_notes or reviewing_memory):
        why = ("this session only reviewed earlier sessions or handoffs" if has_notes and reviewing_memory else
               "no file edits, open todo items, or new decision or unfinished item in this session")
        return Result("skipped", f"nothing new to record: {why}" + (f" ({digest.note})" if digest.note else ""))

    git = git_snapshot(res.root)
    changed_vis, hidden_changed = _visible(p for _, p in git["changed"])
    status_of = {_clean_path(p): c for c, p in git["changed"]}
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    reason = str(payload.get("reason") or "unknown")[:40]
    where = f"branch {git['branch'] or 'unknown'}" + (f" at commit {git['commit']}" if git["commit"] else "")
    summary = f"{stamp}, {where}; session ended ({reason})."
    if digest.goal:
        summary += f' Task: "{digest.goal[:120]}".'
    if edited:
        summary += f" Edited {len(edited)} file(s): {_more(edited, 4)}."
    if digest.decisions:
        summary += f" Decision: {digest.decisions[0][1][:140]}"
    open_items = digest.todos_open + [s for _, s in digest.unfinished]
    if open_items:
        summary += f" Open: {open_items[0][:120]}"
    if changed_vis:
        summary += f" {len(changed_vis)} uncommitted path(s)."

    body = [summary, ""]
    if digest.goal:
        body += ["**Task** (first prompt of the session, truncated; quoted, not verified)", "> " + digest.goal, ""]
    if digest.done:
        body += ["**What was done** (excerpt of Claude's final message; Claude's own claim, not verified)",
                 "> " + digest.done, ""]
    if digest.todos_done or digest.todos_open:
        body += ["**Claude's todo list at the end** (as written by Claude)",
                 *(f"- [done] {t}" for t in digest.todos_done), *(f"- [open] {t}" for t in digest.todos_open), ""]
    if digest.decisions:
        body += ["**Decisions and rationale mentioned** (verbatim sentences that state a choice or reason; [you] = written "
                 "by you, [Claude] = written by Claude; heuristic selection, not confirmed)",
                 *(f"- [{who}] {s}" for who, s in digest.decisions), ""]
    if digest.unfinished:
        body += ["**Unfinished or open** (verbatim sentences from your last message and Claude's final message; "
                 "heuristic selection, not confirmed)", *(f"- [{who}] {s}" for who, s in digest.unfinished), ""]
    if edited:
        body += ["**Files edited this session** (from Edit/Write tool calls; content not stored)",
                 *(f"- {p}" for p in edited[:MAX_LISTED])]
        if len(edited) > MAX_LISTED:
            body.append(f"- ... and {len(edited) - MAX_LISTED} more")
        body.append("")
    if changed_vis:
        body += ["**Uncommitted at session end** (git status; may include changes from before this session)",
                 *(f"- {status_of.get(p, '?')} {p}" for p in changed_vis[:MAX_LISTED])]
        if len(changed_vis) > MAX_LISTED:
            body.append(f"- ... and {len(changed_vis) - MAX_LISTED} more")
        body.append("")
    hidden = hidden_edited | hidden_changed
    if hidden:
        body += [f"{len(hidden)} sensitive-looking path(s) omitted.", ""]
    body.append("_Captured automatically by the session-end hook. Quoted text is verbatim, truncated and "
                "credential-redacted; nothing was summarised or inferred, and the transcript itself is not stored. "
                "Unreviewed: keep it with `cognitive-graph memory confirm <id>` or remove it with `memory forget <id>`._")

    session = str(payload.get("session_id") or "")
    evidence = [f"session:{session}"] + ([f"commit:{git['commit']}"] if git["commit"] else []) + edited[:MAX_EVIDENCE_FILES]
    title = f"Session handoff {now.strftime('%Y-%m-%d %H:%M')} ({git['branch'] or 'no branch'})"
    return Result("saved", "ready", None, title, "\n".join(body), evidence)


def _enabled(env, key: str = "COGNITIVE_GRAPH_HANDOFF", default: str = "on") -> bool:
    return str(env.get(key, default)).strip().lower() not in ("0", "off", "false", "no", "")


def handle(payload: dict, environ=None, now: datetime | None = None, write: bool = True, sync=None) -> Result:
    """Pure core: hook input dict -> Result. Writes the handoff unless `write` is False (preview,
    which also never initialises a project). `sync` defaults to the real background-sync trigger."""
    now = now or datetime.now(timezone.utc)
    env = dict(os.environ if environ is None else environ)
    if not _enabled(env):
        return Result("skipped", "handoff capture is switched off (COGNITIVE_GRAPH_HANDOFF)")
    session = str(payload.get("session_id") or "")
    sid = re.sub(r"[^A-Za-z0-9]", "", session)[:8]
    if not sid:
        return Result("skipped", "no usable session_id in the hook input")

    cwd = payload.get("cwd") or os.getcwd()
    res, created = auto_init_project(cwd, env) if write else (resolve_project(cwd), False)
    if not res.ok:
        return Result("skipped", res.message)  # no project: nothing is written anywhere else
    if environ is None:
        try:
            from .hook import _env_for

            env = _env_for(res.root)
            if not _enabled(env):
                return Result("skipped", "handoff capture is switched off (COGNITIVE_GRAPH_HANDOFF)")
        except Exception:
            pass

    built = build_handoff(res, payload, now, _enabled(env, "COGNITIVE_GRAPH_HANDOFF_QUOTES"), known_sentences(res.root))
    try:
        if built.status == "saved" and write:
            built = _save(res, built, sid, session, now)
    finally:
        if write:
            _nudge_sync(res, env, sync)
    if created and built.status != "error":
        built.message += f"; initialised project {res.name} ({res.project_id})"
    return built


def _save(res: ProjectResolution, built: Result, sid: str, session: str, now: datetime) -> Result:
    try:
        store = MemoryStore(res.root)
        base = f"H-{now.strftime('%Y%m%dT%H%M%SZ')}-{sid}"
        for n in range(1, 50):  # same session ending twice in one second gets -2, -3, ...
            try:
                built.item = store.add_exclusive(
                    "handoff", base if n == 1 else f"{base}-{n}", built.title, built.body,
                    tags=["auto", "session-end"], evidence=built.evidence, source="hook",
                    trust="proposed", session=session)
                break
            except FileExistsError:
                continue
        else:
            return Result("error", "could not allocate a unique handoff id")
    except (OSError, MemoryStoreError) as exc:
        return Result("error", f"could not save the handoff ({type(exc).__name__})")
    built.message = f"saved proposed handoff {built.item.id} (unconfirmed)"
    return built


def _nudge_sync(res: ProjectResolution, env, sync) -> None:
    """Session edits should reach the graph before the next session: start the background sync."""
    try:
        from .graph_sync import SyncConfig, maybe_spawn_sync

        (sync or maybe_spawn_sync)(res, SyncConfig.from_mapping(env), force=True)
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
        result = handle(payload if isinstance(payload, dict) else {})
        verbose = str(os.environ.get("COGNITIVE_GRAPH_HANDOFF_VERBOSE", "")).strip().lower() in ("1", "on", "true", "yes")
        if result.status != "skipped" or verbose:
            print(f"cognitive-graph: {result.message}", file=sys.stderr)
    except Exception as exc:  # never disturb Claude Code's shutdown
        print(f"cognitive-graph: session-end hook error ({type(exc).__name__}); no handoff saved", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
