"""Retrieval agent: graph context in, grounded LLM answer out."""
from collections.abc import Iterator
from dataclasses import dataclass

from .graph_db import GraphDatabase
from .llm import History, LLMClient

SYSTEM_PROMPT = (
    "Act as a Senior Developer answering questions strictly based on the provided "
    "Code Graph context. Each function lists what it calls and what calls it. "
    "If the answer is not in the context, say 'I don't have enough information in the graph.'"
)

# Appended in the chat UI. The app can only splice in a replacement for a function
# it already knows (file + start line), so edits must be proposed in this shape.
EDIT_PROTOCOL = """

CODE CHANGES
When the user asks you to change code, briefly explain the change, then for EACH
function you change output exactly one fenced code block whose opening line is
```<language> file=<File> function=<Function> start=<Start>
with File, Function and Start copied verbatim from that function's context header.
The block must contain the COMPLETE new definition of that one function: every
line from its first line to its last, with the same indentation as the context.
Never abbreviate with '...' and never include other functions in the block.
Use that header only for real proposed changes, not for illustrative snippets."""


@dataclass
class Answer:
    text: str
    functions: list[dict]


def build_context(functions: list[dict]) -> str:
    blocks = []
    for fn in functions:
        header = f"--- File: {fn['file']} | Function: {fn['name']} | Start: {fn['start_line']} ---"
        relations = []
        if fn["calls"]:
            relations.append(f"calls: {', '.join(fn['calls'])}")
        if fn["called_by"]:
            relations.append(f"called by: {', '.join(fn['called_by'])}")
        rel_line = f"({'; '.join(relations)})\n" if relations else ""
        blocks.append(f"{header}\n{rel_line}{fn['code']}")
    return "\n\n".join(blocks)


def _user_message(question: str, functions: list[dict]) -> str:
    return f"CODE GRAPH CONTEXT:\n\n{build_context(functions)}\n\nQUESTION:\n{question}"


class CodeAgent:
    def __init__(self, db: GraphDatabase, llm: LLMClient) -> None:
        self._db = db
        self._llm = llm

    def retrieve(self, question: str, history: History | None = None) -> list[dict]:
        """Graph lookup. Recent user turns are included so a follow-up like
        'do the same for its caller' still finds the functions named earlier."""
        earlier = [m["content"] for m in (history or []) if m["role"] == "user"][-2:]
        return self._db.fetch_context(" ".join([*earlier, question]))

    def stream_answer(
        self, question: str, functions: list[dict], history: History | None = None, allow_edits: bool = False,
    ) -> Iterator[str]:
        system = SYSTEM_PROMPT + (EDIT_PROTOCOL if allow_edits else "")
        return self._llm.stream(system, _user_message(question, functions), history)

    def ask(self, question: str) -> Answer:
        functions = self.retrieve(question)
        if not functions:
            return Answer("The graph is empty. Ingest some code first.", [])
        return Answer(self._llm.complete(SYSTEM_PROMPT, _user_message(question, functions)), functions)
