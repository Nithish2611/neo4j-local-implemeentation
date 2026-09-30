"""MCP server (stdio) exposing project memory to Claude Code.

    claude mcp add cognitive-graph -- cognitive-graph-mcp

The server serves the project in its working directory (Claude Code starts it in
your project folder), or the folder in $COGNITIVE_GRAPH_PROJECT / --project.
Anything Claude saves is stored as 'proposed' unless `confirmed=True`, which
Claude must only pass after the user has agreed to save it.
"""
import argparse
import functools
import os
import sys
from pathlib import Path

from .memory import TYPES, MemoryStore, MemoryStoreError, build_brief


def _fmt(item) -> str:
    return f"{item.id} [{item.type}/{item.status}/{item.trust}] {item.title}"


def create_server(project: str):
    try:
        try:
            from mcp.server.mcpserver import MCPServer as Server  # mcp 2.x
        except ImportError:
            from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
    except ImportError as exc:
        raise SystemExit('The MCP server needs the "mcp" package: pip install "cognitive-graph[mcp]"') from exc

    mcp = Server("cognitive-graph")

    def store() -> MemoryStore:
        return MemoryStore.open(project)

    def guarded(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except MemoryStoreError as exc:
                return f"Error: {exc}"
        return wrapper

    @mcp.tool()
    @guarded
    def prepare_context(task: str = "", include_code_graph: bool = False) -> str:
        """Call at the START of a session or task. Returns a concise brief of this project's
        saved memory (latest handoff, open tasks, relevant decisions/facts). Items marked
        UNCONFIRMED are unaccepted proposals: treat them as hints, not facts."""
        return build_brief(store(), task, with_graph=include_code_graph)

    @mcp.tool()
    @guarded
    def search_memory(query: str, type: str = "") -> str:
        """Keyword search over saved project memory. type: fact, decision, task or handoff (optional)."""
        hits = store().search(query, [type] if type else None)
        return "\n".join(f"{s:3} {_fmt(i)}" for s, i in hits) or "No matches."

    @mcp.tool()
    @guarded
    def get_memory(id: str) -> str:
        """Return the full text of one memory item, e.g. D-0003."""
        return store().get(id).path.read_text(encoding="utf-8")

    @mcp.tool()
    @guarded
    def save_memory(type: str, title: str, body: str = "", evidence: list[str] | None = None,
                    confirmed: bool = False) -> str:
        """Save a fact, decision or task. Set confirmed=True ONLY if the user has explicitly agreed
        to save it; otherwise it is stored as 'proposed' for the user to confirm later. Give evidence
        (file paths, 'commit:<sha>') when you have it. Never save guesses as facts."""
        if type not in TYPES or type == "handoff":
            return "Error: type must be fact, decision or task (use save_handoff for handoffs)"
        i = store().add(type, title, body, evidence=evidence or [], source="claude",
                        trust="confirmed" if confirmed else "proposed")
        return f"Saved {_fmt(i)}"

    @mcp.tool()
    @guarded
    def save_handoff(summary: str, done: list[str] | None = None, open_tasks: list[str] | None = None,
                     decisions: list[str] | None = None, confirmed: bool = False) -> str:
        """Call at the END of a session. Saves what was done, what is still open and decisions
        made ('title :: why'). Show the user the handoff and set confirmed=True only after they
        agree; otherwise it is stored as 'proposed'."""
        made = store().save_handoff(summary, done=done or [], open_tasks=open_tasks or [],
                                    decisions=decisions or [], source="claude",
                                    trust="confirmed" if confirmed else "proposed")
        return "Saved:\n" + "\n".join(_fmt(i) for i in made)

    @mcp.tool()
    @guarded
    def update_memory(id: str, status: str = "", body: str = "", confirm: bool = False) -> str:
        """Change an item's status (e.g. task -> done), body, or mark it user-confirmed.
        Only pass confirm=True when the user has agreed."""
        s = store()
        i = s.update(id, status=status or None, body=body or None, trust="confirmed" if confirm else None)
        return f"Updated {_fmt(i)}"

    return mcp


def main() -> None:
    p = argparse.ArgumentParser(description="cognitive-graph MCP server (stdio)")
    p.add_argument("--project", default=os.environ.get("COGNITIVE_GRAPH_PROJECT", "."))
    args = p.parse_args()
    create_server(str(Path(args.project).resolve())).run()


if __name__ == "__main__":
    sys.exit(main())
