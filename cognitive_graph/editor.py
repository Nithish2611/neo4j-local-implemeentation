"""Turns AI-proposed function rewrites into safe, verified edits on disk.

A proposal replaces exactly one function, located through the graph (file +
start/end line). Before anything is written the edit is checked against the file
as it is now, and afterwards the file is re-ingested so the graph stays in sync.
"""
import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .code_parser import CodeParser
from .graph_db import GraphDatabase
from .ingestor import Ingestor

_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_QUOTES = "\"'`"
# Small models often put the header on the first line inside the block, e.g.
#   ```python
#   file=a.py function=f start=1
_BODY_HEADER = re.compile(r"^[ \t]*(?:#|//|/\*)?[ \t]*(file=[^\n]*function=[^\n]*?)[ \t]*(?:\*/)?[ \t]*\n")


class EditError(Exception):
    """The edit was refused; nothing was written (or, if stated, only the graph sync failed)."""


@dataclass
class Proposal:
    file: str
    function: str
    start: int | None  # start line as the model copied it from the context header
    new_code: str
    old_code: str | None = None
    start_line: int | None = None  # resolved against the graph
    end_line: int | None = None
    error: str | None = None  # set when the target function could not be resolved
    status: str = "pending"  # pending | applied | failed | discarded
    message: str = ""

    @property
    def diff(self) -> str:
        old = (self.old_code or "").splitlines()
        new = self.new_code.splitlines()
        return "\n".join(difflib.unified_diff(old, new, f"a/{self.file}", f"b/{self.file}", lineterm=""))

    @property
    def warning(self) -> str | None:
        if self.old_code and len(self.new_code.splitlines()) < 0.5 * len(self.old_code.splitlines()):
            return "The replacement is less than half the length of the original. Check that nothing was cut off."
        return None


def _attr(info: str, key: str) -> str | None:
    match = re.search(rf"\b{key}=(\S+)", info)
    return match.group(1).strip(_QUOTES + ",") if match else None


def parse_proposals(text: str) -> list[Proposal]:
    """Extract fenced blocks headed `<lang> file=... function=... start=...`, with the
    header either on the fence line or as the first line of the block."""
    proposals = []
    for match in _FENCE.finditer(text):
        info, body = match.groups()
        if "file=" not in info:
            header = _BODY_HEADER.match(body)
            if header:
                info, body = header.group(1), body[header.end():]
        file, function, start = _attr(info, "file"), _attr(info, "function"), _attr(info, "start")
        if not file or not function:
            continue
        proposals.append(Proposal(
            file=file.replace("\\", "/").removeprefix("./"),
            function=function.removesuffix("()"),
            start=int(start) if start and start.isdigit() else None,
            new_code=body.rstrip("\n"),
        ))
    return proposals


def resolve_proposal(db: GraphDatabase, proposal: Proposal) -> Proposal:
    """Look the target up in the graph so the UI can show a real diff."""
    matches = db.get_function(proposal.file, proposal.function, proposal.start)
    if not matches and proposal.start is not None:  # the model may have mis-copied the line number
        matches = db.get_function(proposal.file, proposal.function)
    if len(matches) == 1:
        found = matches[0]
        proposal.old_code = found["code"]
        proposal.start_line, proposal.end_line = found["start_line"], found["end_line"]
    elif not matches:
        proposal.error = f"`{proposal.function}` in `{proposal.file}` is not in the graph."
    else:
        proposal.error = f"`{proposal.function}` in `{proposal.file}` is ambiguous ({len(matches)} matches)."
    return proposal


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))


def apply_proposal(
    proposal: Proposal, root: str | Path, db: GraphDatabase, parser: CodeParser, ingestor: Ingestor,
) -> str:
    """Write the proposal to disk and re-sync that file in the graph."""
    root = Path(root).resolve()
    target = (root / proposal.file).resolve()
    if root not in target.parents:
        raise EditError(f"`{proposal.file}` is outside the project folder.")
    if target.suffix.lower() not in parser.supported_extensions:
        raise EditError(f"Unsupported file type: {target.suffix}")
    if not target.is_file():
        raise EditError(f"`{proposal.file}` does not exist on disk.")

    # Re-read the graph now, not the copy taken when the answer was streamed.
    matches = db.get_function(proposal.file, proposal.function, proposal.start_line)
    if len(matches) != 1:
        raise EditError("The function is no longer in the graph at that position. Re-run ingestion and ask again.")
    found = matches[0]
    start, end = found["start_line"], found["end_line"]

    try:
        original = target.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EditError(f"Cannot edit a non-UTF-8 file: {exc}") from exc
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.replace("\r\n", "\n").split("\n")

    if _norm("\n".join(lines[start - 1:end])) != _norm(found["code"]):
        raise EditError("The file changed on disk since it was last ingested. Run a full ingestion and ask again.")

    new_lines = proposal.new_code.replace("\r\n", "\n").strip("\n").split("\n")
    updated = newline.join(lines[:start - 1] + new_lines + lines[end:])

    ext = target.suffix
    if parser.has_errors(updated, ext) and not parser.has_errors(original, ext):
        raise EditError("The change would introduce syntax errors, so it was not written.")

    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(updated.encode("utf-8"))
    os.replace(tmp, target)

    try:
        report = ingestor.ingest_file(root, target)
    except Exception as exc:
        return f"Saved `{proposal.file}`, but the graph sync failed ({type(exc).__name__}: {exc}). Run a full ingestion."
    return (f"Saved `{proposal.file}` (lines {start}-{end} replaced) and re-synced the graph: "
            f"{report.functions} function(s), {report.calls} call link(s) rebuilt.")
