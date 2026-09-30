"""Streamlit chat UI for the code graph.  Run with:  streamlit run chat_ui.py"""
from pathlib import Path

import streamlit as st
from neo4j.exceptions import AuthError, ServiceUnavailable

from cognitive_graph.agent import CodeAgent
from cognitive_graph.code_parser import CodeParser
from cognitive_graph.config import Settings
from cognitive_graph.editor import EditError, Proposal, apply_proposal, parse_proposals, resolve_proposal
from cognitive_graph.graph_db import GraphDatabase
from cognitive_graph.ingestor import Ingestor
from cognitive_graph.llm import build_llm
from cognitive_graph.memory import ensure_project

st.set_page_config(page_title="Code Graph Assistant", page_icon="🧠", layout="wide")


# --- shared resources (created once per server process) -----------------------

@st.cache_resource
def get_backend():
    settings = Settings.from_env()
    base_db = GraphDatabase(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    base_db.verify()
    base_db.init_schema()
    return settings, base_db, CodeParser()


@st.cache_resource
def get_llm(provider: str):
    return build_llm(get_backend()[0], provider)


try:
    settings, base_db, parser = get_backend()
except ServiceUnavailable:
    st.error("Cannot reach Neo4j. Start it and check NEO4J_URI in .env, then reload.")
    st.stop()
except AuthError:
    st.error("Neo4j rejected the credentials. Check NEO4J_USER / NEO4J_PASSWORD in .env.")
    st.stop()

st.session_state.setdefault("messages", [])
st.session_state.setdefault("confirm_clear", False)
st.session_state.setdefault("project_path", str(Path("sample_project").resolve()))


def project_backend():
    """(project, graph handle, ingestor) for the folder in the sidebar. Every graph call goes
    through a handle scoped to that folder's project id, so projects never mix. The id is
    created on first use at the Git root (or the folder) in .cognitive-graph/project.json."""
    proj = ensure_project(Path(st.session_state.project_path).expanduser())
    if not proj.ok:
        return proj, None, None
    scoped = base_db.scoped(proj.project_id)
    return proj, scoped, Ingestor(parser, scoped)


# --- callbacks (run before the next rerun, so state is fresh when the page redraws) ---

def on_apply(proposal: Proposal) -> None:
    try:
        proj, db, ingestor = project_backend()
        if db is None:
            raise EditError(proj.message)
        proposal.message = apply_proposal(proposal, proj.root, db, parser, ingestor)
        proposal.status = "applied"
    except EditError as exc:
        proposal.status, proposal.message = "failed", str(exc)
    except Exception as exc:
        proposal.status, proposal.message = "failed", f"{type(exc).__name__}: {exc}"


def on_discard(proposal: Proposal) -> None:
    proposal.status = "discarded"


def on_full_ingest() -> None:
    try:
        proj, db, ingestor = project_backend()
        if db is None:
            raise RuntimeError(proj.message)
        r = ingestor.ingest_path(st.session_state.project_path, root=proj.root)
        st.session_state.notice = (
            "success",
            f"Ingested {r.functions} functions from {r.files} file(s), {r.calls} call link(s)"
            + (f", {r.skipped} skipped." if r.skipped else "."),
        )
    except Exception as exc:
        st.session_state.notice = ("error", f"Ingestion failed: {type(exc).__name__}: {exc}")


def on_clear_graph() -> None:
    proj, db, _ = project_backend()
    if db is None:
        st.session_state.notice = ("error", proj.message)
    else:
        db.reset()
        st.session_state.notice = ("success", f"Graph memory cleared for project {proj.name}.")
    st.session_state.confirm_clear = False


# --- sidebar --------------------------------------------------------------------

with st.sidebar:
    st.header("🧠 Code Graph")
    st.text_input("Project path", key="project_path", help="Folder that was (or will be) ingested.")
    path_ok = Path(st.session_state.project_path).expanduser().exists()
    if not path_ok:
        st.warning("That path does not exist.")

    provider = st.selectbox("LLM provider", ["ollama", "gemini"],
                            index=["ollama", "gemini"].index(settings.llm_provider) if settings.llm_provider in ("ollama", "gemini") else 0)

    st.button("Run Full Ingestion", type="primary", disabled=not path_ok, on_click=on_full_ingest,
              use_container_width=True)

    if st.session_state.confirm_clear:
        st.warning("This deletes this project's graph data. Other projects are not touched.")
        left, right = st.columns(2)
        left.button("Yes, delete", type="primary", on_click=on_clear_graph, use_container_width=True)
        right.button("Cancel", on_click=lambda: st.session_state.update(confirm_clear=False),
                     use_container_width=True)
    else:
        st.button("Clear Graph Memory", on_click=lambda: st.session_state.update(confirm_clear=True),
                  use_container_width=True)

    if notice := st.session_state.pop("notice", None):
        getattr(st, notice[0])(notice[1])

    project, db, _ = project_backend()
    if db is None:
        st.warning(project.message)
        st.stop()
    st.caption(f"Project: {project.name} (id {project.project_id})")
    stats = db.stats()
    a, b, c = st.columns(3)
    a.metric("Files", stats["files"])
    b.metric("Functions", stats["functions"])
    c.metric("Calls", stats["calls"])

    st.divider()
    st.button("New chat", on_click=lambda: st.session_state.update(messages=[]), use_container_width=True)
    st.caption("Everything runs locally. Edits are only written after you press Apply.")


# --- chat rendering -------------------------------------------------------------

def render_proposals(msg_index: int, proposals: list[Proposal]) -> None:
    for j, proposal in enumerate(proposals):
        with st.container(border=True):
            st.markdown(f"**Proposed change** · `{proposal.file}` → `{proposal.function}()`")
            if proposal.error:
                st.error(proposal.error)
                continue
            st.code(proposal.diff or "(no difference)", language="diff")
            if proposal.warning:
                st.warning(proposal.warning)
            if proposal.status == "pending":
                apply_col, discard_col, _ = st.columns([1, 1, 3])
                apply_col.button("Apply to disk", key=f"apply_{msg_index}_{j}", type="primary",
                                 on_click=on_apply, args=(proposal,))
                discard_col.button("Discard", key=f"discard_{msg_index}_{j}",
                                   on_click=on_discard, args=(proposal,))
            elif proposal.status == "applied":
                st.success(proposal.message)
            elif proposal.status == "failed":
                st.error(proposal.message)
            else:
                st.caption("Discarded.")


def render_message(index: int, message: dict) -> None:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("context"):
            st.caption("Graph context: " + ", ".join(f"`{name}`" for name in message["context"]))
        render_proposals(index, message.get("proposals", []))


st.title("Code Graph Assistant")
if not st.session_state.messages:
    st.caption("Ask how your code works, or ask for a change. Proposed edits appear as diffs you can apply.")

for i, message in enumerate(st.session_state.messages):
    render_message(i, message)

if prompt := st.chat_input("Ask about your code, or request a change..."):
    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages[-6:]]
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        try:
            agent = CodeAgent(db, get_llm(provider))
            functions = agent.retrieve(prompt, history)
            if not functions:
                answer, proposals, context = "The graph is empty. Use **Run Full Ingestion** in the sidebar first.", [], []
                st.markdown(answer)
            else:
                context = [f["name"] for f in functions]
                st.caption("Graph context: " + ", ".join(f"`{name}`" for name in context))
                answer = st.write_stream(agent.stream_answer(prompt, functions, history, allow_edits=True))
                proposals = [resolve_proposal(db, p) for p in parse_proposals(answer)]
        except Exception as exc:
            hint = " Is Ollama running?" if "Connection" in type(exc).__name__ else ""
            answer, proposals, context = f"⚠️ {type(exc).__name__}: {exc}.{hint}", [], []
            st.error(answer)

    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "context": context, "proposals": proposals}
    )
    st.rerun()  # redraw from history so the diff/apply buttons appear
