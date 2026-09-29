"""Walks a project, parses every supported source file and writes the code graph."""
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from neo4j.exceptions import ClientError

from .code_parser import CodeParser, FunctionEntity
from .graph_db import GraphDatabase

SKIP_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", "legacy", "build", "dist", "vendor", "vendor_php"}
# Third-party bundles that are commonly checked in next to first-party code.
SKIP_FILE_HINTS = ("jquery", "bootstrap", "modernizr", "popper", "slick", "chart.js", "chart.min", "chart.bundle")

Symbol = tuple[str, str, int]  # (path, name, start_line): identifies one Function node


@dataclass
class IngestReport:
    files: int = 0
    functions: int = 0
    calls: int = 0
    skipped: int = 0


class Ingestor:
    def __init__(self, parser: CodeParser, db: GraphDatabase) -> None:
        self._parser = parser
        self._db = db

    # --- full ingestion -----------------------------------------------------

    def ingest_path(self, target: str | Path) -> IngestReport:
        target = Path(target).resolve()
        root = target.parent if target.is_file() else target
        parsed: dict[str, list[FunctionEntity]] = {}
        skipped = 0

        for file in self._source_files(target):
            rel = file.relative_to(root).as_posix()
            try:
                parsed[rel] = self._parser.parse_file(file)
            except Exception as exc:  # one bad file must not abort the whole run
                print(f"  skipped {rel}: {type(exc).__name__}: {exc}")
                skipped += 1

        for rel in list(parsed):
            try:
                self._db.replace_file(rel, parsed[rel])
            except ClientError as exc:  # e.g. constraint violation; connection errors still propagate
                print(f"  skipped {rel}: {exc.code}: {exc.message}")
                del parsed[rel]  # no CALLS edges to nodes that were not written
                skipped += 1

        index: dict[str, list[Symbol]] = {}
        callers = []
        for path, functions in parsed.items():
            for fn in functions:
                symbol = (path, fn.name, fn.start_line)
                index.setdefault(fn.name, []).append(symbol)
                callers.append((symbol, fn.calls))

        edges = self._resolve_calls(callers, index)
        self._db.link_calls(edges)
        return IngestReport(
            files=len(parsed),
            functions=sum(len(f) for f in parsed.values()),
            calls=len(edges),
            skipped=skipped,
        )

    # --- targeted update of one file ----------------------------------------

    def ingest_file(self, root: str | Path, file: str | Path) -> IngestReport:
        """Re-ingest a single file after it changed on disk.

        Replacing a file's nodes also deletes the CALLS edges other files had
        pointing into it, so they are rebuilt here: outgoing edges from this
        file's functions, plus incoming edges from any function in the graph
        whose stored call list mentions a name defined in this file."""
        root = Path(root).resolve()
        file = Path(file).resolve()
        rel = file.relative_to(root).as_posix()

        functions = self._parser.parse_file(file)
        self._db.replace_file(rel, functions)

        callers: dict[Symbol, Iterable[str]] = {(rel, f.name, f.start_line): f.calls for f in functions}
        if functions:
            for row in self._db.functions_calling({f.name for f in functions}):
                callers[(row["path"], row["name"], row["start_line"])] = row["calls"]

        call_names = {name for calls in callers.values() for name in calls}
        index: dict[str, list[Symbol]] = {}
        if call_names:
            for row in self._db.functions_named(call_names):
                index.setdefault(row["name"], []).append((row["path"], row["name"], row["start_line"]))

        edges = self._resolve_calls(callers.items(), index)
        self._db.link_calls(edges)
        return IngestReport(files=1, functions=len(functions), calls=len(edges))

    # --- helpers --------------------------------------------------------------

    def _source_files(self, target: Path):
        supported = self._parser.supported_extensions
        if target.is_file():
            if target.suffix.lower() in supported:
                yield target
            return
        for file in sorted(target.rglob("*")):
            name = file.name.lower()
            if file.suffix.lower() not in supported or name.endswith(".min.js"):
                continue
            if any(hint in name for hint in SKIP_FILE_HINTS):
                continue
            if not SKIP_DIRS.intersection(file.relative_to(target).parts[:-1]) and file.is_file():
                yield file

    @staticmethod
    def _resolve_calls(
        callers: Iterable[tuple[Symbol, Iterable[str]]],
        index: dict[str, list[Symbol]],
    ) -> list[dict]:
        """Match call names to function nodes. A callee in the same file wins;
        otherwise it links only if the name is unique among files of the same
        language (a PHP call never links to a Python function). Ambiguous or
        unknown names (builtins, libraries) produce no edge."""
        language = CodeParser.language_id
        edges: dict[tuple, dict] = {}
        for (path, name, start), calls in callers:
            for callee_name in calls:
                candidates = index.get(callee_name, [])
                same_file = [c for c in candidates if c[0] == path]
                same_lang = [c for c in candidates if language(c[0]) == language(path)]
                targets = same_file or (same_lang if len(same_lang) == 1 else [])
                for to_path, to_name, to_start in targets:
                    edges[(path, start, to_path, to_start)] = {
                        "from_path": path, "from_name": name, "from_line": start,
                        "to_path": to_path, "to_name": to_name, "to_line": to_start,
                    }
        return list(edges.values())
