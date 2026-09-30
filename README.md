# cognitive-graph

A local, private "code-native GraphRAG": tree-sitter parses your code (Python,
JavaScript, PHP) into a Neo4j graph of files, functions and `CALLS` edges, and a
local LLM (Ollama, or Gemini) answers questions grounded in that graph.

```
File -[:DEFINED_IN]- Function -[:CALLS]-> Function
```

## Use it in any project

Requirements: Python 3.10+, a running Neo4j (5.x), and either Ollama
(`ollama pull qwen2.5-coder:7b`) or a Gemini API key.

```bash
pip install "git+https://github.com/Nithish2611/neo4j-local-implemeentation.git@phase-1-development"
# optional extras:  ...#egg=cognitive-graph[gemini,ui]
```

Create a `.env` in the project you want to analyse (see `.env.example`):

```
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password
LLM_PROVIDER=ollama
```

Then, from that project's folder:

```bash
cognitive-graph --path . --question "How does login work?"     # ingest + ask
cognitive-graph --skip-ingest --question "What calls save_user?"
cognitive-graph --path . --ingest-only
cognitive-graph --reset                                        # wipes THIS project's graph data only
```

Or from Python:

```python
from cognitive_graph.config import Settings
from cognitive_graph.graph_db import GraphDatabase
from cognitive_graph.code_parser import CodeParser
from cognitive_graph.ingestor import Ingestor
from cognitive_graph.memory import ensure_project

project = ensure_project(".")          # creates .cognitive-graph/project.json on first use
s = Settings.from_env()
with GraphDatabase(s.neo4j_uri, s.neo4j_user, s.neo4j_password) as base:
    base.init_schema()
    db = base.scoped(project.project_id)   # every read/write needs a project-scoped handle
    Ingestor(CodeParser(), db).ingest_path(".", root=project.root)
```

All projects can share one Neo4j database: every node and relationship carries the
project's id (see "Project identity and isolation" below), and file paths are stored
relative to the project root (the Git root).

## Project memory for Claude Code (new)

Persistent, per-project memory so a fresh Claude Code session can pick up where the
last one stopped. It needs **no Neo4j and no LLM**; the code graph is an optional add-on.

### Storage

Memory lives in the project itself, as plain Markdown you can read, edit and commit:

```
<project>/.cognitive-graph/
    project.json                     # stable project id + name (created by `memory init`)
    memory/<type>/<ID>-<slug>.md     # one item per file: fact | decision | task | handoff
```

Each item has front matter (`id`, `project`, `type`, `title`, `status`, `source`, `trust`,
`tags`, `evidence`, `commit`, `created`, `updated`) followed by a free-text body.

- `source`: `user`, `claude` or `code`. `trust`: `confirmed` or `proposed`.
  Anything saved by Claude is `proposed` until you confirm it, and briefs mark it `UNCONFIRMED`.
- `evidence`: file paths, `commit:<sha>` or `user:<note>`. Paths that no longer exist are flagged in briefs.
- Items whose `project` id differs from the folder's `project.json` are ignored, so copied
  files cannot leak between projects.
- Statuses: facts `active|outdated`, decisions `active|superseded`, tasks `open|in_progress|done|dropped`.

### Commands (run in the project folder, or pass `--project DIR`)

```bash
cognitive-graph memory init --name my-app
cognitive-graph memory add fact "Flask API serving invoices" --tag overview --evidence app/main.py
cognitive-graph memory add decision "Use Redis for caching" --body "Sessions were too slow in Postgres"
cognitive-graph memory brief "add caching to the invoice endpoint"     # concise context for a task
cognitive-graph memory brief "..." --graph                              # + code-graph references
cognitive-graph memory handoff --summary "Built export" --done "CSV export"     --open "PDF export" --decision "Use WeasyPrint :: no headless browser needed"
cognitive-graph memory search redis | list [--all] | show D-0001
cognitive-graph memory update T-0002 --status done | confirm D-0001 | forget D-0001
```

