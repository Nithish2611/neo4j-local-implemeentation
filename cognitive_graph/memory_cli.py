"""`cognitive-graph memory ...` commands (no Neo4j or LLM needed, except `brief --graph`)."""
import argparse
import sys
from pathlib import Path

from .memory import SOURCES, TYPES, MemoryStore, MemoryStoreError, build_brief


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cognitive-graph memory", description="Git-backed project memory")
    p.add_argument("--project", default=".", help="project folder (default: current folder or nearest parent with memory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create .cognitive-graph/ in the project")
    s.add_argument("--name", help="project name (default: folder name)")

    s = sub.add_parser("add", help="save a fact, decision, task or handoff")
    s.add_argument("type", choices=list(TYPES))
    s.add_argument("title")
    s.add_argument("--body", default="")
    s.add_argument("--tag", action="append", default=[], help="repeatable; tag a fact 'overview' to always include it in briefs")
    s.add_argument("--evidence", action="append", default=[], help="repeatable: a file path, 'commit:<sha>' or 'user:<note>'")
    s.add_argument("--source", choices=SOURCES, default="user")
    s.add_argument("--status")

    s = sub.add_parser("list", help="list items")
    s.add_argument("--type", choices=list(TYPES))
    s.add_argument("--all", action="store_true", help="include done/dropped/superseded/outdated items")

    s = sub.add_parser("show", help="print one item")
    s.add_argument("id")

    s = sub.add_parser("search", help="keyword search")
    s.add_argument("query")
    s.add_argument("--type", action="append", choices=list(TYPES))

    s = sub.add_parser("brief", help="print a concise context brief for a task")
    s.add_argument("task", nargs="?", default="", help="what you are about to work on")
    s.add_argument("--max-chars", type=int, default=6000)
    s.add_argument("--graph", action="store_true", help="add code-graph references (needs Neo4j)")

    s = sub.add_parser("handoff", help="save an end-of-session handoff")
    s.add_argument("--summary", required=True)
    s.add_argument("--done", action="append", default=[], help="repeatable")
    s.add_argument("--open", action="append", default=[], dest="open_tasks", help="repeatable; also creates a task")
    s.add_argument("--decision", action="append", default=[], help="repeatable: 'title :: why'; also creates a decision")
    s.add_argument("--evidence", action="append", default=[])
    s.add_argument("--source", choices=SOURCES, default="user")

    s = sub.add_parser("update", help="change an item")
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--body")
    s.add_argument("--status")

    s = sub.add_parser("confirm", help="mark a proposed item as accepted")
    s.add_argument("id", nargs="+")

    s = sub.add_parser("forget", help="delete an item's file")
    s.add_argument("id")
    return p


def _row(i) -> str:
    mark = " [unconfirmed]" if i.trust == "proposed" else ""
    return f"{i.id:8} {i.status:11} {i.title}{mark}"


def run(argv: list[str]) -> int:
    a = _parser().parse_args(argv)
    try:
        if a.cmd == "init":
            root = Path(a.project).resolve()
            existed = (root / ".cognitive-graph" / "project.json").exists()
            st = MemoryStore.init(root, a.name)
            print(f"{'Already initialised' if existed else 'Initialised'}: {st.name} (id {st.project_id}) at {st.dir}")
            return 0
        st = MemoryStore.open(a.project)
        if a.cmd == "add":
            i = st.add(a.type, a.title, a.body, tags=a.tag, evidence=a.evidence, source=a.source, status=a.status)
            print(f"Saved {i.id} ({i.trust}) -> {i.path.relative_to(st.root)}")
        elif a.cmd == "list":
            hidden = {"done", "dropped", "superseded", "outdated"}
            for i in st.items(a.type):
                if a.all or i.status not in hidden:
                    print(_row(i))
        elif a.cmd == "show":
            i = st.get(a.id)
            print(i.path.read_text(encoding="utf-8"))
        elif a.cmd == "search":
            hits = st.search(a.query, a.type)
            print("\n".join(f"{s:3} {_row(i)}" for s, i in hits) or "No matches.")
        elif a.cmd == "brief":
            print(build_brief(st, a.task, max_chars=a.max_chars, with_graph=a.graph))
        elif a.cmd == "handoff":
            for i in st.save_handoff(a.summary, done=a.done, open_tasks=a.open_tasks, decisions=a.decision,
                                     evidence=a.evidence, source=a.source):
                print(f"Saved {i.id} ({i.trust}) {i.title}")
        elif a.cmd == "update":
            i = st.update(a.id, title=a.title, body=a.body, status=a.status)
            print(f"Updated {i.id}")
        elif a.cmd == "confirm":
            for item_id in a.id:
                print(f"Confirmed {st.confirm(item_id).id}")
        elif a.cmd == "forget":
            i = st.forget(a.id)
            print(f"Deleted {i.id} ({i.title})")
        for w in st.warnings:
            print(f"warning: {w}", file=sys.stderr)
    except MemoryStoreError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
