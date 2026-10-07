"""The prompt-time graph lookup: when it runs, what it costs, and how a slow lookup is reported.

Background: the Neo4j driver import (~0.8 s idle, 15 s+ on a saturated machine) used to run inside the
hook's 3-second budget for every prompt, so busy machines produced "code graph unavailable (TimeoutError)"."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cognitive_graph.memory import MemoryStore
from cognitive_graph.retrieval import HookConfig, SlowLookup, _with_timeout, may_name_code, prompt_brief
from cognitive_graph.memory import resolve_project

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("prompt", [
    "what does run_sync do and what calls it?",
    "why does resolve_project pick the wrong folder?",
    "explain charge_invoice",
    "what calls save()",
    "is the parseFile function right?",
    "look at app/billing.py",
    "the bug is in Billing.php",
])
def test_prompts_that_name_code_consult_the_graph(prompt):
    assert may_name_code(prompt)


@pytest.mark.parametrize("prompt", [
    "rename this variable to something clearer",
    "hai",
    "hai",
    "ok thanks",
    "Please fix the bug",
    "We decided to bill enterprise customers through Stripe",
])
def test_plain_prompts_do_not_consult_the_graph(prompt):
    assert not may_name_code(prompt)


def test_a_plain_prompt_never_calls_the_graph(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    MemoryStore.init(root, "p").add("decision", "Invoices use Stripe", "tax handling")
    calls = []
    brief, diags = prompt_brief(resolve_project(root), "hai", HookConfig(), lambda *a: calls.append(a) or [])
    assert calls == [] and brief is None and diags == []  # nothing meaningful to look up


def test_a_code_prompt_consults_the_graph_once(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    MemoryStore.init(root, "p")
    calls = []
    prompt_brief(resolve_project(root), "explain charge_invoice", HookConfig(),
                 lambda *a: calls.append(a) or [])
    assert len(calls) == 1


def test_plain_prompt_does_not_import_the_neo4j_driver():
    """Fresh process, real hook code path, no graph stub: the driver must not be imported at all."""
    code = ("import sys, json\n"
            "from cognitive_graph.hook import handle\n"
            "out = handle({'cwd': sys.argv[1], 'prompt': 'rename this variable to something clearer'}, {'COGNITIVE_GRAPH_SYNC': 'off'})\n"
            "print('neo4j' in sys.modules, out)\n")
    env = {**os.environ, "PYTHONPATH": str(REPO), "NEO4J_URI": "bolt://127.0.0.1:1"}
    r = subprocess.run([sys.executable, "-c", code, str(REPO)], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("False"), r.stdout


def test_a_slow_lookup_is_reported_as_skipped_not_as_an_outage(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    MemoryStore.init(root, "p").add("decision", "Charge invoices with Stripe", "tax")

    def slow(res, prompt, cfg):
        time.sleep(3)
        return []

    t0 = time.time()
    brief, diags = prompt_brief(resolve_project(root), "why does charge_invoice use Stripe for invoices?",
                                HookConfig(timeout=0.2), slow)
    assert time.time() - t0 < 1.5
    assert brief and "Charge invoices with Stripe" in brief
    assert len(diags) == 1 and "code graph lookup skipped: took over 0.2s" in diags[0]
    assert "TimeoutError" not in diags[0] and "unavailable" not in diags[0]


def test_a_failed_lookup_still_reports_unavailable(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    MemoryStore.init(root, "p").add("decision", "Charge invoices with Stripe", "tax")

    def down(res, prompt, cfg):
        raise ConnectionError("refused")

    _, diags = prompt_brief(resolve_project(root), "why does charge_invoice use Stripe?", HookConfig(), down)
    assert diags == ["code graph unavailable (ConnectionError); using memory only"]


def test_with_timeout_raises_slow_lookup_not_a_generic_timeout():
    with pytest.raises(SlowLookup):
        _with_timeout(lambda: time.sleep(1) or [], 0.05)
    assert issubclass(SlowLookup, TimeoutError)  # callers that catch TimeoutError still work


def test_the_hook_budget_default_is_unchanged():
    """The budget is not raised: the fix removes the unnecessary driver import rather than waiting longer."""
    assert HookConfig().timeout == 3.0