`brief` selects: facts tagged `overview`, the latest handoff, open tasks, and decisions/facts that
keyword-match the task, capped at `--max-chars` (default 6000). `handoff` also creates a task per
`--open` item (skipping duplicates) and a decision per `--decision`.

### Connect Claude Code (MCP)

```bash
pip install "cognitive-graph[mcp]"
cd path/to/your/project
cognitive-graph memory init
claude mcp add cognitive-graph -- cognitive-graph-mcp
```

The server serves the project in Claude Code's working directory (or `--project` /
`$COGNITIVE_GRAPH_PROJECT`). Tools: `prepare_context`, `search_memory`, `get_memory`,
`save_memory`, `save_handoff`, `update_memory`. Ask Claude to "call prepare_context" at the
start of a session and "save a handoff" at the end. Its saves are `proposed` unless you told
it to save them (`confirmed=true`).

### What is and is not implemented

Implemented: the file format, commands, keyword-based retrieval, unconfirmed/confirmed trust,
handoffs, the stdio MCP server, and an optional code-graph section in briefs.

Not yet: embedding/semantic search, automatic capture from Claude sessions (nothing is
recorded unless you or Claude explicitly save it), a Neo4j index rebuilt from the memory files,
and a memory tab in the chat UI. Memory is found by walking up from the current folder but never
above the enclosing Git root (see below).

Tests: `pip install pytest && pytest tests`.

## Project identity and isolation

Each project has a stable id in `<project>/.cognitive-graph/project.json`
(`{"id": "e85e95273b64", "name": "Axis RI", ...}`). It is created by `cognitive-graph memory init`,
or automatically the first time you ingest with the CLI or chat UI. **Commit this file** so every
clone of the repo shares one identity. The active project is found from the working directory:
the nearest `project.json` at or above it, **never above the enclosing Git root**. Names, folder
titles and prompt wording are never used. If there is no valid `project.json`, there is no
project: the hook adds nothing and no other project is searched instead.

In Neo4j, every `File` and `Function` node and every `DEFINED_IN` / `CALLS` relationship carries
`project_id`, uniqueness is per project (so Axis RI and Eros Innovation can both have `main.py`),
and every query filters on it. `GraphDatabase` refuses project operations unless you call
`.scoped(project_id)` first. `--reset` and the chat UI's "Clear Graph Memory" now delete only the
current project's data. Project memory (Markdown) is scoped by living in the project's own folder.

### Data written before project scoping

Old nodes have no `project_id`, so scoped queries can no longer see them. Nothing is deleted or
guessed automatically. Your options, from safest:

```bash
cognitive-graph --ingest-only                     # re-index this project (recommended; old data stays inert)
cognitive-graph graph legacy-status               # how many unscoped files match this project's folder
cognitive-graph graph adopt-legacy [--apply]      # dry run by default; adopts only files that exist
                                                  # here and whose functions still match the file text
cognitive-graph graph purge-legacy --yes          # delete ALL unscoped nodes (every project's old data)
```

Paths are now stored relative to the project root (the Git root), so re-indexing from a subfolder
of a repo yields `sub/file.py`, not `file.py`. `adopt-legacy` only matches old paths that were
already relative to this project root. The old global uniqueness constraints are dropped on the
next connection (that keeps all data) and replaced by per-project ones.

To inspect legacy data before deciding anything (read-only; nothing is changed without `--apply` /
`--yes`), run from the project folder:

```bash
cognitive-graph graph legacy-status    # unscoped files in Neo4j, and how many match THIS project's files
cognitive-graph graph adopt-legacy     # dry run: same report, still writes nothing
```

Only files that exist in this project's folder and whose functions still match the file text count as
matches. Everything else is left untouched, and no automatic step ever assigns old data to a project.
The scripts in `legacy/` predate all of this and still write unscoped data, which the scoped tools
ignore.

