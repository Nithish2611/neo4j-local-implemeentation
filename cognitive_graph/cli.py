"""CLI: parse code -> write graph -> retrieve -> ask the LLM.

    cognitive-graph --question "How does login work?"   # ingest the current folder, then ask
    cognitive-graph --path D:\\repo --question "How does login work?"
    cognitive-graph --skip-ingest --question "..."   # reuse the existing graph
    cognitive-graph --provider gemini
    cognitive-graph --reset                          # wipe THIS project's graph first

    cognitive-graph memory ...                       # project memory (see README)
    cognitive-graph graph legacy-status|adopt-legacy|purge-legacy   # pre-project-scoping data
    cognitive-graph hook install|uninstall|status|test              # Claude Code prompt hook

Graph data is scoped to the project (its id lives in <project>/.cognitive-graph/project.json,
created on first use at the Git root).
"""
import argparse
import sys

from neo4j.exceptions import AuthError, ServiceUnavailable

from cognitive_graph.agent import CodeAgent
from cognitive_graph.code_parser import CodeParser
from cognitive_graph.config import Settings
from cognitive_graph.graph_db import GraphDatabase
from cognitive_graph.ingestor import Ingestor
from cognitive_graph.memory import ensure_project
from cognitive_graph.llm import build_llm

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Zero-Loss Cognitive Code Graph")
    p.add_argument("--path", default=".", help="file or folder to ingest (default: current folder)")
    p.add_argument("--question", help="what to ask (required unless --ingest-only)")
    p.add_argument("--provider", choices=["ollama", "gemini"], help="overrides LLM_PROVIDER")
    p.add_argument("--skip-ingest", action="store_true", help="only ask, reuse the existing graph")
    p.add_argument("--ingest-only", action="store_true", help="only build the graph")
    p.add_argument("--reset", action="store_true", help="delete this project's graph data before ingesting (other projects are untouched)")
    args = p.parse_args()
    if not args.ingest_only and not args.question:
        p.error("--question is required unless --ingest-only is given")
    return args


def run(args: argparse.Namespace) -> None:
    project = ensure_project(args.path)
    if not project.ok:
        raise SystemExit(f"Error: {project.message}")
    settings = Settings.from_env(project.root / ".env")

    with GraphDatabase(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password) as base:
        base.verify()
        base.init_schema()
        db = base.scoped(project.project_id)
        print(f"Project: {project.name} (id {project.project_id}) at {project.root}")

        if args.reset:
            db.reset()
            print("Graph reset for this project.")

        if not args.skip_ingest:
            report = Ingestor(CodeParser(), db).ingest_path(args.path, root=project.root)
            print(f"Ingested {report.functions} functions from {report.files} file(s); "
                  f"{report.calls} CALLS relationship(s)"
                  + (f"; {report.skipped} file(s) skipped." if report.skipped else "."))
        if args.ingest_only:
            return

        llm = build_llm(settings, args.provider)
        print(f"\nQuestion: {args.question}")
        print(f"Asking {llm.name} ...\n")
        answer = CodeAgent(db, llm).ask(args.question)
        print(f"Context: {[f['name'] for f in answer.functions]}\n")
        print("--- AGENT RESPONSE ---")
        print(answer.text)


def main() -> int:
    if sys.argv[1:2] == ["memory"]:
        from cognitive_graph.memory_cli import run as run_memory
        return run_memory(sys.argv[2:])
    if sys.argv[1:2] == ["graph"]:
        from cognitive_graph.graph_admin import run as run_graph
        return run_graph(sys.argv[2:])
    if sys.argv[1:2] == ["hook"]:
        from cognitive_graph.hook_cli import run as run_hook
        return run_hook(sys.argv[2:])
    try:
        run(parse_args())
    except ServiceUnavailable:
        print("Cannot reach Neo4j. Is it running? (check NEO4J_URI in .env)", file=sys.stderr)
        return 1
    except AuthError:
        print("Neo4j rejected the credentials. Check NEO4J_USER / NEO4J_PASSWORD in .env.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
