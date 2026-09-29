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
cognitive-graph --reset                                        # wipes ALL graph data
```

Or from Python:

```python
from cognitive_graph.config import Settings
from cognitive_graph.graph_db import GraphDatabase
from cognitive_graph.code_parser import CodeParser
from cognitive_graph.ingestor import Ingestor

s = Settings.from_env()
with GraphDatabase(s.neo4j_uri, s.neo4j_user, s.neo4j_password) as db:
    db.init_schema()
    Ingestor(CodeParser(), db).ingest_path(".")
```

All projects share one Neo4j database, and file paths are stored relative to the
ingested folder. Point each project at its own database or run `--reset` between
projects if their file names could collide.

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
