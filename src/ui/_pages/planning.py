"""
Planning Mode dashboard page.

Sends a query to POST /plan and renders the ImplementationPlan as a
structured, colour-coded document — similar to Cursor's planning view.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

import httpx
import streamlit as st

from src.ui.helpers import AGENT_DEFAULT_ICON, AGENT_TOOL_ICONS, api_get, render_agent_timeline_html

# ── Severity colours ──────────────────────────────────────────────────────────
_SEVERITY_COLOR = {"low": "🟡", "medium": "🟠", "high": "🔴"}
_ACTION_COLOR = {
    "create": "🟢",
    "modify": "🔵",
    "delete": "🔴",
    "rename": "🟡",
    "move": "🟡",
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
        "**Requires:** `ANTHROPIC_API_KEY` in `.env`. "
        "Claude searches the codebase iteratively with extended thinking, then outputs a grounded plan.",
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

    # ── Model selector ────────────────────────────────────────────────────────
    models_data, _ = api_get("/models", timeout=5)
    model_options = ["Default"]
    model_map: dict[str, str] = {}
    if models_data:
        for m in models_data:
            label = f"{m['model']} ({m['provider']})"
            model_options.append(label)
            model_map[label] = m["model"]

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

        col1, col2, col3, col4, col5 = st.columns([2, 1.5, 1, 1, 1])
        with col1:
            repo_label = st.selectbox("Scope to repository (optional)", options=repo_options)
        with col2:
            model_label = st.selectbox("LLM Model", options=model_options)
        with col3:
            web_research = st.checkbox(
                "🌐 Web research",
                value=True,
                help=(
                    "Search the web for best practices and library recommendations "
                    "before generating the plan. Runs in parallel — no extra wait time. "
                    "Only available with Anthropic models."
                ),
            )
        with col4:
            stream_enabled = st.checkbox(
                "⚡ Stream",
                value=False,
                help="Stream tokens in real-time (experimental). Off = wait for full response.",
            )
        with col5:
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

        payload: dict = {
            "query": query.strip(),
            "stream": stream_enabled,
            "web_research": web_research,
        }
        if model_label != "Default":
            payload["model"] = model_map.get(model_label)
        if repo_label != "All repos":
            owner, name = repo_map.get(repo_label, (None, None))
            if owner and name:
                payload["repo_owner"] = owner
                payload["repo_name"] = name

        api_url = os.getenv("API_URL", "http://localhost:8000")

        # Reset execution timeline for this new plan
        st.session_state["plan_agent_logs"] = []

        if stream_enabled:
            plan_data, elapsed = _request_streaming(api_url, payload)
        else:
            plan_data, elapsed = _request_sync(api_url, payload)

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


# ── Sync request (default) ────────────────────────────────────────────────────


def _request_sync(api_url: str, payload: dict) -> tuple[dict | None, float]:
    """POST /plan with stream=false, wait for the full JSON response."""
    status_box = st.empty()
    web_label = " + web research" if payload.get("web_research") else ""
    status_box.info(f"⏳ Generating plan{web_label}… this may take 20–60 seconds.")

    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=180) as client:
            resp = client.post(f"{api_url}/plan", json=payload)
    except httpx.ConnectError:
        status_box.empty()
        st.error("Cannot connect to the API server. Is it running on `localhost:8000`?")
        st.stop()
    except Exception as exc:
        status_box.empty()
        st.error(f"Request failed: {exc}")
        st.stop()

    elapsed = time.monotonic() - t0
    status_box.empty()

    if resp.status_code != 200:
        data = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        msg = data.get("error", f"HTTP {resp.status_code}")
        st.error(f"❌ {msg}")
        if resp.status_code == 429 or "rate limit" in str(msg).lower():
            st.info(
                "⏳ **Rate limit exceeded.** The server serializes concurrent "
                "requests and retries automatically, but your API tier's token "
                "limit was still reached. Please wait 60 seconds and try again."
            )
        elif "overloaded" in str(msg).lower() or "529" in str(msg):
            st.info(
                "The Anthropic API is temporarily overloaded. "
                "The server already retried automatically. "
                "Please wait 30–60 seconds and try again."
            )
        st.stop()

    return resp.json(), elapsed


# ── Streaming request ──────────────────────────────────────────────────────────


def _request_streaming(api_url: str, payload: dict) -> tuple[dict | None, float]:
    """
    POST /plan with stream=true, render SSE events as a Cursor-style timeline.

    Layout during execution:
      ┌─ 🧠 Generating plan… (expanded) ─────────────────────────────────┐
      │  ✓ 🔍 search_codebase   "rate limiting patterns"    2,341t        │
      │  💭 Reviewing best approach…                                      │
      │  ✓ 🔧 resolve-library-id  "express-rate-limit"        418t        │
      └───────────────────────────────────────────────────────────────────┘
      ✍️ Generating… 3,412 chars received   ← plan_chunk progress

    On plan_complete: trace collapses to "✅ 4 tool calls · 12.3s", plan renders below.
    """
    plan_steps: list[dict] = []
    plan_data:  dict | None = None
    t0 = time.monotonic()
    accumulated_text = ""

    web_label = " + web research" if payload.get("web_research") else ""

    # ── Reasoning trace (Cursor-style st.status) ──────────────────────────────
    with st.status(f"🧠 Generating plan{web_label}…", expanded=True) as trace_status:
        trace_placeholder = st.empty()

    # Progress counter for partial JSON (plan_chunk) — rendered below trace
    progress_box = st.empty()

    try:
        with (
            httpx.Client(timeout=180) as client,
            client.stream("POST", f"{api_url}/plan", json=payload) as resp,
        ):
            if resp.status_code != 200:
                trace_status.update(label="❌ Request failed", state="error")
                st.error(f"API error: HTTP {resp.status_code}")
                st.stop()

            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")

                # ── Status label ──────────────────────────────────────────────
                if etype == "status":
                    trace_status.update(
                        label=f"🧠 {event['message']}", state="running"
                    )

                # ── Extended thinking ─────────────────────────────────────────
                elif etype == "thinking":
                    text = event.get("text", "")
                    if text:
                        preview = (text[:120] + "…") if len(text) > 120 else text
                        step = {"type": "thinking", "summary": preview}
                        plan_steps.append(step)
                        st.session_state["plan_agent_logs"].append(step)
                        trace_placeholder.markdown(
                            render_agent_timeline_html(plan_steps),
                            unsafe_allow_html=True,
                        )

                # ── Tool called ───────────────────────────────────────────────
                elif etype == "agent_tool_call":
                    tool    = event.get("tool", "")
                    summary = event.get("input_summary", "")
                    step = {"type": "tool_call", "tool": tool,
                            "summary": summary, "state": "running", "tokens": None}
                    plan_steps.append(step)
                    st.session_state["plan_agent_logs"].append(step)
                    trace_placeholder.markdown(
                        render_agent_timeline_html(plan_steps),
                        unsafe_allow_html=True,
                    )
                    icon  = AGENT_TOOL_ICONS.get(tool, AGENT_DEFAULT_ICON)
                    short = summary[:50] + "…" if len(summary) > 50 else summary
                    trace_status.update(
                        label=f"{icon} {tool}: {short}" if short else f"{icon} {tool}",
                        state="running",
                    )

                # ── Tool returned ─────────────────────────────────────────────
                elif etype == "agent_tool_result":
                    tool   = event.get("tool", "")
                    tokens = event.get("tokens", 0)
                    for step in plan_steps:
                        if step.get("tool") == tool and step.get("state") == "running":
                            step["state"]  = "done"
                            step["tokens"] = tokens
                            break
                    trace_placeholder.markdown(
                        render_agent_timeline_html(plan_steps),
                        unsafe_allow_html=True,
                    )
                    running = sum(1 for s in plan_steps if s.get("state") == "running")
                    done_n  = sum(1 for s in plan_steps if s.get("state") == "done")
                    if running:
                        trace_status.update(
                            label=f"🧠 {running} tool{'s' if running > 1 else ''} in progress…",
                            state="running",
                        )
                    else:
                        trace_status.update(
                            label=f"🧠 {done_n} tool{'s' if done_n > 1 else ''} done · composing plan…",
                            state="running",
                        )

                # ── Legacy retrieval event ────────────────────────────────────
                elif etype == "retrieval_complete":
                    chunks   = event.get("chunks", 0)
                    tokens   = event.get("tokens", 0)
                    web_icon = "🌐 " if event.get("web_research_used") else ""
                    trace_status.update(
                        label=f"✅ {web_icon}Retrieved {chunks} chunks · {tokens:,} tokens",
                        state="running",
                    )

                # ── Plan tokens streaming ─────────────────────────────────────
                elif etype == "plan_chunk":
                    accumulated_text += event.get("text", "")
                    # Plan = partial JSON → show char counter
                    # Answer/analysis = plain markdown → stream it live
                    if accumulated_text.lstrip().startswith("{"):
                        progress_box.caption(
                            f"✍️ Generating… **{len(accumulated_text):,}** chars received"
                        )
                    else:
                        progress_box.markdown(accumulated_text + " ▌")

                # ── Plan complete ─────────────────────────────────────────────
                elif etype == "plan_complete":
                    plan_data = event.get("plan")

                    # Finalise all still-running steps
                    for step in plan_steps:
                        if step.get("state") == "running":
                            step["state"] = "done"

                    st.session_state["plan_agent_logs"] = list(plan_steps)
                    trace_placeholder.markdown(
                        render_agent_timeline_html(plan_steps),
                        unsafe_allow_html=True,
                    )

                    elapsed_so_far = time.monotonic() - t0
                    n_calls = sum(1 for s in plan_steps if s.get("type") == "tool_call")
                    trace_status.update(
                        label=f"✅ {n_calls} tool call{'s' if n_calls != 1 else ''} · {elapsed_so_far:.1f}s",
                        state="complete",
                        expanded=False,
                    )
                    progress_box.empty()

                # ── Error ─────────────────────────────────────────────────────
                elif etype == "error":
                    progress_box.empty()
                    trace_status.update(label="❌ Error", state="error", expanded=True)
                    msg = event.get("message", "Unknown error")
                    st.error(f"❌ {msg}")
                    if "rate limit" in str(msg).lower() or event.get("retry_after"):
                        st.info(
                            "⏳ **Rate limit exceeded.** The server serializes concurrent "
                            "requests and retries automatically, but your API tier's token "
                            "limit was still reached. Please wait 60 seconds and try again."
                        )
                    elif "overloaded" in str(msg).lower() or "529" in str(msg):
                        st.info(
                            "The Anthropic API is temporarily overloaded. "
                            "The server already retried automatically. "
                            "Please wait 30–60 seconds and try again."
                        )
                    elif "ANTHROPIC_API_KEY" in str(msg):
                        st.markdown(
                            "Make sure `ANTHROPIC_API_KEY` is set in your `.env` file "
                            "and the server has been restarted."
                        )
                    st.stop()

    except httpx.ConnectError:
        trace_status.update(label="❌ Connection failed", state="error")
        st.error("Cannot connect to the API server. Is it running on `localhost:8000`?")
        st.stop()
    except Exception as exc:
        trace_status.update(label="❌ Streaming error", state="error")
        st.error(f"Streaming error: {exc}")
        st.stop()

    elapsed = time.monotonic() - t0
    return plan_data, elapsed


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
        with st.expander(
            "📦 Codebase Stack Fingerprint (what's already installed)", expanded=False
        ):
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

    # ── Key Design Decisions ──────────────────────────────────────────────────
    decisions = plan.get("design_decisions") or []
    if decisions:
        st.divider()
        st.subheader("Key Design Decisions")
        for d in decisions:
            st.markdown(f"- {d}")

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
