"""
History viewer page — shows past Ask Mode chat sessions and Planning Mode plans.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

import streamlit as st

from src.ui.helpers import api_get


def render():
    st.title("📜 History")
    st.markdown(
        "Browse past **Ask Mode** conversations and **Planning Mode** plans. "
        "Click any entry to expand the full content."
    )

    tab_ask, tab_plan = st.tabs(["💬 Ask Sessions", "🧩 Plan History"])

    with tab_ask:
        _render_ask_sessions()

    with tab_plan:
        _render_plan_history()


# ── Ask Sessions tab ──────────────────────────────────────────────────────────


def _render_ask_sessions():
    st.subheader("Ask Mode — Chat Sessions")

    # Filters
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

    col_repo, col_limit = st.columns([3, 1])
    with col_repo:
        repo_label = st.selectbox("Filter by repository", repo_options, key="hist_ask_repo")
    with col_limit:
        limit = st.slider("Max sessions", 5, 100, 20, key="hist_ask_limit")

    repo_owner = repo_name = None
    if repo_label != "All repos":
        repo_owner, repo_name = repo_map.get(repo_label, (None, None))

    # Fetch sessions
    params = f"?limit={limit}"
    if repo_owner:
        params += f"&repo_owner={repo_owner}"
    if repo_name:
        params += f"&repo_name={repo_name}"

    sessions, err = api_get(f"/history/ask{params}", timeout=15)

    if err:
        st.error(f"Could not load sessions: {err}")
        return
    if not sessions:
        st.info("No chat sessions found yet. Ask a question in **Ask Mode** to get started.")
        return

    st.caption(f"Showing **{len(sessions)}** session(s)")

    for sess in sessions:
        session_id = sess.get("session_id", "")
        title = sess.get("title", "Untitled")
        turn_count = sess.get("turn_count", 0)
        last_active = sess.get("last_active_at", "")
        repo_scope = ""
        if sess.get("repo_owner") and sess.get("repo_name"):
            repo_scope = f"  ·  `{sess['repo_owner']}/{sess['repo_name']}`"

        label = (
            f"**{title[:80]}{'…' if len(title) > 80 else ''}** — {turn_count} turn(s){repo_scope}"
        )
        with st.expander(label):
            # Show timestamp
            if last_active:
                st.caption(f"Last active: `{last_active[:19].replace('T', ' ')}`")

            # Fetch full session lazily
            detail, err2 = api_get(f"/history/ask/{session_id}", timeout=15)
            if err2:
                st.warning(f"Could not load session: {err2}")
                continue
            if not detail:
                st.info("Empty session.")
                continue

            turns = detail.get("turns", [])
            for turn in turns:
                with st.chat_message("user"):
                    st.markdown(turn.get("user_query", ""))
                with st.chat_message("assistant"):
                    st.markdown(turn.get("answer", ""))
                    cited = turn.get("cited_files", [])
                    if cited:
                        st.caption("📁 " + "  ·  ".join(f"`{f}`" for f in cited))
                    hints = turn.get("follow_up_hints", [])
                    if hints:
                        st.caption("💡 Follow-ups: " + " · ".join(hints))
                    elapsed = turn.get("elapsed_ms")
                    tokens = turn.get("context_tokens")
                    if elapsed or tokens:
                        st.caption(
                            f"⏱ {round((elapsed or 0) / 1000, 1)}s"
                            + (f" · {tokens:,} tokens" if tokens else "")
                        )

            st.divider()
            # Resume button
            if st.button("▶ Resume in Ask Mode", key=f"resume_{session_id}"):
                # Rebuild messages list for Ask Mode session state
                rebuilt = []
                for t in turns:
                    rebuilt.append({"role": "user", "content": t.get("user_query", "")})
                    rebuilt.append(
                        {
                            "role": "assistant",
                            "content": t.get("answer", ""),
                            "meta": {
                                "cited_files": t.get("cited_files", []),
                                "follow_up_hints": t.get("follow_up_hints", []),
                                "elapsed_ms": round((t.get("elapsed_ms") or 0) / 1000, 1),
                                "context_tokens": t.get("context_tokens") or 0,
                                "context_files": t.get("context_files") or 0,
                            },
                        }
                    )
                st.session_state.ask_messages = rebuilt
                st.session_state.ask_session_id = session_id
                st.info("Session loaded. Navigate to **💬 Ask Mode** to continue.")


# ── Plan History tab ──────────────────────────────────────────────────────────


def _render_plan_history():
    st.subheader("Planning Mode — Generated Plans")

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

    col_repo, col_type, col_limit = st.columns([3, 2, 1])
    with col_repo:
        repo_label = st.selectbox("Filter by repository", repo_options, key="hist_plan_repo")
    with col_type:
        type_filter = st.selectbox(
            "Response type",
            ["All types", "plan", "answer", "analysis"],
            key="hist_plan_type",
        )
    with col_limit:
        limit = st.slider("Max entries", 5, 100, 20, key="hist_plan_limit")

    repo_owner = repo_name = None
    if repo_label != "All repos":
        repo_owner, repo_name = repo_map.get(repo_label, (None, None))

    params = f"?limit={limit}"
    if repo_owner:
        params += f"&repo_owner={repo_owner}"
    if repo_name:
        params += f"&repo_name={repo_name}"
    if type_filter != "All types":
        params += f"&response_type={type_filter}"

    entries, err = api_get(f"/history/plan{params}", timeout=15)

    if err:
        st.error(f"Could not load plan history: {err}")
        return
    if not entries:
        st.info("No plans found yet. Submit a query in **Planning Mode** to get started.")
        return

    st.caption(f"Showing **{len(entries)}** plan(s)")

    _TYPE_BADGE = {"plan": "🧩", "answer": "💬", "analysis": "🔍"}

    for entry in entries:
        plan_id = entry.get("plan_id", "")
        query = entry.get("query", "Untitled")
        rtype = entry.get("response_type", "plan")
        elapsed = entry.get("elapsed_ms")
        created_at = entry.get("created_at", "")
        badge = _TYPE_BADGE.get(rtype, "📄")
        repo_scope = ""
        if entry.get("repo_owner") and entry.get("repo_name"):
            repo_scope = f"  ·  `{entry['repo_owner']}/{entry['repo_name']}`"

        elapsed_str = f"  ·  ⏱ {round((elapsed or 0) / 1000, 1)}s" if elapsed else ""
        created_str = f"  ·  `{created_at[:19].replace('T', ' ')}`" if created_at else ""
        label = f"{badge} **{query[:80]}{'…' if len(query) > 80 else ''}**{repo_scope}{elapsed_str}{created_str}"

        with st.expander(label):
            detail, err2 = api_get(f"/history/plan/{plan_id}", timeout=15)
            if err2:
                st.warning(f"Could not load plan: {err2}")
                continue
            if not detail:
                st.info("Empty entry.")
                continue

            plan_data = detail.get("plan", {})
            elapsed_full = detail.get("elapsed_ms")

            # Re-use planning page renderers
            try:
                from src.ui._pages.planning import _render_analysis, _render_answer, _render_plan

                response_type = plan_data.get("response_type", rtype)
                elapsed_sec = round((elapsed_full or 0) / 1000, 1)
                if response_type == "answer":
                    _render_answer(plan_data, elapsed_sec)
                elif response_type == "analysis":
                    _render_analysis(plan_data, elapsed_sec)
                else:
                    _render_plan(plan_data, elapsed_sec)
            except Exception as exc:
                st.warning(f"Could not render plan: {exc}")
                st.json(plan_data)
