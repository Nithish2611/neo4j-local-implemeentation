"""Durable, Git-friendly project memory.

Each project keeps its memory as plain Markdown files next to its source:

    <project>/.cognitive-graph/
        project.json                 # stable project id + name
        memory/<type>/<ID>-<slug>.md # one item per file (front matter + body)

The files are the source of truth. Nothing here needs Neo4j or an LLM.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = ".cognitive-graph"
PROJECT_FILE = "project.json"
SCHEMA_VERSION = 1

# type -> (id prefix, default status, allowed statuses)
TYPES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "fact": ("F", "active", ("active", "outdated")),
    "decision": ("D", "active", ("active", "superseded")),
    "task": ("T", "open", ("open", "in_progress", "done", "dropped")),
    "handoff": ("H", "active", ("active",)),
}
SOURCES = ("user", "claude", "code", "hook")  # who/what produced the item ("hook" = captured automatically)
TRUST = ("confirmed", "proposed")  # proposed = not yet accepted by the user
OPEN_STATUSES = ("open", "in_progress")

_STOPWORDS = frozenset(
    "the and for with that this from have has are was were will would should could into about "
    "what when how why not you your our can all any but use using get set add new "
    "are does did done has had its who whom which where there their them they then than these those "
    "through over under out off also just like make made need want please may might must shall more "
    "most some such only own same too very here should would could been being other about between".split()
)


class MemoryStoreError(Exception):
    """Raised for user-facing memory problems (bad id, no project, ...)."""


def link_no_overwrite(tmp: Path, dest: Path) -> None:
    """Atomically give `tmp`'s content the name `dest`, failing with FileExistsError if `dest`
    exists. A hard link is atomic and refuses to overwrite; on filesystems without hard links the
    name is claimed exclusively first, then the content is swapped in."""
    try:
        os.link(tmp, dest)
    except FileExistsError:
        raise
    except OSError:
        os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        os.replace(tmp, dest)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "item"


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]{3,}", text.lower()) if t not in _STOPWORDS}


def _git_head(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


@dataclass(frozen=True)
class ProjectResolution:
    """Which project a working directory belongs to.

    status: "ok" | "uninitialized" (no project.json) | "invalid" (unreadable or bad id).
    Never falls back to another project: unresolved means no project."""
    status: str
    cwd: Path
    root: Path | None = None
    project_id: str = ""
    name: str = ""
    message: str = ""
    graph_id: str = ""  # scope used for Neo4j data: project_id, plus a checkout suffix in linked worktrees

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def git_root(start: Path) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 and out.stdout.strip() else None


def _graph_scope(root: Path, project_id: str) -> str:
    """The Neo4j scope for this checkout. Normally the project id. In a *linked* Git worktree the
    id may be shared with the main checkout (if project.json was committed) while the files differ,
    so the worktree gets its own graph scope and the two never mix."""
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-dir", "--git-common-dir"],
                             capture_output=True, text=True, timeout=3)
        lines = out.stdout.split("\n") if out.returncode == 0 else []
        if len(lines) >= 2 and (root / lines[0].strip()).resolve() != (root / lines[1].strip()).resolve():
            return f"{project_id}~{hashlib.sha1(os.path.normcase(str(root)).encode()).hexdigest()[:6]}"
    except (OSError, subprocess.SubprocessError):
        pass
    return project_id


def resolve_project(start: Path | str = ".") -> ProjectResolution:
    """Find the project for `start`: the nearest `.cognitive-graph/project.json` at or above it,
    but never above the enclosing Git root (a repo without its own project.json is NOT
    attributed to some parent folder's project). The stable id comes from project.json only."""
    start = Path(start).resolve()
    boundary = git_root(start if start.is_dir() else start.parent)
    for d in (start, *start.parents):
        pj = d / MEMORY_DIR / PROJECT_FILE
        if pj.is_file():
            try:
                meta = json.loads(pj.read_text(encoding="utf-8"))
                pid = meta["id"]
                if not isinstance(pid, str) or not _PROJECT_ID_RE.match(pid):
                    raise ValueError("id must be 6-64 chars of letters, digits, '-' or '_'")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return ProjectResolution("invalid", start, d, message=f"{pj} is unusable: {exc}")
            return ProjectResolution("ok", start, d, pid, str(meta.get("name") or d.name),
                                     graph_id=_graph_scope(d, pid))
        if boundary is not None and d == boundary:
            break
    where = f"Git root {boundary}" if boundary else str(start)
    return ProjectResolution("uninitialized", start, boundary, message=(
        f"No .cognitive-graph/project.json found in {where}. Run: cognitive-graph memory init"))


def _exclude_locally(root: Path) -> None:
    """Keep `.cognitive-graph/` out of `git status` and out of `git add .`, without touching the
    user's tracked files: the entry goes in the repository's private .git/info/exclude."""
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
                             capture_output=True, text=True, timeout=3)
        if out.returncode != 0 or not out.stdout.strip():
            return
        path = root / out.stdout.strip()
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if any(line.strip() in (".cognitive-graph", ".cognitive-graph/") for line in text.splitlines()):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(("" if text.endswith("\n") or not text else "\n") + ".cognitive-graph/\n")
    except OSError:
        pass


