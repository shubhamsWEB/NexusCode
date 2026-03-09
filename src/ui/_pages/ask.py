"""
Ask Mode dashboard page.

Chat interface for asking natural-language questions about the codebase.
Answers are mentor-style: conversational, grounded in real code, with
inline citations and follow-up suggestions.

The reasoning trace uses st.status() to show a persistent Cursor/Copilot-style
timeline of every tool call the agent made — search queries, symbol lookups,
external MCP calls, etc. — all stacked up and visible before the answer appears.

Session state keys
------------------
  ask_messages     — full chat history [{role, content, meta}]
  ask_agent_logs   — last run's execution timeline (list of step dicts)
  ask_pending_hint — next hint to inject as a user message
  ask_session_id   — server-side session UUID for multi-turn memory
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

# isort: split

import httpx
import streamlit as st

from src.ui.helpers import AGENT_DEFAULT_ICON, AGENT_TOOL_ICONS, api_get, render_agent_timeline_html


# ── Session state helpers ──────────────────────────────────────────────────────


def _init_state():
    defaults = {
        "ask_messages":     [],   # [{role, content, meta}]
        "ask_agent_logs":   [],   # execution timeline for the latest run
        "ask_pending_hint": None,
        "ask_session_id":   None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ── Main render ────────────────────────────────────────────────────────────────


def render():
    _init_state()

    st.title("💬 Ask Mode")
    st.markdown(
        "Ask any question about your codebase. Get a grounded, cited answer "
        "from a senior-engineer perspective — not a generic plan."
    )

    st.info(
        "**Requires:** `ANTHROPIC_API_KEY` in `.env`. "
        "Claude searches the codebase iteratively, then answers with inline citations.",
        icon="ℹ️",
    )

    # ── Repo selector ──────────────────────────────────────────────────────────
    repos_data, _ = api_get("/repos", timeout=10)
    repo_options = ["All repos"]
    repo_map: dict[str, tuple[str, str]] = {}

    if repos_data:
        for repo in repos_data:
            owner = repo.get("owner", "")
            name  = repo.get("name", "")
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

    key_is_set = bool(st.session_state.get("api_key", "").strip())

    col_repo, col_model, col_clear = st.columns([3, 2, 1])
    with col_repo:
        repo_label = st.selectbox(
            "Scope to repository (optional)",
            options=repo_options,
            key="ask_repo",
            disabled=key_is_set,
            help="Disabled — scope is determined by the API key." if key_is_set else None,
        )
        if key_is_set:
            st.caption("🔑 Repo scope set by API key")
    with col_model:
        model_label = st.selectbox(
            "LLM Model",
            options=model_options,
            key="ask_model",
        )
    with col_clear:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        if st.button("🗑 Clear chat", use_container_width=True):
            st.session_state.ask_messages   = []
            st.session_state.ask_agent_logs = []
            st.session_state.ask_pending_hint = None
            st.session_state.ask_session_id   = None
            st.rerun()

    st.divider()

    # ── Conversation history ───────────────────────────────────────────────────
    for msg in st.session_state.ask_messages:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            else:
                # Assistant: trace accordion → answer → footer
                _render_assistant_extras(msg.get("meta", {}))
                st.markdown(msg["content"])
                _render_assistant_footer(msg.get("meta", {}))

    # ── Pending hint injection ─────────────────────────────────────────────────
    if st.session_state.ask_pending_hint:
        hint = st.session_state.ask_pending_hint
        st.session_state.ask_pending_hint = None
        _handle_query(hint, repo_label, repo_map, model_label, model_map)
        st.rerun()

    # ── Follow-up suggestion chips ─────────────────────────────────────────────
    last_msg = st.session_state.ask_messages[-1] if st.session_state.ask_messages else None
    hints = (
        (last_msg or {}).get("meta", {}).get("follow_up_hints", [])
        if last_msg and last_msg["role"] == "assistant"
        else []
    )

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


def _handle_query(
    query: str,
    repo_label: str,
    repo_map: dict,
    model_label: str = "Default",
    model_map: dict | None = None,
):
    """Send the query to /ask (streaming) and append messages to session state."""
    # Reset execution timeline for this new query
    st.session_state.ask_agent_logs = []

    st.session_state.ask_messages.append({"role": "user", "content": query})

    payload: dict = {"query": query, "stream": True}
    if st.session_state.ask_session_id:
        payload["session_id"] = st.session_state.ask_session_id
    if model_label != "Default" and model_map:
        model_val = model_map.get(model_label)
        if model_val:
            payload["model"] = model_val
    api_key = st.session_state.get("api_key", "").strip()
    # Only pin repo when no API key is active (key scope overrides manual selection)
    if not api_key and repo_label != "All repos":
        owner, name = repo_map.get(repo_label, (None, None))
        if owner and name:
            payload["repo_owner"] = owner
            payload["repo_name"]  = name

    api_url = st.session_state.get("api_url", os.getenv("API_URL", "http://localhost:8000"))
    answer, meta = _stream_ask(api_url, payload, api_key=api_key)
    st.session_state.ask_messages.append({"role": "assistant", "content": answer, "meta": meta})


# ── Streaming request ──────────────────────────────────────────────────────────


def _stream_ask(api_url: str, payload: dict, api_key: str = "") -> tuple[str, dict]:
    """
    POST /ask with stream=true.

    Renders a Cursor/Copilot-style persistent reasoning trace inside
    st.status().  Every tool call, tool result, and thinking block is
    appended to st.session_state.ask_agent_logs and rendered as an HTML
    timeline that updates in-place.

    Layout:
      ┌─ 🧠 Thinking… (expanded during run) ──────────────────────────────┐
      │  ✓ 🔍 search_codebase  "JWT token validation"    1,234t            │
      │  ✓ 🎯 get_symbol       "JWTValidator"              892t            │
      │  💭 Reviewing the validator…                                       │
      └───────────────────────────────────────────────────────────────────┘
      [final answer rendered below, accordion collapses to "✅ 3 calls · 2.4s"]

    Returns (final_answer_markdown, metadata_dict).
    """
    with st.chat_message("assistant"):
        tool_steps: list[dict] = []

        # ── Reasoning trace container ─────────────────────────────────────────
        # We exit the `with` block immediately so answer_box renders BELOW the
        # status box, but trace_status + trace_placeholder stay writable.
        with st.status("🧠 Thinking…", expanded=True) as trace_status:
            trace_placeholder = st.empty()

        answer_box = st.empty()   # live partial-answer cursor
        meta_box   = st.empty()   # timing / token footer

        accumulated  = ""
        final_meta: dict = {}

        _headers = {"X-Api-Key": api_key} if api_key else {}
        try:
            with (
                httpx.Client(timeout=180) as client,
                client.stream("POST", f"{api_url}/ask", json=payload, headers=_headers) as resp,
            ):
                if resp.status_code != 200:
                    trace_status.update(label="❌ Request failed", state="error")
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

                    # ── Status label update ───────────────────────────────────
                    if etype == "status":
                        trace_status.update(
                            label=f"🧠 {event['message']}", state="running"
                        )

                    # ── Tool called ───────────────────────────────────────────
                    elif etype == "agent_tool_call":
                        tool    = event.get("tool", "")
                        summary = event.get("input_summary", "")
                        step = {"type": "tool_call", "tool": tool,
                                "summary": summary, "state": "running", "tokens": None}
                        tool_steps.append(step)
                        st.session_state.ask_agent_logs.append(step)
                        trace_placeholder.markdown(
                            render_agent_timeline_html(tool_steps),
                            unsafe_allow_html=True,
                        )
                        icon  = AGENT_TOOL_ICONS.get(tool, AGENT_DEFAULT_ICON)
                        short = summary[:50] + "…" if len(summary) > 50 else summary
                        trace_status.update(
                            label=f"{icon} {tool}: {short}" if short else f"{icon} {tool}",
                            state="running",
                        )

                    # ── Tool returned ─────────────────────────────────────────
                    elif etype == "agent_tool_result":
                        tool   = event.get("tool", "")
                        tokens = event.get("tokens", 0)
                        for step in tool_steps:
                            if step.get("tool") == tool and step.get("state") == "running":
                                step["state"]  = "done"
                                step["tokens"] = tokens
                                break
                        trace_placeholder.markdown(
                            render_agent_timeline_html(tool_steps),
                            unsafe_allow_html=True,
                        )
                        running = sum(1 for s in tool_steps if s.get("state") == "running")
                        done_n  = sum(1 for s in tool_steps if s.get("state") == "done")
                        if running:
                            trace_status.update(
                                label=f"🧠 {running} tool{'s' if running > 1 else ''} in progress…",
                                state="running",
                            )
                        else:
                            trace_status.update(
                                label=f"🧠 {done_n} tool{'s' if done_n > 1 else ''} done · composing answer…",
                                state="running",
                            )

                    # ── Extended thinking ─────────────────────────────────────
                    elif etype == "thinking":
                        text = event.get("text", "")
                        if text:
                            # Thinking arrives as streaming chunks — accumulate into
                            # a single row instead of adding one row per packet.
                            if tool_steps and tool_steps[-1].get("type") == "thinking":
                                raw = tool_steps[-1].get("_raw", "") + text
                                tool_steps[-1]["_raw"] = raw
                                preview = raw[:120] + ("…" if len(raw) > 120 else "")
                                tool_steps[-1]["summary"] = preview
                            else:
                                step = {
                                    "type": "thinking", "tool": "_thinking",
                                    "summary": text[:120] + ("…" if len(text) > 120 else ""),
                                    "_raw": text,
                                    "state": "done", "tokens": None,
                                }
                                tool_steps.append(step)
                                st.session_state.ask_agent_logs.append(step)
                            trace_placeholder.markdown(
                                render_agent_timeline_html(tool_steps),
                                unsafe_allow_html=True,
                            )

                    # ── Answer streaming (partial JSON) ───────────────────────
                    elif etype == "answer_chunk":
                        accumulated += event.get("text", "")
                        partial = _extract_partial_answer(accumulated)
                        if partial:
                            answer_box.markdown(partial + " ▌")

                    # ── Answer complete ───────────────────────────────────────
                    elif etype == "answer_complete":
                        elapsed = round(event.get("elapsed_ms", 0) / 1000, 1)
                        cited   = event.get("cited_files", [])
                        hints   = event.get("follow_up_hints", [])
                        meta    = event.get("metadata", {})
                        sid     = event.get("session_id")
                        if sid:
                            st.session_state.ask_session_id = sid

                        # Finalize any still-running steps
                        for step in tool_steps:
                            if step.get("state") == "running":
                                step["state"] = "done"

                        # Persist final timeline to session state
                        st.session_state.ask_agent_logs = list(tool_steps)

                        trace_placeholder.markdown(
                            render_agent_timeline_html(tool_steps),
                            unsafe_allow_html=True,
                        )

                        # Collapse the trace with a clean summary
                        n_calls = sum(
                            1 for s in tool_steps
                            if s.get("type") == "tool_call" or (
                                s.get("tool", "").strip("_") and s.get("tool") != "_thinking"
                            )
                        )
                        label_parts = [f"✅ {n_calls} tool call{'s' if n_calls != 1 else ''}"]
                        if elapsed:
                            label_parts.append(f"{elapsed}s")
                        trace_status.update(
                            label=" · ".join(label_parts),
                            state="complete",
                            expanded=False,
                        )

                        # Final answer rendered prominently below collapsed trace
                        answer_text = event.get("answer", accumulated)
                        answer_box.empty()
                        st.markdown(answer_text)

                        if cited:
                            st.divider()
                            st.caption(
                                "📁 **Citations:** " + "  ·  ".join(f"`{f}`" for f in cited)
                            )

                        if meta.get("retrieval_log"):
                            with st.expander("Retrieval log (debug)", expanded=False):
                                st.code(meta["retrieval_log"], language="text")

                        meta_box.caption(
                            f"⏱ {elapsed}s · {meta.get('context_tokens', 0):,} tokens · "
                            f"{meta.get('context_files', 0)} tool calls"
                        )

                        final_meta = {
                            "cited_files":    cited,
                            "follow_up_hints": hints,
                            "elapsed_ms":     elapsed,
                            "tool_trace":     tool_steps,
                            **meta,
                        }
                        return answer_text, final_meta

                    elif etype == "error":
                        trace_status.update(label="❌ Error", state="error", expanded=True)
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
    key = '"answer": "'
    idx = raw.find(key)
    if idx == -1:
        return ""
    start  = idx + len(key)
    partial = raw[start:]
    result: list[str] = []
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
            break
        else:
            result.append(c)
            i += 1
    return "".join(result)


# ── Assistant message history rendering ────────────────────────────────────────


def _render_assistant_extras(meta: dict) -> None:
    """
    Rendered ABOVE the answer text (history replay).

    Shows a collapsed execution timeline accordion so users can expand it to
    see every tool call made — mirrors how Cursor shows "X tool calls" in history.
    """
    tool_trace = meta.get("tool_trace", [])
    if not tool_trace:
        return

    n_calls  = sum(1 for s in tool_trace if not s.get("tool", "").startswith("_"))
    elapsed  = meta.get("elapsed_ms", 0)
    elapsed_str = f" · {elapsed}s" if elapsed else ""
    label = f"🧠 {n_calls} tool call{'s' if n_calls != 1 else ''}{elapsed_str}"

    with st.expander(label, expanded=False):
        st.markdown(render_agent_timeline_html(tool_trace), unsafe_allow_html=True)


def _render_assistant_footer(meta: dict) -> None:
    """
    Rendered BELOW the answer text (history replay).
    Shows citations, retrieval debug log, and timing.
    """
    cited = meta.get("cited_files", [])
    if cited:
        st.divider()
        st.caption("📁 **Citations:** " + "  ·  ".join(f"`{f}`" for f in cited))

    if meta.get("retrieval_log"):
        with st.expander("Retrieval log (debug)", expanded=False):
            st.code(meta["retrieval_log"], language="text")

    elapsed = meta.get("elapsed_ms", 0)
    tokens  = meta.get("context_tokens", 0)
    chunks  = meta.get("context_files", 0)
    if elapsed or tokens:
        st.caption(f"⏱ {elapsed}s · {tokens:,} tokens · {chunks} tool calls")
