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

## Hands-free workflow (start here)

After **one setup command**, normal Claude Code use needs no cognitive-graph commands: no `memory init`,
no `--ingest-only`, no `memory handoff`. Open Claude Code in a Git project and work.

### One-time setup (once per machine)

```bash
pip install "cognitive-graph[ui,gemini]"     # or `pip install -e ".[ui,gemini]"` from a clone
cognitive-graph hook install                  # user scope: writes ~/.claude/settings.json, covers every project
cognitive-graph hook status                   # optional check: hooks, this folder's project, sync state, Neo4j
```

Then restart Claude Code (or open `/hooks`). This is the only manual step, and Claude Code requires it:
hooks only run once they are registered in its settings. The installer keeps your other settings, is
idempotent, and refuses to touch a settings file that is not valid JSON. Remove everything with
`cognitive-graph hook uninstall`. Use `--scope local` or `--scope project` only if you want the hooks
in one project (see "Manual and per-project setup" below).

Optional: for the code-graph features run a local Neo4j 5.x and put `NEO4J_URI`, `NEO4J_USER`,
`NEO4J_PASSWORD` in your environment or in each project's `.env`. **Without Neo4j everything else
still works** (memory, handoffs); only graph retrieval and sync are skipped, and Claude Code tells you.

### What becomes automatic

| When | What happens, with no command from you |
| --- | --- |
| Session starts (`startup`, `clear`, `compact`) | A Git project without metadata is **initialised**. The **background graph sync** starts (full initial indexing the first time). The newest session handoff (at most 14 days old) is offered as one line, labelled UNCONFIRMED. You are told about a new project, Neo4j being unreachable, or indexing in progress. |
| Every prompt | The project is resolved from the working directory. At most every 30 s the working tree is checked and **only new/changed/deleted source files** are re-indexed in the background. A short brief (memory + graph, that project only) is added before Claude sees the prompt. |
| Session ends | A **proposed handoff** is saved (task, what was done, decisions and rationale, open items, edited files) when the session produced **new context**: edited files, an open todo list, or a new decision, rationale or unfinished item stated by you or Claude, even in a discussion with no edits. A session that only repeats an earlier handoff saves nothing. A final sync is started. |

Nothing waits for indexing: the hooks return immediately and the sync runs in a detached background
process (with a per-project lock, per-run caps of 3000 files / 8 minutes, and resumable progress).
Your prompt is never blocked or rewritten. If Neo4j or anything else is unavailable, the prompt proceeds
with whatever project memory exists and you get a one-line notice.

### Identity rule (what counts as "a project")

- One project per **Git working tree**: the folder `git rev-parse --show-toplevel` reports. Opening
  Claude Code in a subfolder uses the repository's project.
- Its identity is a random id stored in `<repo>/.cognitive-graph/project.json`, created on first use.
  It is **never** derived from prompt text, folder names or remotes, so two repos with the same name are
  two projects.
- Every clone, and every linked `git worktree`, is its **own project by default**, because the automatic
  identity is written to that checkout only and excluded from Git through the repository's private
  `.git/info/exclude` (your `.gitignore` and `git status` are untouched). A submodule or a repo nested inside
  another project is separate too.
- If you *deliberately* commit `project.json` (`git add -f`) so clones share memory, linked worktrees keep the
  shared memory identity but get their **own graph scope** (`<id>~<hash of the folder>`), so their code
  graphs never mix. Two separate clones that share a committed id do share one graph scope; keep that in
  mind before committing the id.
- Not initialised automatically: folders that are not Git working trees, your home folder, the filesystem
  root, or anything when `COGNITIVE_GRAPH_AUTO_INIT=off`. An unreadable `project.json` is reported and never
  overwritten. Two sessions opening a fresh project at the same instant end up with one identity.

### What still needs you

Only the one-time install and, if you want graph features, a running Neo4j. Proposed handoffs stay
"unconfirmed" until you run `cognitive-graph memory confirm <id>` (optional). Manual commands still exist
for overrides: see "Manual and per-project setup".

### Settings (environment or a project's `.env`)