def auto_init_project(start: Path | str = ".", environ=None) -> tuple[ProjectResolution, bool]:
    """Resolve the project and, if this is a Git working tree with no metadata yet, initialise it.

    Identity rule: one project per Git working-tree root (`git rev-parse --show-toplevel`), with a
    random stable id in <root>/.cognitive-graph/project.json. It is never derived from prompts,
    folder names or remotes, and a subfolder resolves to its Git root. Folders that are not Git
    working trees, the home folder and the filesystem root are never auto-initialised. An unreadable
    project.json is reported and left alone. Returns (resolution, created)."""
    env = os.environ if environ is None else environ
    res = resolve_project(start)
    if res.status != "uninitialized" or res.root is None:
        return res, False
    if str(env.get("COGNITIVE_GRAPH_AUTO_INIT", "on")).strip().lower() in ("0", "off", "false", "no"):
        return res, False
    root = res.root.resolve()
    if root.parent == root or root == Path.home().resolve():
        return res, False
    created = MemoryStore.init(root).created
    _exclude_locally(root)
    return resolve_project(start), created


def ensure_project(start: Path | str = ".") -> ProjectResolution:
    """Resolve the project, creating its identity at the Git root (or `start`) if it has none.
    An existing but invalid project.json is reported, never overwritten."""
    start = Path(start)
    res = resolve_project(start)
    if res.status == "uninitialized":
        if not start.exists():
            return ProjectResolution("uninitialized", res.cwd, message=f"{start} does not exist")
        base = start.resolve()
        MemoryStore.init(res.root or (base if base.is_dir() else base.parent))
        res = resolve_project(start)
    return res


