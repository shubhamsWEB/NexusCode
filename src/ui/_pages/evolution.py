"""
🧬 Evolution — Self-Evolution Dashboard Page.

Shows three tabs:
  Worldview   — LLM-generated semantic understanding of each repo (versioned)
  Evolution   — Reflection cycle history and what changed
  Performance — Quality/latency trends and user feedback
"""

from __future__ import annotations

import requests
import streamlit as st


def _api(path: str, method: str = "GET", json: dict | None = None) -> dict | list | None:
    """Make a request to the NexusCode API."""
    url = st.session_state.get("api_url", "http://localhost:8000").rstrip("/")
    headers = {}
    key = st.session_state.get("api_key", "")
    if key:
        headers["X-Api-Key"] = key
    try:
        if method == "POST":
            r = requests.post(f"{url}{path}", json=json or {}, headers=headers, timeout=30)
        else:
            r = requests.get(f"{url}{path}", headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"API error: {exc}")
        return None


def _repo_options() -> list[str]:
    """Fetch available repos from /stats/repos."""
    data = _api("/stats/repos")
    if not data:
        return []
    return [f"{r['repo_owner']}/{r['repo_name']}" for r in data]


def render():
    st.title("🧬 Self-Evolution")
    st.caption(
        "NexusCode learns from every interaction — building semantic worldviews, "
        "discovering usage patterns, and tuning its own retrieval parameters."
    )

    repos = _repo_options()
    if not repos:
        st.warning("No indexed repositories found. Index a repo first.")
        return

    selected = st.selectbox("Repository", repos, key="evolution_repo")
    if not selected or "/" not in selected:
        return
    owner, name = selected.split("/", 1)

    tab_wv, tab_evo, tab_perf, tab_log = st.tabs(
        ["🌐 Worldview", "🔁 Evolution Log", "📊 Performance", "📋 Interaction Log"]
    )

    # ── Worldview Tab ─────────────────────────────────────────────────────────
    with tab_wv:
        st.subheader("Semantic Worldview")
        st.caption("LLM-generated understanding of this codebase. Injected into every Ask and Plan prompt.")

        versions = _api(f"/evolution/worldview/{owner}/{name}/versions") or []

        col1, col2 = st.columns([3, 1])
        with col2:
            if st.button("🔄 Generate New Worldview", use_container_width=True):
                with st.spinner("Generating worldview…"):
                    result = _api(f"/evolution/worldview/{owner}/{name}/regenerate", method="POST")
                    if result:
                        st.success(f"Worldview v{result.get('version', '?')} generated. Refresh to view.")
                        st.rerun()
            if st.button("▶ Run Full Cycle", use_container_width=True, help="Analyze interactions and apply improvements"):
                with st.spinner("Triggering reflection cycle…"):
                    result = _api(f"/evolution/cycle/{owner}/{name}", method="POST", json={"force": True})
                    if result:
                        st.success("Reflection cycle started. Refresh in ~30 seconds.")

        with col1:
            if versions:
                version_labels = [f"v{v['version']} — {v['generated_at'][:10] if v['generated_at'] else 'unknown'}" for v in versions]
                selected_version_label = st.selectbox("Version", version_labels, key="wv_version")
                selected_version_idx = version_labels.index(selected_version_label)
                selected_version_num = versions[selected_version_idx]["version"]
            else:
                st.info("No worldview yet. Click 'Generate New Worldview' to create one.")
                selected_version_num = None

        if selected_version_num is not None:
            wv = _api(f"/evolution/worldview/{owner}/{name}?version={selected_version_num}")
            if wv:
                # Architecture summary
                st.markdown("#### Architecture")
                st.markdown(wv.get("architecture_summary") or "_Not available._")

                col_a, col_b = st.columns(2)
                with col_a:
                    patterns = wv.get("key_patterns") or []
                    if patterns:
                        st.markdown("#### Key Patterns")
                        for p in patterns:
                            st.markdown(f"- {p}")

                with col_b:
                    zones = wv.get("difficult_zones") or []
                    if zones:
                        st.markdown("#### Difficult Zones")
                        for z in zones:
                            st.markdown(f"- ⚠️ {z}")

                conventions = wv.get("conventions") or []
                if conventions:
                    st.markdown("#### Conventions")
                    for c in conventions:
                        st.markdown(f"- {c}")

                if wv.get("recent_changes"):
                    st.markdown("#### Recent Changes")
                    st.markdown(wv["recent_changes"])

                with st.expander("Full Worldview Document"):
                    st.markdown(wv.get("full_worldview") or "_Empty._")

                meta = wv.get("metadata") or {}
                st.caption(
                    f"v{wv.get('version')} · "
                    f"Model: {meta.get('model_used', 'unknown')} · "
                    f"Chunks sampled: {meta.get('chunks_sampled', 0)} · "
                    f"Generated: {meta.get('generated_at', 'unknown')}"
                )

    # ── Evolution Log Tab ─────────────────────────────────────────────────────
    with tab_evo:
        st.subheader("Reflection Cycle History")
        st.caption(
            "Each cycle analyzes recent interactions, proposes improvements, "
            "and autonomously applies parameter + prompt changes."
        )

        log = _api(f"/evolution/log/{owner}/{name}?limit=20") or []

        if not log:
            st.info("No reflection cycles yet. Trigger one from the Worldview tab.")
        else:
            for entry in log:
                status_icon = {
                    "complete": "✅",
                    "analyzing": "⏳",
                    "failed": "❌",
                    "pending": "🕐",
                }.get(entry.get("status", ""), "❓")

                with st.expander(
                    f"{status_icon} Cycle #{entry['cycle_number']} "
                    f"— {(entry.get('completed_at') or entry.get('started_at') or '')[:16] or 'unknown'}"
                ):
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        st.metric("Interactions Analyzed", entry.get("metrics_analyzed") or 0)
                    with col2:
                        st.metric("Improvements Proposed", entry.get("improvements_proposed") or 0)
                    with col3:
                        st.metric("Improvements Applied", entry.get("improvements_applied") or 0)

                    if entry.get("parameter_changes"):
                        st.markdown("**Parameter Changes:**")
                        for change in (entry["parameter_changes"] if isinstance(entry["parameter_changes"], list) else [entry["parameter_changes"]]):
                            if isinstance(change, dict):
                                st.markdown(
                                    f"- `{change.get('param', '?')}`: "
                                    f"{change.get('old')} → {change.get('new')} "
                                    f"_{change.get('reason', '')}_"
                                )

                    if entry.get("discovered_patterns"):
                        st.markdown("**Discovered Patterns:**")
                        for p in entry["discovered_patterns"]:
                            st.markdown(f"- {p}")

                    if entry.get("new_worldview_version"):
                        st.success(f"New worldview generated: v{entry['new_worldview_version']}")

                    if entry.get("error"):
                        st.error(f"Error: {entry['error']}")

        # A/B Experiments
        st.divider()
        st.subheader("A/B Experiments")
        experiments = _api(f"/evolution/experiments/{owner}/{name}") or []

        if not experiments:
            st.caption("No A/B experiments recorded yet.")
        else:
            for exp in experiments:
                status_color = {"active": "🟡", "completed": "🟢", "rolled_back": "🔴"}.get(
                    exp.get("status", ""), "⚪"
                )
                with st.expander(
                    f"{status_color} {exp['name']} — `{exp['parameter']}`"
                ):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown(f"**Control:** `{exp['control']}`")
                        if exp.get("quality", {}).get("control_avg") is not None:
                            st.metric("Control Quality", round(exp["quality"]["control_avg"], 3))
                        st.metric("Control Samples", exp.get("samples", {}).get("control", 0))
                    with col2:
                        st.markdown(f"**Treatment:** `{exp['treatment']}`")
                        if exp.get("quality", {}).get("treatment_avg") is not None:
                            st.metric("Treatment Quality", round(exp["quality"]["treatment_avg"], 3))
                        st.metric("Treatment Samples", exp.get("samples", {}).get("treatment", 0))

                    if exp.get("winner"):
                        st.success(f"Winner: **{exp['winner']}** (confidence: {exp.get('confidence', 0):.1%})")

    # ── Performance Tab ───────────────────────────────────────────────────────
    with tab_perf:
        st.subheader("Performance Metrics")

        days = st.slider("Look-back window (days)", 1, 90, 14, key="perf_days")
        metrics = _api(f"/evolution/metrics/{owner}/{name}?days={days}")
        if not metrics:
            st.info("No performance data yet for this repository.")
        else:
            quality = metrics.get("quality") or {}
            latency = metrics.get("latency") or {}
            agent = metrics.get("agent") or {}
            feedback = metrics.get("feedback") or {}

            # Top KPIs
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Interactions", metrics.get("total_interactions", 0))
            with col2:
                mq = quality.get("mean")
                st.metric(
                    "Mean Quality",
                    f"{mq:.3f}" if mq is not None else "N/A",
                    delta=f"Low quality: {quality.get('low_quality_ratio', 0):.0%}",
                    delta_color="inverse",
                )
            with col3:
                st.metric(
                    "P95 Latency",
                    f"{latency.get('p95_ms', 0):.0f}ms" if latency.get("p95_ms") else "N/A",
                )
            with col4:
                st.metric(
                    "Mean Iterations",
                    f"{agent.get('mean_iterations', 0):.1f}" if agent.get("mean_iterations") else "N/A",
                )

            # By complexity
            by_complexity = metrics.get("by_complexity") or {}
            if by_complexity:
                st.markdown("#### By Query Complexity")
                cols = st.columns(len(by_complexity))
                for i, (complexity, data) in enumerate(by_complexity.items()):
                    with cols[i]:
                        st.metric(
                            f"{complexity.title()} queries",
                            f"n={data.get('count', 0)}",
                            delta=f"quality={data.get('mean_quality') or 'N/A'}",
                            delta_color="normal",
                        )

            # User feedback
            st.markdown("#### User Feedback")
            if feedback.get("rated_interactions", 0) > 0:
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Rated Interactions", feedback["rated_interactions"])
                with col2:
                    mr = feedback.get("mean_rating")
                    st.metric("Mean Rating", f"⭐ {mr:.1f}/5" if mr else "N/A")
                with col3:
                    sr = feedback.get("satisfaction_rate")
                    st.metric("Satisfaction Rate", f"{sr:.0%}" if sr else "N/A")
            else:
                st.caption("No user feedback collected yet. Use `POST /evolution/feedback` to rate responses.")

    # ── Interaction Log Tab ────────────────────────────────────────────────────
    with tab_log:
        st.subheader("Interaction Log")
        st.caption(
            "Every Ask and Plan query recorded by the evolution system — "
            "quality scores, latency, iteration counts, and retrieval parameters."
        )

        # ── Filters ──────────────────────────────────────────────────────────
        f_col1, f_col2, f_col3, f_col4 = st.columns(4)
        with f_col1:
            log_days = st.selectbox("Time window", [1, 7, 14, 30, 90], index=2, key="log_days")
        with f_col2:
            log_type = st.selectbox("Type", ["all", "ask", "plan"], key="log_type")
        with f_col3:
            log_complexity = st.selectbox(
                "Complexity", ["all", "simple", "moderate", "complex"], key="log_complexity"
            )
        with f_col4:
            log_min_q = st.number_input(
                "Min quality", min_value=0.0, max_value=1.0, value=0.0,
                step=0.05, format="%.2f", key="log_min_q"
            )

        page_size = 25
        page_num = st.number_input("Page", min_value=1, value=1, step=1, key="log_page")
        offset = (page_num - 1) * page_size

        # Build query params
        params = f"?limit={page_size}&offset={offset}&days={log_days}"
        if log_type != "all":
            params += f"&interaction_type={log_type}"
        if log_complexity != "all":
            params += f"&complexity={log_complexity}"
        if log_min_q > 0.0:
            params += f"&min_quality={log_min_q}"

        data = _api(f"/evolution/interactions/{owner}/{name}{params}")
        if data is None:
            st.error("Could not load interaction log — check API connection.")
        else:
            total = data.get("total", 0)
            interactions = data.get("interactions", [])

            st.caption(
                f"Showing {offset + 1}–{min(offset + page_size, total)} of **{total}** interactions"
            )

            if not interactions:
                st.info("No interactions found for the selected filters.")
            else:
                # Summary bar
                if interactions:
                    scores = [i["quality_score"] for i in interactions if i["quality_score"] is not None]
                    latencies = [i["elapsed_ms"] for i in interactions if i["elapsed_ms"] is not None]
                    sm_col1, sm_col2, sm_col3, sm_col4 = st.columns(4)
                    with sm_col1:
                        st.metric("On this page", len(interactions))
                    with sm_col2:
                        st.metric(
                            "Avg quality",
                            f"{sum(scores)/len(scores):.3f}" if scores else "N/A",
                        )
                    with sm_col3:
                        st.metric(
                            "Median latency",
                            f"{sorted(latencies)[len(latencies)//2]:.0f}ms" if latencies else "N/A",
                        )
                    with sm_col4:
                        low_q = sum(1 for s in scores if s < 0.5)
                        st.metric(
                            "Low quality (<0.5)",
                            low_q,
                            delta=f"{low_q/len(scores):.0%}" if scores else "0%",
                            delta_color="inverse",
                        )

                st.divider()

                # Individual rows
                for row in interactions:
                    quality = row["quality_score"]
                    if quality is None:
                        q_icon = "⚪"
                    elif quality >= 0.75:
                        q_icon = "🟢"
                    elif quality >= 0.5:
                        q_icon = "🟡"
                    else:
                        q_icon = "🔴"

                    type_badge = "💬 Ask" if row["type"] == "ask" else "📐 Plan"
                    complexity_badge = {
                        "simple": "◽ simple",
                        "moderate": "◾ moderate",
                        "complex": "⬛ complex",
                    }.get(row.get("complexity") or "", "")

                    ts = (row["created_at"] or "")[:16].replace("T", " ")
                    rating_str = f" ⭐{row['user_rating']}" if row.get("user_rating") else ""

                    header = (
                        f"{q_icon} `{ts}` · {type_badge} · {complexity_badge} · "
                        f"quality={quality if quality is not None else 'N/A'} · "
                        f"{row.get('elapsed_ms', '?')}ms{rating_str}"
                    )

                    with st.expander(header):
                        st.markdown(f"**Query:** {row['query']}")

                        d_col1, d_col2, d_col3, d_col4 = st.columns(4)
                        with d_col1:
                            st.metric("Iterations", row.get("iterations") or "—")
                        with d_col2:
                            st.metric("Tool calls", row.get("tool_calls") or "—")
                        with d_col3:
                            st.metric("Context tokens", row.get("context_tokens") or "—")
                        with d_col4:
                            st.metric("Answer tokens", row.get("answer_tokens") or "—")

                        # ── Response accordion ───────────────────────────────
                        response_text = row.get("response")
                        if response_text:
                            resp_label = "💬 Answer" if row["type"] == "ask" else f"📐 Plan ({row.get('plan_response_type') or 'plan'})"
                            with st.expander(resp_label, expanded=False):
                                st.markdown(response_text)
                                cited = row.get("cited_files") or []
                                if cited:
                                    st.markdown("**Cited files:**")
                                    for f in cited:
                                        st.caption(f"  `{f}`")
                        else:
                            st.caption("_Response not available (recorded before response linkage was added)._")

                        params_used = row.get("params") or {}
                        if any(v is not None for v in params_used.values()):
                            with st.expander("⚙️ Retrieval params at call time", expanded=False):
                                p_col1, p_col2, p_col3 = st.columns(3)
                                with p_col1:
                                    st.caption(f"strategy: `{params_used.get('strategy', '—')}`")
                                    st.caption(f"hnsw_ef: `{params_used.get('hnsw_ef', '—')}`")
                                with p_col2:
                                    st.caption(f"rrf_k: `{params_used.get('rrf_k', '—')}`")
                                    st.caption(f"reranker_top_n: `{params_used.get('reranker_top_n', '—')}`")
                                with p_col3:
                                    st.caption(f"rel_threshold: `{params_used.get('rel_threshold', '—')}`")
                                    st.caption(f"max_iter: `{params_used.get('max_iter', '—')}`")

                        if row.get("session_id"):
                            st.caption(f"Session: `{row['session_id']}`  ·  ID: `{row['id']}`")
                        else:
                            st.caption(f"Interaction ID: `{row['id']}`")

                        # Inline feedback form
                        if not row.get("user_rating"):
                            with st.form(f"feedback_{row['id']}"):
                                fb_rating = st.slider("Rate this response", 1, 5, 3)
                                fb_text = st.text_input("Optional feedback")
                                if st.form_submit_button("Submit Rating"):
                                    result = _api(
                                        "/evolution/feedback",
                                        method="POST",
                                        json={
                                            "interaction_id": row["id"],
                                            "rating": fb_rating,
                                            "feedback_text": fb_text or None,
                                        },
                                    )
                                    if result:
                                        st.success("Rating saved!")
                                    else:
                                        st.error("Failed to save rating.")