| Variable | Default | Effect |
| --- | --- | --- |
| `COGNITIVE_GRAPH_HOOK=off` | on | turn all hook behaviour off |
| `COGNITIVE_GRAPH_AUTO_INIT=off` | on | never initialise projects automatically |
| `COGNITIVE_GRAPH_SYNC=off` | on | no automatic graph sync |
| `COGNITIVE_GRAPH_SYNC_INTERVAL` | 30 | seconds between per-prompt change checks |
| `COGNITIVE_GRAPH_SYNC_MAX_FILES` / `_MAX_SECONDS` | 3000 / 480 | per-run caps; the rest continues next run |
| `COGNITIVE_GRAPH_HANDOFF=off` | on | no automatic handoffs |
| `COGNITIVE_GRAPH_HANDOFF_QUOTES=off` | on | handoffs keep only tool facts (files, branch, commit), no quoted text |
| `COGNITIVE_GRAPH_START_HANDOFF=off` / `_DAYS` | on / 14 | offer the newest handoff at session start |
| `COGNITIVE_GRAPH_HOOK_*` | see below | prompt-brief size and behaviour |

### What was verified, and what was not

Verified with real Claude Code sessions (Windows, Claude Code 2.1.285, scratch repositories): a fresh Git
repo was initialised by the SessionStart hook with no command; the identity stayed out of `git status`;
the SessionStart hook started a **detached background process that kept running after the hook and Claude
Code had exited** (the `async: true` hook option did not: Claude Code kills it at exit); a real session
that edited a file produced a proposed handoff with the task, what was done, a decision and the open item;
a brand-new session received it at start and answered from it; a read-only Q&A session added no handoff. A discussion-only session in which you stated a decision and its reason (no edits, no todo list) saved a proposed handoff, and repeating the same discussion, or asking about it later, saved nothing more.
Verified by automated tests only: the incremental sync logic (with a fake graph and the real parser),
locking, caps and resume, concurrent writers (real processes), and the Cypher's project scoping
(structurally). **Not verified: a real Neo4j.** The sync and graph retrieval against a live database, the
user-scope install through real Claude Code (tested with a redirected home directory and local scope),
interactive sessions, Ctrl+C / terminal-close exits, `resume`/`fork`, real `TodoWrite` transcripts (only
synthetic ones), and macOS/Linux (the POSIX branch is untested).

### Limits

- Change detection uses file size and modification time; an edit that preserves both is missed until
  the file changes again. Edits made outside Claude (editor, `git checkout`) are picked up at the next
  prompt (at most every 30 s) or session start, and a big initial index may take minutes, so graph
  results can be incomplete for a while (Claude Code says so).
- The session-end hook is best effort: in one run alongside other hooks the SessionEnd command did not
  fire at all, and a crash or kill produces no handoff. The next session start and prompts repair the graph.
- Handoff "decisions" and "open items" are verbatim sentences picked by keyword patterns, labelled
  heuristic; they can pick the wrong sentence or miss one, and a decision phrased without words like
  *decided / chose / instead of / rationale* is not recognised. Questions and talk about earlier sessions
  or handoffs are never recorded.
- Retrieval is keyword based. Graph context appears only when a prompt names a function or file.

## Project memory for Claude Code (new)

Persistent, per-project memory so a fresh Claude Code session can pick up where the
last one stopped. It needs **no Neo4j and no LLM**; the code graph is an optional add-on.

The rest of this section documents the manual commands; with the hooks installed you rarely need them.

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
(`{"id": "e85e95273b64", "name": "Axis RI", ...}`). The hooks create it automatically the first time
Claude Code is used in a Git repository (see "Identity rule" above); `cognitive-graph memory init` and the CLI
create it too. The active project is found from the working directory: the nearest `project.json` at or
above it, **never above the enclosing Git root**. Names, folder titles and prompt wording are never used.
If there is no valid `project.json` (and it cannot be created, e.g. outside Git), there is no project: the
hooks add nothing and no other project is searched instead. In a linked Git worktree the graph scope gets a
per-folder suffix so worktrees never share graph data.

In Neo4j, every `File` and `Function` node and every `DEFINED_IN` / `CALLS` relationship carries
`project_id`, uniqueness is per project (so Axis RI and Eros Innovation can both have `main.py`),
and every query filters on it. `GraphDatabase` refuses project operations unless you call
`.scoped(project_id)` first. `--reset` and the chat UI's "Clear Graph Memory" now delete only the
current project's data. Project memory (Markdown) is scoped by living in the project's own folder.

### Data written before project scoping

Old nodes have no `project_id`, so scoped queries can no longer see them. Nothing is deleted or
guessed automatically. Your options, from safest:

```bash
cognitive-graph --ingest-only                     # re-index this project (the hooks do this automatically now; old data stays inert)
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

### Manual and per-project setup (optional overrides)

None of this is needed for normal use. Replace `<PROJECT_PATH>` with a repository folder.

```bash
cd <PROJECT_PATH>
cognitive-graph memory init --name "Axis RI"       # only to choose the name or to commit the identity yourself
cognitive-graph sync --status                      # last sync state, files indexed, pending, running
cognitive-graph sync [--full]                      # run a sync now, in the foreground
cognitive-graph --ingest-only                      # the older whole-project ingest (still works)
cognitive-graph hook install --scope local         # hooks for this folder only (.claude/settings.local.json)
cognitive-graph hook handoff disable               # keep the other hooks, drop the session-end handoff
cognitive-graph hook status                        # hooks per scope, project, sync state, Neo4j
cognitive-graph hook test "how does invoice charging work?"   # preview the prompt brief
```

Notes on scopes:
- `--scope user` (the default) writes `~/.claude/settings.json` and applies to every folder.
- `--scope local` writes `<current folder>/.claude/settings.local.json` and `--scope project` the shared
  `<current folder>/.claude/settings.json`.
- **Claude Code reads project-level settings only from the folder you launch it in.** A hook installed at
  the Git root does not fire if you start Claude Code in a subfolder (verified), so install from the folder
  you launch from, or use user scope. The installer warns when the folder is not the Git root.
- If the `cognitive-graph-*` commands are not on your PATH, the installer writes `python -m cognitive_graph...`
  with the current interpreter's path (machine-specific: fine for user/local scope, don't commit it).
- Install and uninstall only add or remove this tool's entries.

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

## Session handoff (SessionEnd hook)

When a Claude Code session ends, the `SessionEnd` hook saves a **proposed** handoff in that project's
memory, and the next session finds it (below). Claude Code's own context, memory and compaction are
untouched. It is installed with everything else by `cognitive-graph hook install`.

### Turn it off, on, or preview it

```bash
cognitive-graph hook handoff disable [--scope user|local|project]   # remove only the SessionEnd hook
cognitive-graph hook handoff enable  [--scope ...]
cognitive-graph hook handoff preview [--transcript FILE.jsonl]      # what would be saved now; writes nothing
```

Or set `COGNITIVE_GRAPH_HANDOFF=off` (environment or the project's `.env`). `COGNITIVE_GRAPH_HANDOFF_QUOTES=off`
keeps handoffs to tool facts only (no quoted text). `COGNITIVE_GRAPH_HANDOFF_VERBOSE=1` prints a line for
skipped sessions too; by default only saves and errors are printed (to stderr, which Claude Code shows).

### When one is saved, and what is in it

A handoff is saved when the session has **new context**, that is any of:

- **edited files** (tool-call evidence),
- a **todo list with open items** (structured evidence),
- a **decision, rationale or unfinished item** that you or Claude stated, quoted verbatim, that no earlier
  handoff already contains. This is what captures a discussion-only session with no edits and no todo list.

The duplicate protection is what keeps that safe. A live run showed that a Q&A session which merely quoted the
previous handoff would otherwise re-record it as new work, so: (1) a sentence is dropped when **80% or more of its
meaningful words already appear in one sentence of an earlier handoff of this project**, which also catches
paraphrases such as "Left to do" for "Still to do"; (2) **questions** are never recorded; (3) sentences that mention
handoffs, `cognitive-graph`, "unconfirmed" or "last / previous / earlier session" are ignored; (4) a no-edit session
whose opening prompt is about earlier sessions or handoffs is skipped. A session whose only qualifying sentences are
repeats is skipped with the diagnostic `nothing new to record`. A transcript that is missing, unreadable or over 40 MB
is skipped with a diagnostic. If a decision is phrased in a way the patterns do not recognise, or you turn quoting off
(`COGNITIVE_GRAPH_HANDOFF_QUOTES=off`), a no-edit discussion saves nothing.

One Markdown file per session, `.cognitive-graph/memory/handoff/H-<UTC time>-<first 8 chars of session id>.md`,
`source: hook`, `trust: proposed`, tags `auto`, evidence `session:`, `commit:` and edited paths:

- **Task**: the first prompt you typed in the session (first 240 characters, quoted).
- **What was done**: an excerpt (700 characters) of Claude's final message, labelled as Claude's own claim.
- **Claude's todo list** at the end, if it kept one (`TodoWrite`).
- **Decisions and rationale mentioned**: up to 5 verbatim sentences that contain words like *decided, chose, opted,
  instead of, rather than, trade-off, rationale, the reason is*, from **your messages** (marked `[you]`, up to 3)
  and Claude's replies (marked `[Claude]`), because business decisions are often stated by the user and only
  acknowledged by Claude. Heuristic selection, not confirmed.
- **Unfinished or open**: up to 5 verbatim sentences from your last message and Claude's final message that contain
  words like *todo, not yet, still need, remaining, next step, unverified*. Heuristic selection, not confirmed.
- **Files edited** (from `Write`, `Edit`, `MultiEdit`, `NotebookEdit` tool calls, inside the project only),
  **uncommitted paths** from `git status`, branch and commit.

Never saved: the transcript, tool output, file contents, thinking, sub-agent conversations, environment
values, or anything paraphrased or invented. Quotes are truncated and passed through a **best-effort**
credential redactor (API-key shapes, `password=`/`token=` values, bearer tokens, URLs with credentials,
private keys, JWTs, long mixed-character blobs); redaction can miss unusual secrets, so treat handoff files
as sensitive. Paths that look sensitive (`.env*`, `*.pem`, `*.key`, `id_rsa`, names containing `secret`,
`credential`, `password`) are omitted and counted. Keep one with `cognitive-graph memory confirm <id>`, edit it
with `memory update`, or delete it with `memory forget <id>`.

### How a new session gets it

1. **Session start** (`startup`, `clear`, `compact`): the newest handoff of *this project* that is at most
   14 days old is added as one line (about 600 characters), labelled UNCONFIRMED and "may not relate to
   today's task". Resumed and forked sessions already carry their context and get nothing.
2. **Every prompt**: the normal prompt brief can also select the newest *relevant* handoff: "continue where we
   left off" phrases, a prompt naming a file the handoff recorded, or two meaningful words in common with its
   task and text. Only one handoff is ever injected, inside the 1800-character brief limit.

### Safety with simultaneous sessions

The project comes from the hook's `cwd`, exactly as for the other hooks; no project, or an unreadable
`project.json`, means nothing is written, and another project is never used as a fallback. Every session has
its own file (session id in the name; `-2`, `-3` if one session ends twice in one second). A file is written to
a hidden temporary name and hard-linked into place, which fails rather than overwriting, so simultaneous
sessions cannot clobber each other and readers never see half a file. (Manual `memory add` still numbers
items `T-0001`, `D-0001`... by scanning files, which two simultaneous manual writers could race on.)
Automatic writes are covered by a test with real concurrent processes. The hook always exits 0.

### When it runs, and its limits

- Claude Code fires `SessionEnd` when a session ends (documented reasons: `clear`, `resume`, `logout`,
  `prompt_input_exit`, `other`). Its default hook budget is 1.5 s; the installer writes a 15 s timeout.
- **Verified:** a normal end of `claude -p` runs (reported reason `other`). **Not verified:** `/clear`, `/exit`,
  Ctrl+C, closing the terminal. The documentation says they fire the hook; a crash, kill or power loss may
  leave no handoff (nothing is corrupted; that session is simply unrecorded).
- Edits made through shell commands (`sed`, generators, formatters run via Bash) are not tool-call edits and
  are not seen. The transcript format is undocumented, so the parser is defensive and would skip rather than
  guess if it changed. The `TodoWrite` layout was only exercised with synthetic transcripts.
- Handoff files contain file paths, branch names and quoted text. The automatic project folder is kept out
  of Git; if you choose to commit `.cognitive-graph/`, add `memory/handoff/` to your `.gitignore` to keep
  per-session handoffs local. If you use Claude's hosted models, a retrieved handoff line, including the
  quoted task, is sent to the model with your prompt.

## Tests

```bash
pip install pytest
pytest tests                                   # unit / fake-driver / subprocess tests; live Neo4j tests are skipped
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
| `session_start.py` | `SessionStart` hook: auto-init, starts the sync, offers the newest handoff |
| `session_end.py` | `SessionEnd` hook: builds and atomically saves a proposed session handoff |
| `graph_sync.py`, `indexing_rules.py` | bounded incremental background graph sync (change scan, lock, resumable state) |
| `graph_admin.py` | inspect / adopt / purge graph data from before project scoping |
