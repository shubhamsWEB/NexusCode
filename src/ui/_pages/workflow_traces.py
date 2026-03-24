"""
Workflow Run Trace Viewer — Streamlit page.

Shows:
  - Run list with status, duration, token cost
  - Step-by-step execution timeline
  - Per-step token cost breakdown (bar chart)
  - code_context entries — which files each agent retrieved
  - Integration tool call history (Jira, Slack, GitHub, etc.)
  - LangSmith trace link for deep-dive
  - Evaluation scores (PRD completeness, review verdict accuracy)

Mounted as "📊 Workflow Traces" in the dashboard sidebar.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import streamlit as st


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _api_url() -> str:
    return st.session_state.get("api_url", "http://localhost:8000")


def _api(path: str, method: str = "GET", json_body=None, timeout: int = 15):
    url = f"{_api_url()}{path}"
    try:
        if method == "GET":
            resp = httpx.get(url, timeout=timeout)
        else:
            resp = httpx.post(url, json=json_body, timeout=timeout)
        resp.raise_for_status()
        return resp.json(), None
    except httpx.TimeoutException:
        return None, "Request timed out"
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return None, str(e)


def _time_ago(ts_str: str | None) -> str:
    if not ts_str:
        return "—"
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta = datetime.now(UTC) - ts
        s = int(delta.total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return ts_str or "—"


_STATUS_COLOR = {
    "completed": "🟢",
    "running":   "🔵",
    "failed":    "🔴",
    "waiting_human": "🟡",
    "pending":   "⚪",
}

_STEP_STATUS_COLOR = {
    "completed": "✅",
    "running":   "⏳",
    "failed":    "❌",
    "skipped":   "⬛",
}

_INTEGRATION_ICONS = {
    "jira_": "🎫", "slack_": "💬", "github_": "🐙",
    "figma_": "🎨", "notion_": "📝",
}


# ── Run list panel ─────────────────────────────────────────────────────────────

def _render_run_list() -> str | None:
    """Render the run list in the sidebar. Returns selected run_id."""
    st.sidebar.subheader("Recent Runs")

    wf_filter = st.sidebar.text_input("Filter by workflow", "", placeholder="workflow name…")
    limit = st.sidebar.slider("Show last N runs", 5, 50, 20)

    path = f"/workflows/runs?limit={limit}"
    # workflow_name filter: resolve to workflow_id via the workflows list
    if wf_filter.strip():
        wf_list, _ = _api("/workflows")
        if wf_list:
            matched = next((w for w in wf_list if wf_filter.strip().lower() in w.get("name", "").lower()), None)
            if matched:
                path += f"&workflow_id={matched['id']}"

    runs, err = _api(path)
    if err:
        st.sidebar.error(f"Could not load runs: {err}")
        return None
    if not runs:
        st.sidebar.info("No runs found.")
        return None

    options = []
    for r in runs:
        icon = _STATUS_COLOR.get(r.get("status", ""), "⚪")
        label = f"{icon} {r.get('workflow_name', 'unknown')} — {_time_ago(r.get('started_at'))}"
        options.append((label, r.get("run_id") or r["id"]))

    run_id_to_label = {o[1]: o[0] for o in options}
    selected = st.sidebar.radio(
        "Select run",
        options=[o[1] for o in options],
        format_func=lambda rid: run_id_to_label.get(rid, rid),
        key="wt_selected_run_id",
        label_visibility="collapsed",
    )
    return selected


# ── Main trace detail panel ────────────────────────────────────────────────────

def _render_run_detail(run_id: str) -> None:
    run, err = _api(f"/workflows/runs/{run_id}/trace")
    if err:
        # Fall back to basic run endpoint
        run, err = _api(f"/workflows/runs/{run_id}")
    if err or not run:
        st.error(f"Could not load run {run_id}: {err}")
        return

    # ── Header ────────────────────────────────────────────────────────────────
    status = run.get("status", "unknown")
    icon = _STATUS_COLOR.get(status, "⚪")
    st.title(f"{icon} {run.get('workflow_name', 'Workflow Run')}")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Status", status.replace("_", " ").title())
    total_tokens = run.get("total_tokens") or run.get("total_tokens_used", 0)
    c2.metric("Total Tokens", f"{total_tokens:,}")
    c3.metric("Steps", len(run.get("steps", [])))
    c4.metric("Started", _time_ago(run.get("started_at")))
    dur = run.get("duration_seconds")
    if not dur and run.get("started_at") and run.get("completed_at"):
        try:
            t1 = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(run["completed_at"].replace("Z", "+00:00"))
            dur = (t2 - t1).total_seconds()
        except Exception:
            pass
    c5.metric("Duration", f"{dur:.0f}s" if dur else "—")

    if run.get("error_message"):
        st.error(f"**Error:** {run['error_message']}")

    # LangSmith link
    ls_url = run.get("langsmith_url") or run.get("graph_state", {}).get("langsmith_url")
    if ls_url:
        st.link_button("Open in LangSmith", ls_url, use_container_width=False)

    st.divider()

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab_timeline, tab_tokens, tab_context, tab_integrations, tab_state, tab_eval = st.tabs([
        "Timeline", "Token Breakdown", "Code Context", "Integration Calls", "State Snapshot", "Evaluation",
    ])

    steps = run.get("steps", [])
    graph_state = run.get("graph_state", {}) or {}
    artifacts = graph_state.get("artifacts") or run.get("artifacts", [])

    # ── Timeline tab ──────────────────────────────────────────────────────────
    with tab_timeline:
        st.subheader("Step Execution Timeline")
        if not steps:
            st.info("No step data available.")
        else:
            for step in steps:
                step_icon = _STEP_STATUS_COLOR.get(step.get("status", ""), "⬛")
                role = step.get("agent_role") or step.get("step_type") or "step"
                tokens = step.get("tokens_used", 0)
                err_msg = step.get("error_message")

                with st.container(border=True):
                    col_icon, col_info, col_tokens, col_time = st.columns([1, 5, 2, 2])
                    col_icon.markdown(f"## {step_icon}")
                    col_info.markdown(f"**{step.get('step_id', '?')}**  \n`{role}`")
                    col_tokens.metric("Tokens", f"{tokens:,}" if tokens else "—")
                    col_time.caption(_time_ago(step.get("started_at")))

                    if err_msg:
                        st.error(f"Error: {err_msg}")

                    output = step.get("output") or {}
                    if isinstance(output, dict) and output.get("text"):
                        with st.expander("Output preview"):
                            st.text(output["text"][:800] + ("..." if len(output.get("text", "")) > 800 else ""))

    # ── Token breakdown tab ───────────────────────────────────────────────────
    with tab_tokens:
        st.subheader("Token Cost per Step")
        token_data = [
            {"step": s.get("step_id", "?"), "tokens": s.get("tokens_used", 0)}
            for s in steps if s.get("tokens_used", 0) > 0
        ]
        if token_data:
            try:
                import pandas as pd
                import plotly.express as px

                df = pd.DataFrame(token_data)
                fig = px.bar(
                    df, x="step", y="tokens",
                    title="Token Usage per Agent Step",
                    color="tokens",
                    color_continuous_scale="blues",
                    labels={"step": "Step ID", "tokens": "Tokens Used"},
                )
                fig.update_layout(showlegend=False, height=350)
                st.plotly_chart(fig, use_container_width=True)

                total = sum(d["tokens"] for d in token_data)
                st.caption(f"**Total tokens:** {total:,}  |  Estimated cost (claude-sonnet-4-6): ~${total * 0.000003:.4f}")
            except Exception as e:
                st.warning(f"Chart unavailable: {e}")
                for d in token_data:
                    st.text(f"{d['step']}: {d['tokens']:,} tokens")
        else:
            st.info("No token data recorded for this run.")

    # ── Code context tab ──────────────────────────────────────────────────────
    with tab_context:
        st.subheader("Accumulated Codebase Context")
        st.caption(
            "These are the codebase snippets retrieved by all agents during this run. "
            "Each downstream agent received the prior agents' context automatically."
        )
        code_context = graph_state.get("code_context") or []
        if code_context:
            # Group by agent step prefix (entries may be tagged as "step_id → snippet")
            grouped: dict[str, list[str]] = {}
            for entry in code_context:
                if "→" in entry or ": " in entry:
                    sep = "→" if "→" in entry else ": "
                    parts = entry.split(sep, 1)
                    step_key = parts[0].strip()
                    snippet = parts[1].strip() if len(parts) > 1 else entry
                    grouped.setdefault(step_key, []).append(snippet)
                else:
                    grouped.setdefault("(general)", []).append(entry)

            for step_key, entries in grouped.items():
                with st.expander(f"**{step_key}** — {len(entries)} snippet(s)"):
                    for e in entries:
                        st.code(e, language="text")

            st.caption(f"**{len(code_context)} total context entries** accumulated across all agents.")
        else:
            # Fallback: show key state fields that contain code
            code_diff = graph_state.get("code_diff") or ""
            impl_plan = graph_state.get("implementation_plan") or ""
            if code_diff or impl_plan:
                st.info(
                    "No explicit code_context entries — showing key state fields instead. "
                    "Run a new workflow to see per-step context accumulation."
                )
                if impl_plan:
                    with st.expander("📋 Implementation Plan"):
                        st.markdown(impl_plan[:3000])
                if code_diff:
                    with st.expander("💻 Generated Code"):
                        st.code(code_diff[:4000], language="python")
            else:
                st.info(
                    "No codebase context was accumulated during this run. "
                    "This may be a run from before context tracking was added."
                )

    # ── Integration calls tab ─────────────────────────────────────────────────
    with tab_integrations:
        st.subheader("Integration Tool Calls")
        st.caption("External service calls made by agents during this workflow run.")

        integration_results = {}

        # Surface known integration state fields
        if graph_state.get("jira_issue_key"):
            integration_results["Jira Issue Created"] = f"🎫 {graph_state['jira_issue_key']}"
        if graph_state.get("github_pr_url"):
            integration_results["GitHub PR"] = f"🐙 [{graph_state['github_pr_url']}]({graph_state['github_pr_url']})"
        if graph_state.get("slack_message_ts"):
            integration_results["Slack Message"] = f"💬 ts={graph_state['slack_message_ts']}"

        # Also scan pr_creator / devops_agent step outputs for PR_URL lines
        if not graph_state.get("github_pr_url"):
            import re as _re
            for step in steps:
                if step.get("step_id") in ("pr_creator", "devops_agent"):
                    out = step.get("output") or {}
                    text = out.get("text", "") if isinstance(out, dict) else str(out)
                    m = _re.search(r'https://github\.com/[^\s"<>]+/pull/\d+', text)
                    if m:
                        pr_url = m.group(0)
                        integration_results["GitHub PR (from step output)"] = f"🐙 [{pr_url}]({pr_url})"
                        break

        if integration_results:
            st.markdown("**External Resources Created:**")
            for label, value in integration_results.items():
                st.markdown(f"  **{label}:** {value}")
            st.divider()

        # Show pr_creator step output details
        pr_step = next((s for s in steps if s.get("step_id") in ("pr_creator", "devops_agent")), None)
        if pr_step:
            out = pr_step.get("output") or {}
            text = out.get("text", "") if isinstance(out, dict) else str(out)
            if text and text.strip() != "router:pr_creator":
                with st.expander("🐙 pr_creator — Full Execution Report", expanded=bool(integration_results)):
                    st.markdown(text)
        elif not integration_results:
            st.info("No integration tools were called in this run.")

    # ── State snapshot tab ────────────────────────────────────────────────────
    with tab_state:
        st.subheader("Final Workflow State Snapshot")
        st.caption("The complete GraphState values at the end of the run.")

        state_fields = [
            ("prd", "PRD", "📋"),
            ("component_spec", "Component Spec", "🎨"),
            ("implementation_plan", "Implementation Plan", "🗺️"),
            ("code_diff", "Code Changes", "💻"),
            ("review_verdict", "Review Verdict", "🔍"),
            ("review_notes", "Review Notes", "📝"),
            ("test_plan", "Test Plan", "🧪"),
            ("deployment_plan", "Deployment Plan", "🚀"),
            ("final_report", "Final Report", "📊"),
        ]

        found_any = False
        for key, label, icon in state_fields:
            val = graph_state.get(key, "")
            if val:
                found_any = True
                with st.expander(f"{icon} {label}"):
                    st.markdown(val[:3000] + ("..." if len(val) > 3000 else ""))

        if not found_any:
            st.info("No enterprise state fields were written in this run.")

    # ── Evaluation tab ────────────────────────────────────────────────────────
    with tab_eval:
        st.subheader("Workflow Quality Evaluation")
        st.caption("Automated quality scores computed against golden evaluation criteria.")

        if st.button("Run Evaluation", use_container_width=False):
            with st.spinner("Evaluating workflow outputs…"):
                eval_data, eval_err = _api(
                    f"/workflows/runs/{run_id}/evaluate",
                    method="POST",
                )

            if eval_err:
                # Compute locally as fallback
                try:
                    import asyncio
                    from src.observability.evaluators import run_workflow_evaluation
                    eval_data = asyncio.run(run_workflow_evaluation(
                        graph_state,
                        workflow_name=run.get("workflow_name", "unknown"),
                    ))
                except Exception as e:
                    st.error(f"Evaluation failed: {e}")
                    eval_data = None

            if eval_data:
                overall = eval_data.get("overall_score", 0)
                status_icon = "✅" if eval_data.get("passed") else "❌"
                st.metric("Overall Quality Score", f"{overall:.0%}", help="0-100% quality score")
                st.markdown(f"**Verdict:** {status_icon} {'Passed' if eval_data.get('passed') else 'Needs Improvement'}")

                step_scores = eval_data.get("step_scores", {})
                if step_scores:
                    st.divider()
                    st.markdown("**Per-Step Scores:**")
                    for step_key, score_data in step_scores.items():
                        score_val = score_data.get("score", 0)
                        bar = "█" * int(score_val * 10) + "░" * (10 - int(score_val * 10))
                        st.text(f"  {step_key:<25} {bar} {score_val:.0%}")
        else:
            st.info("Click 'Run Evaluation' to score this workflow run against quality criteria.")


# ── Page entry point ───────────────────────────────────────────────────────────

def render() -> None:
    st.title("📊 Workflow Traces")
    st.caption("Step-level execution traces, token costs, and quality evaluations for every workflow run.")

    selected_run_id = _render_run_list()

    if selected_run_id:
        _render_run_detail(selected_run_id)
    else:
        st.info("Select a run from the sidebar to view its trace.")
        st.markdown(
            "**Tip:** Trigger a workflow via `POST /workflows/{id}/run` "
            "or the **Workflows** page to generate traces."
        )
