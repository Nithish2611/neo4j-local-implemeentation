"""`cognitive-graph graph ...`: deal with graph data written before project scoping.

Old nodes have no project id, so all project-scoped queries ignore them. Nothing here
runs automatically. `adopt-legacy` assigns only files that verifiably exist in THIS
project's folder (and whose functions still match the file text); everything else is
left alone. The cleanest alternative is simply re-indexing: `cognitive-graph --ingest-only`.
"""
import argparse
import sys
from pathlib import Path

from neo4j.exceptions import AuthError, ServiceUnavailable

from .config import Settings
from .graph_db import GraphDatabase
from .memory import resolve_project


def _verified_paths(root: Path, db: GraphDatabase) -> tuple[list[str], list[str]]:
    by_file: dict[str, list[dict]] = {}
    for fn in db.legacy_functions():
        by_file.setdefault(fn["path"], []).append(fn)
    good, bad = [], []
    for path in db.legacy_file_paths():
        f = root / path
        try:
            text = f.read_text(encoding="utf-8", errors="ignore") if f.is_file() else None
        except OSError:
            text = None
        fns = by_file.get(path, [])
        ok = text is not None and all(((fn["code"] or "").strip().splitlines() or [""])[0].strip() in text for fn in fns)
        (good if ok else bad).append(path)
    return good, bad


def run(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="cognitive-graph graph", description="Manage pre-project-scoping graph data")
    p.add_argument("--project", default=".", help="project folder (default: current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("legacy-status", help="count unscoped nodes and show how many match this project's files")
    a_ = sub.add_parser("adopt-legacy", help="assign matching unscoped files to this project")
    a_.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    pg = sub.add_parser("purge-legacy", help="DELETE every unscoped node (all projects' old data)")
    pg.add_argument("--yes", action="store_true", help="required")
    a = p.parse_args(argv)

    res = resolve_project(a.project)
    if not res.ok:
        print(f"Error: {res.message}", file=sys.stderr)
        return 1
    try:
        s = Settings.from_env(res.root / ".env")
        with GraphDatabase(s.neo4j_uri, s.neo4j_user, s.neo4j_password, connection_timeout=5) as base:
            base.verify()
            db = base.scoped(res.graph_id)
            if a.cmd == "purge-legacy":
                if not a.yes:
                    print("Refusing without --yes: this deletes ALL nodes that have no project id.", file=sys.stderr)
                    return 1
                base.purge_legacy()
                print("Deleted all unscoped nodes.")
                return 0
            good, bad = _verified_paths(res.root, db)
            print(f"Project {res.name} ({res.project_id}); unscoped files in graph: {len(good) + len(bad)}")
            print(f"  match files in this project: {len(good)}")
            print(f"  not verifiable here (left untouched): {len(bad)}")
            if a.cmd == "adopt-legacy":
                if not a.apply:
                    print("Dry run. Re-run with --apply to assign the matching files to this project.")
                else:
                    print(db.adopt_legacy_files(good))
    except ServiceUnavailable:
        print("Cannot reach Neo4j. Is it running?", file=sys.stderr)
        return 1
    except AuthError:
        print("Neo4j rejected the credentials.", file=sys.stderr)
        return 1
    return 0