@dataclass
class Item:
    id: str
    type: str
    title: str
    body: str = ""
    status: str = ""
    source: str = "user"
    trust: str = "confirmed"
    tags: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    commit: str = ""
    created: str = ""
    updated: str = ""
    project: str = ""
    session: str = ""  # Claude Code session id, for automatically captured handoffs
    path: Path | None = None

    def to_markdown(self) -> str:
        meta = ["id", "project", "type", "title", "status", "source", "trust",
                "tags", "evidence", "commit", "session", "created", "updated"]
        lines = ["---"] + [f"{k}: {json.dumps(getattr(self, k), ensure_ascii=False)}" for k in meta] + ["---"]
        return "\n".join(lines) + f"\n\n{self.body.strip()}\n"

    @classmethod
    def from_markdown(cls, text: str, path: Path | None = None) -> "Item":
        text = text.replace("\r\n", "\n")
        if not text.startswith("---\n") or "\n---" not in text[4:]:
            raise MemoryStoreError(f"{path}: missing front matter")
        head, _, body = text[4:].partition("\n---")
        meta: dict = {}
        for line in head.splitlines():
            key, sep, raw = line.partition(":")
            if not sep:
                continue
            raw = raw.strip()
            try:
                meta[key.strip()] = json.loads(raw)
            except json.JSONDecodeError:
                meta[key.strip()] = raw  # hand-edited plain value
        for k in ("tags", "evidence"):
            v = meta.get(k, [])
            meta[k] = [v] if isinstance(v, str) and v else (v if isinstance(v, list) else [])
        known = {f for f in cls.__dataclass_fields__ if f not in ("body", "path")}
        item = cls(**{k: str(v) if k not in ("tags", "evidence") else v
                      for k, v in meta.items() if k in known}, body=body.lstrip("\n").strip(), path=path)
        if item.type not in TYPES or not item.id:
            raise MemoryStoreError(f"{path}: invalid or missing id/type")
        return item

    @property
    def is_open_task(self) -> bool:
        return self.type == "task" and self.status in OPEN_STATUSES


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.dir = self.root / MEMORY_DIR
        self.warnings: list[str] = []
        pj = self.dir / PROJECT_FILE
        if not pj.is_file():
            raise MemoryStoreError(f"No cognitive-graph memory in {self.root}. Run: cognitive-graph memory init")
        self.meta = json.loads(pj.read_text(encoding="utf-8"))
        self.project_id: str = self.meta["id"]
        self.name: str = self.meta.get("name", self.root.name)

    @property
    def graph_id(self) -> str:
        return _graph_scope(self.root, self.project_id)

    # --- project ------------------------------------------------------------

    @staticmethod
    def find_root(start: Path | str = ".") -> Path:
        """Project folder for `start` (see resolve_project: bounded by the Git root)."""
        res = resolve_project(start)
        if not res.ok:
            raise MemoryStoreError(res.message)
        return res.root

    @classmethod
    def init(cls, root: Path | str, name: str | None = None) -> "MemoryStore":
        root = Path(root).resolve()
        pj = root / MEMORY_DIR / PROJECT_FILE
        created = False
        if not pj.exists():  # never overwrite an existing project identity
            pj.parent.mkdir(parents=True, exist_ok=True)
            meta = {"schema": SCHEMA_VERSION, "id": uuid.uuid4().hex[:12],
                    "name": name or root.name, "created": _now()}
            tmp = pj.with_name(f".{PROJECT_FILE}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
            tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
            try:
                link_no_overwrite(tmp, pj)  # two sessions starting at once: the first identity wins
                created = True
            except FileExistsError:
                pass
            finally:
                tmp.unlink(missing_ok=True)
        store = cls(root)
        store.created = created  # True only for the caller whose identity was actually written
        return store

    @classmethod
    def open(cls, start: Path | str = ".") -> "MemoryStore":
        return cls(cls.find_root(start))

    # --- reading ------------------------------------------------------------

    def items(self, type: str | None = None) -> list[Item]:
        found: list[Item] = []
        self.warnings = []
        for f in sorted((self.dir / "memory").glob("*/*.md")):
            try:
                item = Item.from_markdown(f.read_text(encoding="utf-8"), f)
            except MemoryStoreError as exc:
                self.warnings.append(str(exc))
                continue
            if item.project != self.project_id:
                self.warnings.append(f"{f.name}: belongs to another project ({item.project or '?'}), ignored")
                continue
            if type is None or item.type == type:
                found.append(item)
        return found

    def get(self, item_id: str) -> Item:
        for item in self.items():
            if item.id.lower() == item_id.lower():
                return item
        raise MemoryStoreError(f"No memory item with id {item_id}")

    # --- writing ------------------------------------------------------------

    def add(self, type: str, title: str, body: str = "", *, tags=(), evidence=(),
            source: str = "user", trust: str | None = None, status: str | None = None) -> Item:
        if type not in TYPES:
            raise MemoryStoreError(f"Unknown type '{type}'. Choose from: {', '.join(TYPES)}")
        if source not in SOURCES:
            raise MemoryStoreError(f"Unknown source '{source}'. Choose from: {', '.join(SOURCES)}")
        title = " ".join(title.split())
        if not title:
            raise MemoryStoreError("A title is required")
        prefix, default_status, allowed = TYPES[type]
        status = status or default_status
        if status not in allowed:
            raise MemoryStoreError(f"Status '{status}' is not valid for {type}: {', '.join(allowed)}")
        trust = trust or ("confirmed" if source == "user" else "proposed")
        if trust not in TRUST:
            raise MemoryStoreError(f"Unknown trust '{trust}'")
        nums = [int(m.group(1)) for i in self.items(type) if (m := re.fullmatch(rf"{prefix}-(\d{{1,6}})", i.id))]
        item_id = f"{prefix}-{max(nums, default=0) + 1:04d}"
        now = _now()
        item = Item(id=item_id, type=type, title=title, body=body.strip(), status=status, source=source,
                    trust=trust, tags=[t for t in tags if t], evidence=[e for e in evidence if e],
                    commit=_git_head(self.root), created=now, updated=now, project=self.project_id)
        item.path = self.dir / "memory" / type / f"{item_id}-{_slug(title)}.md"
        self._write(item)
        return item

    def update(self, item_id: str, *, title=None, body=None, status=None, tags=None,
               evidence=None, trust=None) -> Item:
        item = self.get(item_id)
        if status is not None:
            allowed = TYPES[item.type][2]
            if status not in allowed:
                raise MemoryStoreError(f"Status '{status}' is not valid for {item.type}: {', '.join(allowed)}")
            item.status = status
        if trust is not None:
            if trust not in TRUST:
                raise MemoryStoreError(f"Unknown trust '{trust}'")
            item.trust = trust
        if title is not None:
            item.title = " ".join(title.split()) or item.title
        if body is not None:
            item.body = body.strip()
        if tags is not None:
            item.tags = list(tags)
        if evidence is not None:
            item.evidence = list(evidence)
        item.updated = _now()
        self._write(item)
        return item

    def confirm(self, item_id: str) -> Item:
        return self.update(item_id, trust="confirmed")

    def forget(self, item_id: str) -> Item:
        """Delete one item's file (Git keeps the history if it was committed)."""
        item = self.get(item_id)
        item.path.unlink()
        return item

    def _write(self, item: Item) -> None:
        """Replace an item's file atomically, so a crash never leaves a half-written file."""
        item.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = item.path.with_name(f".{item.path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(item.to_markdown(), encoding="utf-8", newline="\n")
        os.replace(tmp, item.path)

    def add_exclusive(self, type: str, item_id: str, title: str, body: str, *, tags=(), evidence=(),
                      source: str = "hook", trust: str = "proposed", session: str = "") -> Item:
        """Create an item under a caller-chosen unique id WITHOUT ever overwriting another file.

        The content is written to a temp file (ignored by readers) and hard-linked to its final
        name, which fails atomically if that name exists, so concurrent writers cannot clobber
        one another and readers never see a partial file. Raises FileExistsError on a duplicate id."""
        if type not in TYPES or source not in SOURCES or trust not in TRUST:
            raise MemoryStoreError("Invalid type, source or trust")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", item_id):
            raise MemoryStoreError(f"Unsafe item id: {item_id!r}")
        now = _now()
        item = Item(id=item_id, type=type, title=" ".join(title.split()), body=body.strip(),
                    status=TYPES[type][1], source=source, trust=trust, tags=list(tags), evidence=list(evidence),
                    commit=_git_head(self.root), session=session, created=now, updated=now, project=self.project_id)
        item.path = self.dir / "memory" / type / f"{item_id}.md"
        item.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = item.path.with_name(f".{item.path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(item.to_markdown(), encoding="utf-8", newline="\n")
        try:
            link_no_overwrite(tmp, item.path)
        finally:
            tmp.unlink(missing_ok=True)
        return item

    # --- handoff ------------------------------------------------------------

    def save_handoff(self, summary: str, *, done=(), open_tasks=(), decisions=(),
                     evidence=(), source: str = "user", trust: str | None = None) -> list[Item]:
        """Record a session handoff plus one task per open item and one decision
        per decision ('title :: rationale' allowed). Existing open tasks with the
        same title are not duplicated."""
        done, open_tasks, decisions = list(done), list(open_tasks), list(decisions)
        body = summary.strip()
        if done:
            body += "\n\n**Done**\n" + "\n".join(f"- {d}" for d in done)
        if open_tasks:
            body += "\n\n**Still open**\n" + "\n".join(f"- {t}" for t in open_tasks)
        if decisions:
            body += "\n\n**Decisions**\n" + "\n".join(f"- {d.split('::')[0].strip()}" for d in decisions)
        first = " ".join(summary.split())
        created = [self.add("handoff", (first[:70] + "...") if len(first) > 70 else first, body,
                            evidence=evidence, source=source, trust=trust)]
        existing = {i.title.lower() for i in self.items("task") if i.is_open_task}
        for t in open_tasks:
            if " ".join(t.split()).lower() not in existing:
                created.append(self.add("task", t, evidence=evidence, source=source, trust=trust))
        for d in decisions:
            title, _, why = d.partition("::")
            created.append(self.add("decision", title, why, evidence=evidence, source=source, trust=trust))
        return created

    # --- retrieval ----------------------------------------------------------

    def search(self, query: str, types=None, limit: int = 10) -> list[tuple[int, Item]]:
        q = _tokens(query)
        hits = []
        for item in self.items():
            if types and item.type not in types:
                continue
            score = (3 * len(q & _tokens(item.title)) + 2 * len(q & _tokens(" ".join(item.tags)))
                     + len(q & _tokens(item.body))
                     + sum(2 for e in item.evidence if e and e.lower() in query.lower()))
            if score:
                hits.append((score, item))
        hits.sort(key=lambda h: (-h[0], h[1].updated))
        return hits[:limit]


# --- brief ------------------------------------------------------------------


def _fmt(item: Item, root: Path, body_chars: int = 300) -> str:
    flags = [item.status] + (["UNCONFIRMED"] if item.trust == "proposed" else [])
    body = " ".join(item.body.split())
    if len(body) > body_chars:
        body = body[:body_chars].rstrip() + f"... (full text: memory show {item.id})"
    ev = []
    for e in item.evidence:
        missing = "" if e.startswith(("commit:", "user:", "session:", "http")) or (root / e).exists() else " (file no longer exists)"
        ev.append(e + missing)
    line = f"- [{item.id}] ({', '.join(flags)}) {item.title}"
    if body and item.type == "handoff":
        line = f"- [{item.id}] ({', '.join(flags)}) {body}"  # the title is just the summary's first line
    elif body:
        line += f" - {body}"
    if ev:
        line += f"\n  evidence: {'; '.join(ev)}"
    return line


def graph_references(root: Path, task: str, project_id: str, limit: int = 8) -> tuple[list[str], str]:
    """Code-graph hits for `task` in this project only, or ([], reason) if unavailable.
    Hits are also checked against the files on disk, so a stale graph cannot mislead."""
    try:
        from .config import Settings
        from .graph_db import GraphDatabase

        s = Settings.from_env(root / ".env")
        with GraphDatabase(s.neo4j_uri, s.neo4j_user, s.neo4j_password, connection_timeout=3) as base:
            base.verify()
            functions = base.scoped(project_id).fetch_context(task, fallback_all=False)
    except Exception as exc:  # Neo4j down, not installed, bad credentials...
        return [], f"code graph unavailable ({type(exc).__name__})"
    return verified_refs(root, functions, limit)


def verified_refs(root: Path, functions: list[dict], limit: int = 8) -> tuple[list[str], str]:
    refs = []
    for fn in functions:
        f = root / fn["file"]
        first = (fn["code"].strip().splitlines() or [""])[0].strip()
        try:
            if not f.is_file() or first not in f.read_text(encoding="utf-8", errors="ignore"):
                continue
        except OSError:
            continue
        rel = f"; calls {', '.join(fn['calls'][:4])}" if fn["calls"] else ""
        rel += f"; called by {', '.join(fn['called_by'][:4])}" if fn["called_by"] else ""
        refs.append(f"- {fn['file']}:{fn['start_line']} `{fn['name']}`{rel}")
    return refs[:limit], "" if refs else "no graph matches for this task in this project's files"


def build_brief(store: MemoryStore, task: str = "", *, max_chars: int = 6000,
                with_graph: bool = False) -> str:
    items = store.items()
    handoffs = sorted((i for i in items if i.type == "handoff"), key=lambda i: i.created)
    scored = {i.id: s for s, i in store.search(task, limit=50)} if task else {}
    overview = [i for i in items if i.type == "fact" and "overview" in i.tags]
    tasks = sorted((i for i in items if i.is_open_task), key=lambda i: (-scored.get(i.id, 0), i.created))[:10]
    picked = {i.id for i in [*overview, *tasks, *handoffs[-1:]]}
    related = [i for i in items if i.id in scored and i.id not in picked and i.type in ("decision", "fact")
               and (i.type != "decision" or i.status == "active")]
    related.sort(key=lambda i: -scored[i.id])

    out = [f"# Project brief: {store.name} (id {store.project_id})",
           "Source: .cognitive-graph memory files. Items marked UNCONFIRMED are proposals the user has "
           "not accepted; treat them as hints, not facts."]
    if task:
        out.append(f"Task: {task}")
    sections = [("Overview", overview), ("Where we left off (latest handoff)", handoffs[-1:]),
                ("Open tasks", tasks), ("Relevant decisions and facts", related[:8])]
    for title, group in sections:
        if group:
            out += ["", f"## {title}", *(_fmt(i, store.root, 700 if title.startswith("Where") else 300) for i in group)]
    if not (overview or handoffs or tasks or related):
        out += ["", "No stored memory matches yet. Use `memory add` / `memory handoff` to record some."]
    if with_graph and task:
        refs, note = graph_references(store.root, task, store.graph_id)
        out += ["", "## Code references (from the code graph, verified against files on disk)",
                *(refs or [f"({note})"])]
    for w in store.warnings:
        out.append(f"warning: {w}")
    brief = "\n".join(out)
    if len(brief) > max_chars:
        brief = brief[:max_chars].rstrip() + "\n... (brief truncated; use `memory search` for more)"
    return brief
