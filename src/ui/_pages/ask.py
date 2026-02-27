"""
Ask Mode dashboard page.

Chat interface for asking natural-language questions about the codebase.
Answers are mentor-style: conversational, grounded in real code, with
inline citations and follow-up suggestions.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

# isort: split

import httpx
import streamlit as st

from src.ui.helpers import api_get


# ── Session state helpers ──────────────────────────────────────────────────────


def _init_state():
    if "ask_messages" not in st.session_state:
        st.session_state.ask_messages = []  # list of {role, content, meta}
    if "ask_pending_hint" not in st.session_state:
        st.session_state.ask_pending_hint = None
    if "ask_session_id" not in st.session_state:
        st.session_state.ask_session_id = None


# ── Main render ────────────────────────────────────────────────────────────────


def render():
    _init_state()

    st.title("💬 Ask Mode")
    st.markdown(
        "Ask any question about your codebase. Get a grounded, cited answer "
        "from a senior-engineer perspective — not a generic plan."
    )

    st.info(
        "**Requires:** At least one LLM API key in `.env` (ANTHROPIC_API_KEY, "
        "OPENAI_API_KEY, or GROK_API_KEY). "
        "Answers are cited to real files and line numbers in your codebase index.",
        icon="ℹ️",
    )

    # ── Repo selector ──────────────────────────────────────────────────────────
    repos_data, _ = api_get("/repos", timeout=10)
    repo_options = ["All repos"]
    repo_map: dict[str, tuple[str, str]] = {}

    if repos_data:
        for repo in repos_data:
            owner = repo.get("owner", "")
            name = repo.get("name", "")
            if owner and name:
                label = f"{owner}/{name}"
                repo_options.append(label)
                repo_map[label] = (owner, name)

    # ── Model selector ────────────────────────────────────────────────────────
    models_data, _ = api_get("/models", timeout=5)
    model_options = ["Default"]
    model_map: dict[str, str] = {}
    if models_data:
        for m in models_data:
            label = f"{m['model']} ({m['provider']})"
            model_options.append(label)
            model_map[label] = m["model"]

    col_repo, col_model, col_clear = st.columns([3, 2, 1])
    with col_repo:
        repo_label = st.selectbox(
            "Scope to repository (optional)",
            options=repo_options,
            key="ask_repo",
        )
    with col_model:
        model_label = st.selectbox(
            "LLM Model",
            options=model_options,
            key="ask_model",
        )
    with col_clear:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        if st.button("🗑 Clear chat", use_container_width=True):
            st.session_state.ask_messages = []
            st.session_state.ask_pending_hint = None
            st.session_state.ask_session_id = None
            st.rerun()

    st.divider()

    # ── Conversation history ───────────────────────────────────────────────────
    for msg in st.session_state.ask_messages:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            else:
                # Assistant message — render answer + cited files + hints
                st.markdown(msg["content"])
                _render_assistant_extras(msg.get("meta", {}))

    # ── Pending hint injection (clickable suggestion was tapped) ───────────────
    if st.session_state.ask_pending_hint:
        hint = st.session_state.ask_pending_hint
        st.session_state.ask_pending_hint = None
        _handle_query(hint, repo_label, repo_map, model_label, model_map)
        st.rerun()

    # ── Follow-up suggestion chips (shown below conversation) ─────────────────
    last_msg = st.session_state.ask_messages[-1] if st.session_state.ask_messages else None
    hints = (last_msg or {}).get("meta", {}).get("follow_up_hints", []) if last_msg and last_msg["role"] == "assistant" else []

    if hints:
        st.markdown("**Suggested follow-ups:**")
        cols = st.columns(len(hints))
        for i, hint in enumerate(hints):
            with cols[i]:
                if st.button(f"💡 {hint}", key=f"hint_{i}_{hint[:20]}", use_container_width=True):
                    st.session_state.ask_pending_hint = hint
                    st.rerun()

    # ── Chat input ─────────────────────────────────────────────────────────────
    user_input = st.chat_input("Ask anything about the codebase…")

    if user_input and user_input.strip():
        _handle_query(user_input.strip(), repo_label, repo_map, model_label, model_map)
        st.rerun()


# ── Query handler ──────────────────────────────────────────────────────────────


def _handle_query(query: str, repo_label: str, repo_map: dict, model_label: str = "Default", model_map: dict | None = None):
    """Send the query to /ask (streaming) and append messages to session state."""
    # Append user message immediately
    st.session_state.ask_messages.append({"role": "user", "content": query})

    payload: dict = {"query": query, "stream": True}
    if st.session_state.ask_session_id:
        payload["session_id"] = st.session_state.ask_session_id
    if model_label != "Default" and model_map:
        model_val = model_map.get(model_label)
        if model_val:
            payload["model"] = model_val
    if repo_label != "All repos":
        owner, name = repo_map.get(repo_label, (None, None))
        if owner and name:
            payload["repo_owner"] = owner
            payload["repo_name"] = name

    api_url = os.getenv("API_URL", "http://localhost:8000")

    answer, meta = _stream_ask(api_url, payload, query)

    st.session_state.ask_messages.append(
        {"role": "assistant", "content": answer, "meta": meta}
    )


# ── Streaming request ──────────────────────────────────────────────────────────


def _stream_ask(api_url: str, payload: dict, query: str) -> tuple[str, dict]:
    """
    POST /ask with stream=true.
    Returns (final_answer_markdown, metadata_dict).
    """
    with st.chat_message("assistant"):
        status_box = st.empty()
        answer_box = st.empty()
        meta_box = st.empty()

        accumulated = ""
        final_meta: dict = {}

        try:
            with (
                httpx.Client(timeout=180) as client,
                client.stream("POST", f"{api_url}/ask", json=payload) as resp,
            ):
                if resp.status_code != 200:
                    st.error(f"API error: HTTP {resp.status_code}")
                    return "_Request failed._", {}

                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")

                    if etype == "status":
                        status_box.caption(f"⏳ {event['message']}")

                    elif etype == "retrieval_complete":
                        chunks = event.get("chunks", 0)
                        tokens = event.get("tokens", 0)
                        status_box.caption(
                            f"✅ Found **{chunks}** relevant chunks · **{tokens}** tokens"
                        )

                    elif etype == "answer_chunk":
                        # answer_question tool streams partial JSON via input_json deltas
                        # Try to extract the "answer" value incrementally
                        text = event.get("text", "")
                        accumulated += text
                        # Try to render whatever answer we have so far
                        partial_answer = _extract_partial_answer(accumulated)
                        if partial_answer:
                            answer_box.markdown(partial_answer + " ▌")

                    elif etype == "answer_complete":
                        status_box.empty()
                        answer_text = event.get("answer", accumulated)
                        cited = event.get("cited_files", [])
                        hints = event.get("follow_up_hints", [])
                        elapsed = round(event.get("elapsed_ms", 0) / 1000, 1)
                        meta = event.get("metadata", {})
                        sid = event.get("session_id")
                        if sid:
                            st.session_state.ask_session_id = sid

                        # Clear streaming box, render final answer
                        answer_box.empty()
                        st.markdown(answer_text)

                        # Cited files
                        if cited:
                            st.divider()
                            st.caption(
                                "📁 **Citations:** "
                                + "  ·  ".join(f"`{f}`" for f in cited)
                            )

                        # Retrieval debug (collapsed)
                        if meta.get("retrieval_log"):
                            with st.expander("Retrieval log (debug)", expanded=False):
                                st.code(meta["retrieval_log"], language="text")

                        # Timing
                        meta_box.caption(
                            f"⏱ {elapsed}s · {meta.get('context_tokens', 0):,} tokens · "
                            f"{meta.get('context_files', 0)} chunks"
                        )

                        final_meta = {
                            "cited_files": cited,
                            "follow_up_hints": hints,
                            "elapsed_ms": elapsed,
                            **meta,
                        }
                        return answer_text, final_meta

                    elif etype == "error":
                        status_box.empty()
                        answer_box.empty()
                        msg = event.get("message", "Unknown error")
                        st.error(f"❌ {msg}")
                        if "rate limit" in str(msg).lower() or event.get("retry_after"):
                            st.info("⏳ Rate limit hit. Please wait 60 seconds and try again.")
                        elif "ANTHROPIC_API_KEY" in str(msg):
                            st.markdown(
                                "Make sure `ANTHROPIC_API_KEY` is set in `.env` "
                                "and the server has been restarted."
                            )
                        return "_Error getting answer._", {}

        except httpx.ConnectError:
            st.error("Cannot connect to the API server. Is it running on `localhost:8000`?")
        except Exception as exc:
            st.error(f"Streaming error: {exc}")

    return accumulated or "_No answer received._", final_meta


# ── Partial answer extractor ───────────────────────────────────────────────────


def _extract_partial_answer(raw: str) -> str:
    """
    Try to extract the 'answer' value from a partial JSON stream.
    The tool streams partial JSON like: {"answer": "Walking you through...
    We parse what we have and return whatever text is available.
    """
    # Look for the answer field value in partial JSON
    key = '"answer": "'
    idx = raw.find(key)
    if idx == -1:
        return ""
    start = idx + len(key)
    # Return everything after the key opening quote, unescape common sequences
    partial = raw[start:]
    # Stop at first unescaped quote if complete, otherwise return as-is
    result = []
    i = 0
    while i < len(partial):
        c = partial[i]
        if c == "\\" and i + 1 < len(partial):
            nc = partial[i + 1]
            if nc == "n":
                result.append("\n")
            elif nc == "t":
                result.append("\t")
            elif nc == '"':
                result.append('"')
            elif nc == "\\":
                result.append("\\")
            else:
                result.append(c)
                result.append(nc)
            i += 2
        elif c == '"':
            break  # end of answer string
        else:
            result.append(c)
            i += 1
    return "".join(result)


# ── Assistant extras (shown in history replay) ─────────────────────────────────


def _render_assistant_extras(meta: dict):
    """Render cited files and retrieval log from stored meta."""
    cited = meta.get("cited_files", [])
    if cited:
        st.divider()
        st.caption(
            "📁 **Citations:** " + "  ·  ".join(f"`{f}`" for f in cited)
        )

    if meta.get("retrieval_log"):
        with st.expander("Retrieval log (debug)", expanded=False):
            st.code(meta["retrieval_log"], language="text")

    elapsed = meta.get("elapsed_ms", 0)
    tokens = meta.get("context_tokens", 0)
    chunks = meta.get("context_files", 0)
    if elapsed or tokens:
        st.caption(f"⏱ {elapsed}s · {tokens:,} tokens · {chunks} chunks")