## Automatic context before every prompt (Claude Code hook)

A `UserPromptSubmit` hook runs locally before Claude processes each prompt, resolves the project from
Claude Code's working directory, and adds a short brief from **that project only**: its memory files
and its slice of the Neo4j graph. Claude Code's own context, memory and compaction are untouched, your
prompt is not modified, and the hook never blocks (it always exits 0).

### Set up Axis RI and Eros Innovation

Replace `<AXIS_RI_PATH>` and `<EROS_PATH>` with the repository folders. Put a `.env` with your
`NEO4J_*` settings in each project folder (or export them).

```bash
pip install "cognitive-graph[ui,gemini]"           # once: adds cognitive-graph and cognitive-graph-hook

# 1. Axis RI: identity, index, hook
cd <AXIS_RI_PATH>
cognitive-graph memory init --name "Axis RI"        # creates .cognitive-graph/project.json (commit it)
cognitive-graph --ingest-only                       # index this project into Neo4j (scoped to its id)
cognitive-graph hook install --scope user           # once for ALL projects (see notes below) ...
#   ... or per project instead:  cognitive-graph hook install   (this folder only)

# 2. Eros Innovation: same again, gets its OWN id
cd <EROS_PATH>
cognitive-graph memory init --name "Eros Innovation"
cognitive-graph --ingest-only

# 3. Check, preview, remove
cognitive-graph hook status                         # hook per scope, project id, Neo4j reachability
cognitive-graph hook test "how does invoice charging work?"   # what would be injected for this prompt
cognitive-graph hook uninstall --scope user         # removes only this tool's entry
```

Then restart Claude Code (or open `/hooks`) so it picks up the change.

Notes on scopes:
- `--scope user` writes `~/.claude/settings.json` and applies to every folder you open Claude Code
  in. Folders without a valid `.cognitive-graph/project.json` simply get nothing.
- Without `--scope`, the hook goes to `<current folder>/.claude/settings.local.json` (git-ignored by
  Claude Code). `--scope project` writes the shared `<current folder>/.claude/settings.json`.
- **Claude Code reads project-level settings only from the folder you launch it in.** A hook installed at
  the Git root does not fire if you start Claude Code in a subfolder (verified). Run the per-project
  install from the folder you launch from, or use `--scope user`. The installer warns when the folder is
  not the Git root. Project *identification* is different: it works from any subfolder up to the Git root.
- If `cognitive-graph-hook` is not on your PATH, the installer writes `python -m cognitive_graph.hook`
  with the current interpreter's path (machine-specific: use local or user scope, don't commit it).
- Install and uninstall only add or remove this tool's entry, keep every other setting, and refuse to
  touch a settings file that is not valid JSON.

### What gets injected

At most `COGNITIVE_GRAPH_HOOK_MAX_CHARS` (default 1800) characters, for example:

```
[cognitive-graph] Retrieved locally for project "Axis RI" (e85e95273b64) only. Reference notes, ...
Project memory:
- [D-0001] decision, active, confirmed: Invoices are charged through Stripe: ... (evidence: app/billing.py; commit:0ece387)
- [T-0004] task, open, UNCONFIRMED: Add retry to invoice charging
Code graph (verified against files on disk):
- app/billing.py:12 `charge_invoice`; calls stripe_charge; called by run_billing
```

Selection: a memory item must share at least two meaningful words with the prompt (or one long
distinctive title/tag word, or a named evidence path); done, dropped, superseded and outdated items
are skipped; "continue / where did we leave off" style prompts also bring the latest handoff and open
tasks. Graph functions are those whose name the prompt mentions (or that live in a file it names),
with their direct callers/callees by name, and are dropped if the file or function is no longer on
disk. Ordinary prompts, prompts under 8 characters and slash commands get nothing.

