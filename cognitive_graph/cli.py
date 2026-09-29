"""CLI: parse code -> write graph -> retrieve -> ask the LLM.

    cognitive-graph --question "How does login work?"   # ingest the current folder, then ask
    cognitive-graph --path D:\\repo --question "How does login work?"
    cognitive-graph --skip-ingest --question "..."   # reuse the existing graph
    cognitive-graph --provider gemini
    cognitive-graph --reset                          # wipe the whole graph first
"""
import argparse
import sys

from neo4j.exceptions import AuthError, ServiceUnavailable

from cognitive_graph.agent import CodeAgent
from cognitive_graph.code_parser import CodeParser
from cognitive_graph.config import Settings
from cognitive_graph.graph_db import GraphDatabase
from cognitive_graph.ingestor import Ingestor
from cognitive_graph.llm import build_llm

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Zero-Loss Cognitive Code Graph")
    p.add_argument("--path", default=".", help="file or folder to ingest (default: current folder)")
    p.add_argument("--question", help="what to ask (required unless --ingest-only)")
    p.add_argument("--provider", choices=["ollama", "gemini"], help="overrides LLM_PROVIDER")
    p.add_argument("--skip-ingest", action="store_true", help="only ask, reuse the existing graph")
    p.add_argument("--ingest-only", action="store_true", help="only build the graph")
    p.add_argument("--reset", action="store_true", help="delete ALL graph data before ingesting")
    args = p.parse_args()
    if not args.ingest_only and not args.question:
        p.error("--question is required unless --ingest-only is given")
    return args


def run(args: argparse.Namespace) -> None:
    settings = Settings.from_env()

    with GraphDatabase(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password) as db:
        db.verify()
        db.init_schema()

        if args.reset:
            db.reset()
            print("Graph reset.")

        if not args.skip_ingest:
            report = Ingestor(CodeParser(), db).ingest_path(args.path)
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
