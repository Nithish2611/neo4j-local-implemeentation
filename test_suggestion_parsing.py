"""Standalone regression test for the two functions in chat_ui.py that parse a
model's proposed file change and guard where it can be written.

Deliberately NOT `from chat_ui import ...`: chat_ui.py imports streamlit and
the neo4j driver at module level, so importing it here would require those
installed just to run this test, and would try to build a GraphDatabase/LLM
client (via the module-level st.cache_resource-decorated functions being
*defined*, which is harmless, but st.set_page_config() at import time is not -
it errors outside a real Streamlit run). SUGGESTION_RE and the two helpers are
copied instead. They are small and change rarely; if you edit the regex or
either helper in chat_ui.py, copy the change here too.

    python test_suggestion_parsing.py
"""
import re
from pathlib import Path

SUGGESTION_RE = re.compile(
    # [^\r\n]+ for the path, not `.` - the group otherwise inherits DOTALL from
    # the flags below and, being greedy, swallows past the end of the line and
    # into every later suggestion's fenced block before backtracking enough to
    # match. This is exactly the bug this test caught the first time around:
    # with a `.`-based path group, two suggestions in one response collapsed
    # into one, with the second suggestion's code swallowed into the first
    # suggestion's "path".
    r"^FILE:\s*(?P<path>[^\r\n]+)\r?\n```[a-zA-Z0-9_+-]*\r?\n(?P<code>.*?)```",
    re.MULTILINE | re.DOTALL,
)


def extract_suggestions(text: str):
    return [(m.group("path").strip(), m.group("code")) for m in SUGGESTION_RE.finditer(text)]


def resolve_within_project(project_root: Path, rel_path: str):
    candidate = (project_root / rel_path).resolve()
    root = project_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def test_single_suggestion_with_surrounding_prose():
    text = (
        "Here's the fix for the off-by-one bug.\n\n"
        "FILE: cognitive_graph/agent.py\n"
        "```python\n"
        'def build_context(functions):\n    return "patched"\n'
        "```\n\n"
        "Let me know if you want me to also update the tests.\n"
    )
    sugs = extract_suggestions(text)
    assert len(sugs) == 1, sugs
    assert sugs[0][0] == "cognitive_graph/agent.py"
    assert sugs[0][1].strip() == 'def build_context(functions):\n    return "patched"'


def test_two_suggestions_stay_separate():
    text = (
        "FILE: cognitive_graph/agent.py\n```python\nA = 1\n```\n\n"
        "some prose in between\n\n"
        "FILE: cognitive_graph/config.py\n```python\nB = 2\n```\n"
    )
    sugs = extract_suggestions(text)
    assert len(sugs) == 2, sugs
    assert sugs[0] == ("cognitive_graph/agent.py", "A = 1\n")
    assert sugs[1] == ("cognitive_graph/config.py", "B = 2\n")


def test_no_marker_finds_nothing():
    assert extract_suggestions("Just an explanation, no code change proposed.") == []


def test_traversal_guard():
    root = Path(".").resolve()
    assert resolve_within_project(root, "cognitive_graph/agent.py") is not None
    assert resolve_within_project(root, "../../../etc/passwd") is None
    assert resolve_within_project(root, "../outside.py") is None


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} test(s) passed.")
