"""
Planning Mode dashboard page.

Sends a query to POST /plan and renders the ImplementationPlan as a
structured, colour-coded document — similar to Cursor's planning view.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

import streamlit as st

from src.ui.helpers import api_get, api_post


# ── Severity colours ──────────────────────────────────────────────────────────
_SEVERITY_COLOR = {"low": "🟡", "medium": "🟠", "high": "🔴"}
_ACTION_COLOR = {
    "create":  "🟢",
    "modify":  "🔵",
    "delete":  "🔴",
    "rename":  "🟡",
    "move":    "🟡",
}


def render():
    st.title("🧩 Planning Mode")
    st.markdown(
        "Describe a bug, feature, or refactoring task. "
        "The planner **searches the web** for the best approach and libraries, "
        "then combines that with your **live codebase index** to generate a "
        "grounded, step-by-step implementation plan."
    )

    st.info(
        "**Requires:** `ANTHROPIC_API_KEY` in `.env` — used for both web research "
        "and plan generation. Web research runs in parallel with codebase retrieval.",
        icon="ℹ️",
    )

    # ── Repo selector ─────────────────────────────────────────────────────────
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

    with st.form("plan_form"):
        query = st.text_area(
            "Describe your task",
            placeholder=(
                "e.g. 'Add rate limiting to the /search endpoint — 100 req/min per IP'\n"
                "or 'Fix the bug where webhook events stay in queued status'\n"
                "or 'Add TypeScript support to the Tree-sitter parser'"
            ),
            height=130,
        )

        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            repo_label = st.selectbox("Scope to repository (optional)", options=repo_options)
        with col2:
            web_research = st.checkbox(
                "🌐 Web research",
                value=True,
                help=(
                    "Search the web for best practices and library recommendations "
                    "before generating the plan. Runs in parallel — no extra wait time."
                ),
            )
        with col3:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            submitted = st.form_submit_button(
                "Generate Plan", type="primary", use_container_width=True
            )

    if submitted:
        if not query.strip():
            st.warning("Please enter a task description.")
            st.stop()

        if len(query.strip()) < 10:
            st.warning("Task description is too short — be more specific.")
            st.stop()

        # Build request payload
        payload: dict = {"query": query.strip(), "stream": False, "web_research": web_research}
        if repo_label != "All repos":
            owner, name = repo_map.get(repo_label, (None, None))
            if owner and name:
                payload["repo_owner"] = owner
                payload["repo_name"] = name

        # Call API
        research_label = " + web research" if web_research else ""
        with st.spinner(f"Retrieving code context{research_label} and generating plan… (30–120s)"):
            t0 = time.monotonic()
            plan_data, err = api_post("/plan", json=payload, timeout=180)
            elapsed = time.monotonic() - t0

        if err:
            st.error(f"Plan generation failed: {err}")
            if "ANTHROPIC_API_KEY" in str(err) or "anthropic" in str(err).lower():
                st.markdown(
                    "Make sure `ANTHROPIC_API_KEY` is set in your `.env` file "
                    "and the server has been restarted."
                )
            st.stop()

        if plan_data and "error" in plan_data:
            st.error(plan_data["error"])
            st.stop()

        if not plan_data:
            st.error("Received an empty response from the server.")
            st.stop()

        # ── Route to the right renderer based on response type ────────────────
        response_type = plan_data.get("response_type", "plan")
        if response_type == "answer":
            _render_answer(plan_data, elapsed)
        elif response_type == "analysis":
            _render_analysis(plan_data, elapsed)
        else:
            _render_plan(plan_data, elapsed)


# ── Shared metadata bar ────────────────────────────────────────────────────────

def _render_metadata_bar(plan: dict, elapsed: float):
    st.success(f"Response generated in **{elapsed:.1f}s**")
    meta = plan.get("metadata") or {}
    if meta:
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("Context tokens", f"{meta.get('context_tokens', 0):,}")
        mc2.metric("Code chunks", meta.get("context_files", 0))
        mc3.metric("Model", meta.get("model", "—").split("-")[-1])
        web_used = meta.get("web_research_used", False)
        mc4.metric("Web research", "✅ yes" if web_used else "⬜ no")
    if plan.get("plan_id"):
        st.caption(f"ID: `{plan['plan_id']}`")


# ── Answer renderer (questions / explanations / analysis) ─────────────────────

def _render_answer(plan: dict, elapsed: float):
    """Render a conversational answer — rich markdown, no files/steps/risks."""
    _render_metadata_bar(plan, elapsed)

    meta = plan.get("metadata") or {}
    stack_fp = meta.get("stack_fingerprint", "")
    if stack_fp:
        with st.expander("📦 Codebase Stack Fingerprint", expanded=False):
            st.markdown(stack_fp)

    st.divider()

    # Main answer
    answer = plan.get("answer", "_No answer generated._")
    st.markdown(answer)

    # Key files for quick navigation
    key_files = plan.get("key_files") or []
    if key_files:
        st.divider()
        st.caption("📁 Referenced files: " + " · ".join(f"`{f}`" for f in key_files))

    # Retrieval log (debug)
    if meta.get("retrieval_log"):
        with st.expander("Retrieval Log (debug)", expanded=False):
            st.code(meta["retrieval_log"], language="text")


# ── Analysis renderer (improvement / review / audit queries) ──────────────────

def _render_analysis(plan: dict, elapsed: float):
    """Render a deep technical analysis — world-class architect review with grounded suggestions."""
    _render_metadata_bar(plan, elapsed)

    meta = plan.get("metadata") or {}
    stack_fp = meta.get("stack_fingerprint", "")
    if stack_fp:
        with st.expander("📦 Codebase Stack Fingerprint", expanded=False):
            st.markdown(stack_fp)

    st.divider()

    # Main analysis (markdown with mandatory sections)
    analysis = plan.get("analysis", "_No analysis generated._")
    st.markdown(analysis)

    # Key files for quick navigation
    key_files = plan.get("key_files") or []
    if key_files:
        st.divider()
        st.caption("📁 Analyzed files: " + " · ".join(f"`{f}`" for f in key_files))

    # Retrieval log (debug)
    if meta.get("retrieval_log"):
        with st.expander("Retrieval Log (debug)", expanded=False):
            st.code(meta["retrieval_log"], language="text")


# ── Plan renderer (implementation tasks) ──────────────────────────────────────

def _render_plan(plan: dict, elapsed: float):
    _render_metadata_bar(plan, elapsed)

    meta = plan.get("metadata") or {}

    st.divider()

    # ── Stack-Aware Gap Analysis ──────────────────────────────────────────────
    web_notes = meta.get("web_research_notes", "")
    if web_notes:
        with st.expander(
            "🔍 Stack-Aware Gap Analysis (what's missing & how to integrate)",
            expanded=True,
        ):
            st.markdown(web_notes)
        st.divider()

    # ── Stack Fingerprint (collapsed by default — it's informational) ─────────
    stack_fp = meta.get("stack_fingerprint", "")
    if stack_fp:
        with st.expander("📦 Codebase Stack Fingerprint (what's already installed)", expanded=False):
            st.markdown(stack_fp)
        st.divider()

    # ── Summary ───────────────────────────────────────────────────────────────
    st.subheader("Summary")
    st.markdown(plan.get("summary", "_No summary generated._"))

    assumptions = plan.get("clarifying_assumptions") or []
    if assumptions:
        with st.expander("Clarifying Assumptions"):
            for a in assumptions:
                st.markdown(f"- {a}")

    st.divider()

    # ── Files to Change ───────────────────────────────────────────────────────
    files = plan.get("files") or []
    if files:
        st.subheader(f"Files to Change ({len(files)})")

        for file_change in files:
            path = file_change.get("path", "unknown")
            action = file_change.get("action", "modify")
            reason = file_change.get("reason", "")
            changes = file_change.get("changes") or []
            icon = _ACTION_COLOR.get(action, "⬜")

            with st.expander(f"{icon} `{path}` — {action.upper()}", expanded=True):
                st.caption(reason)

                for chg in changes:
                    kind = chg.get("kind", "modify")
                    symbol = chg.get("symbol", "")
                    desc = chg.get("description", "")
                    pseudo = chg.get("pseudocode", "")
                    line_hint = chg.get("line_hint", "")

                    sym_str = f" `{symbol}`" if symbol else ""
                    line_str = f"  _(~L{line_hint})_" if line_hint else ""
                    st.markdown(f"**{kind.upper()}**{sym_str}: {desc}{line_str}")

                    if pseudo:
                        st.code(pseudo, language="python")

    st.divider()

    # ── Execution Steps ───────────────────────────────────────────────────────
    steps = plan.get("steps") or []
    if steps:
        st.subheader(f"Execution Steps ({len(steps)})")

        for step in steps:
            num = step.get("step_number", "?")
            title = step.get("title", "")
            desc = step.get("description", "")
            files_inv = step.get("files_involved") or []
            deps = step.get("depends_on_steps") or []
            verify = step.get("verification", "")

            dep_str = f" _(after steps {deps})_" if deps else ""
            with st.container(border=True):
                st.markdown(f"**Step {num}: {title}**{dep_str}")
                st.markdown(desc)
                if files_inv:
                    st.caption("Files: " + " · ".join(f"`{f}`" for f in files_inv))
                if verify:
                    st.success(f"✅ **Verify:** {verify}")

    st.divider()

    # ── Risks ─────────────────────────────────────────────────────────────────
    risks = plan.get("risks") or []
    if risks:
        st.subheader(f"Risks ({len(risks)})")

        for risk in risks:
            sev = risk.get("severity", "low")
            desc = risk.get("description", "")
            affected = risk.get("affected_symbols") or []
            mitigation = risk.get("mitigation", "")
            icon = _SEVERITY_COLOR.get(sev, "⬜")

            with st.container(border=True):
                st.markdown(f"{icon} **{sev.upper()}** — {desc}")
                if affected:
                    st.caption("Affected: " + ", ".join(f"`{s}`" for s in affected))
                st.info(f"**Mitigation:** {mitigation}")

    # ── Test Plan ─────────────────────────────────────────────────────────────
    test_plan = plan.get("test_plan", "")
    if test_plan:
        st.divider()
        st.subheader("Test Plan")
        st.markdown(test_plan)

    # ── Retrieval log (debug) ─────────────────────────────────────────────────
    if meta.get("retrieval_log"):
        with st.expander("Retrieval Log (debug)"):
            st.code(meta["retrieval_log"], language="text")