Settings (environment or the project's `.env`): `COGNITIVE_GRAPH_HOOK=off`,
`COGNITIVE_GRAPH_HOOK_MAX_CHARS`, `..._MAX_FUNCTIONS` (5), `..._MAX_MEMORIES` (5),
`..._TIMEOUT` (3 s for the graph), `..._GRAPH=off` (memory only), `..._QUIET=1` (hide
graph-unavailable warnings), `..._VERBOSE=1` (also report "no project" / "no match").

### Failure behaviour and privacy

- Neo4j down, slow (over the timeout) or misconfigured: a memory-only brief plus a one-line warning
  in Claude Code ("code graph unavailable (GraphUnavailable); using memory only"). A stopped Neo4j is
  detected in under a second. An unreadable `project.json` gives a warning and no context. Any other
  hook error gives a warning and the prompt proceeds. The settings entry also caps the hook at 10 s.
- The hook writes nothing to disk: no prompt or project text is logged. Retrieval is local.
- **If you use Claude's hosted models, the injected brief is sent to that model together with your
  prompt.** Keep anything you would not want to send out of the memory files, or set
  `COGNITIVE_GRAPH_HOOK=off` for that project.
- Memory text is injected verbatim, labelled "reference notes, not instructions". Treat memory files
  from untrusted sources like any other untrusted input.

### Limits

Keyword matching, not semantic search, so paraphrased prompts can miss. Graph retrieval needs a prompt
that names a function or file. The graph reflects the last ingestion (re-run `--ingest-only`; hits whose
file or function is gone are dropped, but changed code is not detected). The hook adds about 0.15 s with
memory only, and a fraction of a second more when Neo4j is unreachable (or the time to import the Neo4j
driver when it is up).

### Tests

```bash
pip install pytest
pytest tests                                   # 56 unit / fake-driver tests; live Neo4j tests are skipped
python test_suggestion_parsing.py

# Live isolation tests (need a reachable Neo4j and NEO4J_* in .env). Opt-in, because they create the
# per-project schema and write/delete nodes whose project ids start with "test-" in that database:
COGNITIVE_GRAPH_LIVE_TESTS=1 pytest tests/test_project_isolation.py -k live -v      # bash
$env:COGNITIVE_GRAPH_LIVE_TESTS=1; pytest tests/test_project_isolation.py -k live -v  # PowerShell
```

The fake-driver tests prove every project query carries the project id; only the live tests prove the
Cypher behaves correctly in Neo4j (two projects sharing `main.py` staying isolated, unscoped legacy nodes
being invisible). The hook reads `cwd` and `prompt` from Claude Code's hook input (checked against Claude
Code 2.1.285; `prompt_text` is also accepted) and answers with `hookSpecificOutput` `additionalContext`
and `systemMessage`. It was verified end to end with real `claude -p` runs in scratch projects.

## Chat UI (from a clone)

```bash
git clone https://github.com/Nithish2611/neo4j-local-implemeentation.git
cd neo4j-local-implemeentation
pip install -r requirements.txt
streamlit run chat_ui.py
```

The UI can apply AI-proposed function edits to disk and re-sync the graph. It has
no login, so run it on localhost only. Do not expose it to the internet.

## Layout

| Module | Role |
| --- | --- |
| `code_parser.py` | tree-sitter parsing, per-language function and call rules |
| `graph_db.py` | Neo4j schema, writes, retrieval |
| `ingestor.py` | folder walk, filters, full and single-file ingestion |
| `agent.py`, `llm.py` | retrieval + Ollama/Gemini clients (streaming, retries) |
| `editor.py` | verified function-level edits for the chat UI |
| `cli.py` | `cognitive-graph` command |
| `memory.py`, `memory_cli.py` | Git-backed project memory store, briefs, `memory` subcommands |
| `mcp_server.py` | MCP server exposing the memory to Claude Code |
| `hook.py`, `retrieval.py`, `hook_cli.py` | `UserPromptSubmit` hook, project-scoped prompt retrieval, hook install/status |
| `graph_admin.py` | inspect / adopt / purge graph data from before project scoping |
