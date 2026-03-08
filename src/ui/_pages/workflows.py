"""
Workflows page — clean UI for the Codebase Automation Engine.

Layout:
  Tab 1 — My Workflows   : list cards with Run / Edit / Delete
  Tab 2 — New Workflow   : YAML editor with sample template + save
  Tab 3 — Run History    : recent runs across all workflows, with live step details
"""
from __future__ import annotations

import time
import textwrap

import streamlit as st
import requests

# ── Helpers ───────────────────────────────────────────────────────────────────

def _base() -> str:
    return st.session_state.get("api_url", "http://localhost:8000")

_SAMPLE_YAML = textwrap.dedent("""\
name: pr-review-workflow
description: "Automatically review a pull request and post a summary comment."

trigger:
  type: manual

steps:
  - id: fetch_diff
    type: action
    action: github.get_pr_diff
    params:
      repo_owner: "{{ trigger.repo_owner }}"
      repo_name:  "{{ trigger.repo_name }}"
      pr_number:  "{{ trigger.pr_number }}"

  - id: review
    type: agent
    role: reviewer
    depends_on: [fetch_diff]
    task: |
      Review the following pull request diff and provide:
      1. A brief summary of the changes
      2. Any potential issues or bugs
      3. Suggestions for improvement

    context_inject:
      - "PR Diff": "{{ steps.fetch_diff.output }}"

  - id: notify
    type: action
    action: github.post_pr_comment
    depends_on: [review]
    params:
      repo_owner: "{{ trigger.repo_owner }}"
      repo_name:  "{{ trigger.repo_name }}"
      pr_number:  "{{ trigger.pr_number }}"
      body:       "{{ steps.review.output }}"
""")

_STATUS_COLOUR = {
    "pending":       "🟡",
    "running":       "🔵",
    "waiting_human": "🟠",
    "completed":     "🟢",
    "failed":        "🔴",
    "cancelled":     "⚫",
    "skipped":       "⚪",
}


def _api(method: str, path: str, **kwargs):
    """Thin wrapper around requests; returns (ok, data/error_str)."""
    try:
        resp = getattr(requests, method)(f"{_base()}{path}", timeout=15, **kwargs)
        if resp.ok:
            return True, resp.json()
        return False, resp.json().get("detail", resp.text)
    except Exception as exc:
        return False, str(exc)


def _repos() -> list[str]:
    """Return list of 'owner/name' strings from the /repos endpoint."""
    ok, data = _api("get", "/repos")
    if ok and isinstance(data, list):
        return [r["repo"] for r in data]
    return []


def _status_badge(status: str) -> str:
    icon = _STATUS_COLOUR.get(status, "❓")
    return f"{icon} {status.replace('_', ' ').title()}"


# ── Main render ───────────────────────────────────────────────────────────────

def render():
    st.title("⚡ Workflows")
    st.caption("Automate multi-step agentic tasks across your codebase.")

    tab_list, tab_new, tab_history = st.tabs(
        ["📋 My Workflows", "➕ New Workflow", "🕐 Run History"]
    )

    with tab_list:
        _render_workflow_list()

    with tab_new:
        _render_new_workflow()

    with tab_history:
        _render_run_history()


# ── Tab 1: Workflow list ───────────────────────────────────────────────────────

def _render_workflow_list():
    col_refresh, col_toggle = st.columns([6, 2])
    with col_refresh:
        st.subheader("Your Workflows")
    with col_toggle:
        show_all = st.toggle("Show inactive", value=False)

    ok, workflows = _api("get", f"/workflows?active_only={not show_all}")
    if not ok:
        st.error(f"Could not load workflows: {workflows}")
        return

    if not workflows:
        st.info("No workflows yet. Create one in the **New Workflow** tab.")
        return

    repos = _repos()

    for wf in workflows:
        with st.expander(
            f"{'🟢' if wf.get('is_active') else '⚫'}  **{wf['name']}**  —  {wf.get('description') or ''}",
            expanded=False,
        ):
            cols = st.columns([2, 2, 1, 1])
            cols[0].caption(f"ID: `{wf['id'][:8]}…`")
            cols[1].caption(f"Trigger: `{wf.get('trigger_type', 'manual')}`")
            cols[2].caption(f"v{wf.get('version', 1)}")
            cols[3].caption("Active" if wf.get("is_active") else "Inactive")

            run_col, edit_col, del_col = st.columns(3)

            # ── Run this workflow ──────────────────────────────────────────
            with run_col:
                with st.popover("▶ Run", use_container_width=True):
                    st.markdown("**Configure run**")

                    trigger_type = wf.get("trigger_type", "manual")

                    # Repo selector
                    repo_choice = st.selectbox(
                        "Repository",
                        options=["(none)"] + repos,
                        key=f"repo_{wf['id']}",
                        help="Passed to workflow as trigger.repo_owner / trigger.repo_name",
                    )

                    # For webhook workflows, hint the user that they need to fill in payload
                    if trigger_type == "webhook":
                        st.info(
                            "⚠️ **Webhook workflow** — this workflow reads data from the "
                            "trigger payload. Paste your JSON payload below (e.g. alert data, "
                            "PR info, event object). Leaving it as `{}` will cause template "
                            "fields like `{{ trigger.service }}` to render as `[MISSING: service]`.",
                            icon=None,
                        )

                    # Payload textarea — remember last value per workflow in session state
                    payload_key = f"payload_{wf['id']}"
                    default_payload = st.session_state.get(f"_last_payload_{wf['id']}", "{}")

                    extra_raw = st.text_area(
                        "Trigger payload (JSON)" if trigger_type == "webhook"
                        else "Extra trigger payload (JSON, optional)",
                        value=default_payload,
                        height=160 if trigger_type == "webhook" else 80,
                        key=payload_key,
                        placeholder='{\n  "service": "my-api",\n  "error_message": "...",\n  "stack_trace": "..."\n}' if trigger_type == "webhook" else "{}",
                        help="All fields become available as {{ trigger.FIELD }} in the workflow YAML.",
                    )

                    if st.button("🚀 Trigger run", key=f"go_{wf['id']}", use_container_width=True):
                        import json as _json
                        try:
                            payload = _json.loads(extra_raw or "{}")
                        except Exception:
                            st.error("❌ Invalid JSON — check syntax and try again.")
                            st.stop()

                        # Warn (but don't block) when webhook workflow has empty payload
                        if trigger_type == "webhook" and payload == {}:
                            st.warning(
                                "Payload is empty `{}` — template variables in this workflow "
                                "will render as `[MISSING: field]`. Add the alert/event JSON above."
                            )

                        # Cache the payload for next time
                        st.session_state[f"_last_payload_{wf['id']}"] = extra_raw

                        # Inject repo into payload
                        if repo_choice != "(none)":
                            owner, name = repo_choice.split("/", 1)
                            payload.setdefault("repo_owner", owner)
                            payload.setdefault("repo_name", name)

                        ok2, run = _api(
                            "post",
                            f"/workflows/{wf['id']}/run",
                            json={"payload": payload},
                        )
                        if ok2:
                            st.success(f"✅ Run started — ID: `{run['run_id'][:12]}…`")
                            st.session_state["last_run_id"] = run["run_id"]
                        else:
                            st.error(f"Failed: {run}")

            # ── Edit workflow YAML ─────────────────────────────────────────
            with edit_col:
                if st.button("✏️ Edit", key=f"edit_{wf['id']}", use_container_width=True):
                    # Fetch full definition — the list endpoint omits yaml_definition
                    ok_full, wf_full = _api("get", f"/workflows/{wf['id']}")
                    if ok_full:
                        st.session_state["edit_wf_id"]   = wf["id"]
                        st.session_state["edit_wf_yaml"] = wf_full.get("yaml_definition", "")
                        st.session_state["edit_wf_desc"] = wf_full.get("description", "")
                        st.rerun()
                    else:
                        st.error(f"Could not load workflow: {wf_full}")

            # ── Delete ────────────────────────────────────────────────────
            with del_col:
                with st.popover("🗑 Delete", use_container_width=True):
                    st.warning("This will delete the workflow and all its run history.")
                    if st.button("Confirm delete", key=f"del_confirm_{wf['id']}", use_container_width=True):
                        ok3, _ = _api("delete", f"/workflows/{wf['id']}")
                        if ok3:
                            st.success("Deleted.")
                            st.rerun()
                        else:
                            st.error("Delete failed.")

    # ── Inline edit panel (shown when an Edit button was clicked) ─────────────
    if "edit_wf_id" in st.session_state:
        st.divider()
        st.subheader("✏️ Edit Workflow")
        new_desc = st.text_input("Description", value=st.session_state.get("edit_wf_desc", ""))
        new_yaml = st.text_area(
            "YAML Definition",
            value=st.session_state.get("edit_wf_yaml", ""),
            height=400,
        )
        save_col, cancel_col = st.columns(2)
        if save_col.button("💾 Save changes", use_container_width=True):
            ok4, result = _api(
                "post",
                "/workflows",
                json={
                    "name": _extract_name_from_yaml(new_yaml),
                    "yaml_definition": new_yaml,
                    "description": new_desc,
                },
            )
            if ok4:
                st.success("Workflow updated.")
                del st.session_state["edit_wf_id"]
                st.rerun()
            else:
                st.error(f"Save failed: {result}")
        if cancel_col.button("Cancel", use_container_width=True):
            del st.session_state["edit_wf_id"]
            st.rerun()


# ── Tab 2: New workflow ────────────────────────────────────────────────────────

_AGENT_ROLES = ["searcher", "planner", "reviewer", "coder", "tester", "supervisor"]
_STEP_TYPES  = ["agent", "action", "human_checkpoint"]

_ROLE_DESCRIPTIONS = {
    "searcher":   "Searches the codebase for relevant context",
    "planner":    "Breaks down tasks and creates implementation plans",
    "reviewer":   "Reviews code, diffs, or outputs for quality issues",
    "coder":      "Writes or modifies code based on instructions",
    "tester":     "Creates or runs tests and validates results",
    "supervisor": "Coordinates other agents and makes high-level decisions",
}

def _render_new_workflow():
    st.subheader("Create a New Workflow")

    # ── Mode toggle ───────────────────────────────────────────────────────────
    mode = st.radio(
        "Creation mode",
        ["🧩 Guided Builder", "📝 YAML Editor"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if mode == "🧩 Guided Builder":
        _render_guided_builder()
    else:
        _render_yaml_editor()


def _render_guided_builder():
    """Step-by-step form that generates the workflow YAML."""
    st.caption("Fill in the form below — the YAML is generated automatically.")

    # ── Step 1: Basic info ────────────────────────────────────────────────────
    st.markdown("### 1 · Workflow info")
    col_name, col_desc = st.columns([2, 3])
    with col_name:
        wf_name = st.text_input(
            "Name *",
            placeholder="e.g. pr-review-workflow",
            help="Lowercase, hyphens only. Used to trigger the workflow via API.",
            key="builder_name",
        )
    with col_desc:
        wf_desc = st.text_input(
            "Description",
            placeholder="One-line summary of what this workflow does",
            key="builder_desc",
        )

    # ── Step 2: Steps ─────────────────────────────────────────────────────────
    st.markdown("### 2 · Steps")
    st.caption(
        "Add one or more steps. Each step can be an **agent** (AI agent with a role), "
        "an **action** (call a built-in integration like GitHub), or a "
        "**human checkpoint** (pause and wait for a human response)."
    )

    if "builder_steps" not in st.session_state:
        st.session_state["builder_steps"] = [_empty_step(0)]

    steps = st.session_state["builder_steps"]

    for i, step in enumerate(steps):
        with st.container(border=True):
            hdr, del_col = st.columns([10, 1])
            hdr.markdown(f"**Step {i + 1}**")
            if del_col.button("✕", key=f"del_step_{i}", help="Remove this step") and len(steps) > 1:
                steps.pop(i)
                st.rerun()

            c1, c2, c3 = st.columns([2, 2, 3])
            step["id"] = c1.text_input(
                "Step ID",
                value=step.get("id", f"step_{i+1}"),
                key=f"step_id_{i}",
                help="Unique identifier — used in `{{ steps.STEP_ID.output }}`",
            )
            step["type"] = c2.selectbox(
                "Type",
                _STEP_TYPES,
                index=_STEP_TYPES.index(step.get("type", "agent")),
                key=f"step_type_{i}",
            )

            # Depends on (multi-select of previous step IDs)
            prev_ids = [s["id"] for s in steps[:i] if s.get("id")]
            if prev_ids:
                step["depends_on"] = c3.multiselect(
                    "Depends on",
                    options=prev_ids,
                    default=[d for d in step.get("depends_on", []) if d in prev_ids],
                    key=f"step_deps_{i}",
                )
            else:
                step["depends_on"] = []

            # Type-specific fields
            if step["type"] == "agent":
                role_idx = _AGENT_ROLES.index(step.get("role", "searcher")) if step.get("role") in _AGENT_ROLES else 0
                role = st.selectbox(
                    "Agent role",
                    _AGENT_ROLES,
                    index=role_idx,
                    key=f"step_role_{i}",
                    format_func=lambda r: f"{r}  —  {_ROLE_DESCRIPTIONS[r]}",
                )
                step["role"] = role
                step["task"] = st.text_area(
                    "Task / prompt",
                    value=step.get("task", ""),
                    height=80,
                    key=f"step_task_{i}",
                    placeholder="Describe what this agent should do. You can use {{ trigger.repo_owner }}, {{ steps.prev_step.output }}, etc.",
                )

            elif step["type"] == "action":
                step["action"] = st.text_input(
                    "Action",
                    value=step.get("action", ""),
                    key=f"step_action_{i}",
                    placeholder="e.g. github.get_pr_diff",
                    help="Built-in integration action to call.",
                )
                step["params_raw"] = st.text_area(
                    "Parameters (YAML key: value)",
                    value=step.get("params_raw", "repo_owner: \"{{ trigger.repo_owner }}\"\nrepo_name: \"{{ trigger.repo_name }}\""),
                    height=80,
                    key=f"step_params_{i}",
                )

            elif step["type"] == "human_checkpoint":
                step["task"] = st.text_area(
                    "Prompt shown to human",
                    value=step.get("task", ""),
                    height=60,
                    key=f"step_cp_prompt_{i}",
                    placeholder="What question or decision do you want the human to respond to?",
                )
                step["options_raw"] = st.text_input(
                    "Response options (comma-separated, leave blank for free text)",
                    value=step.get("options_raw", ""),
                    key=f"step_cp_opts_{i}",
                    placeholder="e.g. approve, reject, needs changes",
                )

    if st.button("➕ Add step", use_container_width=False):
        steps.append(_empty_step(len(steps)))
        st.rerun()

    # ── Preview + Save ────────────────────────────────────────────────────────
    st.markdown("### 3 · Preview & Save")
    yaml_preview = _steps_to_yaml(wf_name, wf_desc, steps)
    st.code(yaml_preview, language="yaml")

    if st.button("💾 Save Workflow", type="primary", use_container_width=True, key="builder_save"):
        if not wf_name.strip():
            st.error("Workflow name is required.")
        elif not any(s.get("id") for s in steps):
            st.error("Add at least one step.")
        else:
            ok, result = _api(
                "post",
                "/workflows",
                json={"name": wf_name.strip(), "yaml_definition": yaml_preview, "description": wf_desc},
            )
            if ok:
                st.success(f"✅ Workflow **{wf_name}** saved! Go to **My Workflows** to run it.")
                del st.session_state["builder_steps"]
            else:
                st.error(f"Save failed: {result}")


def _render_yaml_editor():
    """Raw YAML editor for power users."""
    st.caption("Write the workflow YAML directly. Refer to the cheatsheet below for variable names.")

    with st.expander("📖 Template variables", expanded=False):
        st.markdown("""
| Variable | What it contains |
|---|---|
| `{{ trigger.repo_owner }}` | GitHub org / username set at run time |
| `{{ trigger.repo_name }}` | Repository name set at run time |
| `{{ trigger.pr_number }}` | PR number (if passed in trigger payload) |
| `{{ context.KEY }}` | A global workflow context value |
| `{{ steps.STEP_ID.output }}` | The text output of a previous step |
        """)

    description = st.text_input(
        "Description (optional)",
        placeholder="One-line summary",
        key="yaml_editor_desc",
    )

    yaml_text = st.text_area(
        "YAML Definition",
        value=st.session_state.get("new_wf_yaml", _SAMPLE_YAML),
        height=500,
        key="yaml_editor_text",
    )

    col_save, col_reset = st.columns([3, 1])
    with col_reset:
        if st.button("Reset to sample", use_container_width=True):
            st.session_state["new_wf_yaml"] = _SAMPLE_YAML
            st.rerun()
    with col_save:
        if st.button("💾 Save Workflow", type="primary", use_container_width=True, key="yaml_save"):
            name = _extract_name_from_yaml(yaml_text)
            if not yaml_text.strip():
                st.error("YAML cannot be empty.")
            elif not name:
                st.error("No `name:` field found in the YAML.")
            else:
                ok, result = _api(
                    "post",
                    "/workflows",
                    json={"name": name, "yaml_definition": yaml_text, "description": description},
                )
                if ok:
                    st.success(f"✅ Workflow **{name}** saved!")
                    st.session_state.pop("new_wf_yaml", None)
                else:
                    st.error(f"Save failed: {result}")


# ── Guided builder helpers ─────────────────────────────────────────────────────

def _empty_step(index: int) -> dict:
    return {"id": f"step_{index + 1}", "type": "agent", "role": "searcher", "task": "", "depends_on": []}


def _yaml_scalar(value: str) -> str:
    """
    Return a safely-quoted YAML scalar.
    Uses block literal style (|) for multi-line, single-quoted style for
    strings containing special chars (: { } [ ] , # & * ? | > ! % @ `),
    and plain style otherwise.
    Single-quote style escapes any embedded single-quote by doubling it.
    """
    if "\n" in value:
        # Caller handles block scalars separately
        return value
    # Characters that require quoting in YAML plain scalars
    _NEEDS_QUOTE = set(r':{}[],#&*?|>!%@`"')
    if any(c in _NEEDS_QUOTE for c in value) or value.startswith("-") or not value:
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return value


def _steps_to_yaml(name: str, description: str, steps: list[dict]) -> str:
    """Convert the guided form state into a valid workflow YAML string."""
    lines = [
        f"name: {_yaml_scalar(name or 'my-workflow')}",
        f"description: {_yaml_scalar(description or '')}",
        "",
        "trigger:",
        "  type: manual",
        "",
        "steps:",
    ]

    for step in steps:
        sid = step.get("id", "step")
        stype = step.get("type", "agent")
        deps = step.get("depends_on", [])

        lines.append(f"  - id: {sid}")
        lines.append(f"    type: {stype}")

        if deps:
            lines.append(f"    depends_on: [{', '.join(deps)}]")

        if stype == "agent":
            lines.append(f"    role: {step.get('role', 'searcher')}")
            task = step.get("task", "").strip()
            if task:
                if "\n" in task:
                    lines.append("    task: |")
                    for tl in task.splitlines():
                        # Preserve the original indentation; just prepend 6 spaces
                        lines.append(f"      {tl.rstrip()}")
                else:
                    lines.append(f"    task: {_yaml_scalar(task)}")

        elif stype == "action":
            lines.append(f"    action: {step.get('action', '')}")
            params_raw = step.get("params_raw", "").strip()
            if params_raw:
                lines.append("    params:")
                for pl in params_raw.splitlines():
                    lines.append(f"      {pl}")

        elif stype == "human_checkpoint":
            task = step.get("task", "").strip()
            if task:
                lines.append(f"    prompt: {_yaml_scalar(task)}")
            # Strip any surrounding quotes the user may have typed, then de-dup
            raw_opts = step.get("options_raw", "")
            opts = [o.strip().strip("\"'") for o in raw_opts.split(",") if o.strip().strip("\"'")]
            if opts:
                # Use block sequence style — one option per line, avoids flow-
                # sequence issues with dashes, colons, and em-dashes inside values
                lines.append("    options:")
                for opt in opts:
                    lines.append(f"      - {_yaml_scalar(opt)}")

        lines.append("")  # blank line between steps

    return "\n".join(lines)


# ── Tab 3: Run History ────────────────────────────────────────────────────────

_STEP_TYPE_LABEL = {
    "agent":            "🤖 Agent",
    "action":           "⚙️ Action",
    "human_checkpoint": "🧑 Checkpoint",
}

_STATUS_BG = {
    "completed":     "#1a3a2a",
    "failed":        "#3a1a1a",
    "running":       "#1a2a3a",
    "pending":       "#2a2a1a",
    "waiting_human": "#2a1a3a",
    "skipped":       "#2a2a2a",
    "cancelled":     "#2a2a2a",
}

_STATUS_PILL = {
    "completed":     ("🟢", "completed"),
    "failed":        ("🔴", "failed"),
    "running":       ("🔵", "running"),
    "pending":       ("🟡", "pending"),
    "waiting_human": ("🟠", "waiting"),
    "skipped":       ("⚪", "skipped"),
    "cancelled":     ("⚫", "cancelled"),
}


def _pill(status: str) -> str:
    icon, label = _STATUS_PILL.get(status, ("❓", status))
    return f"{icon} **{label}**"


def _duration(started: str | None, completed: str | None) -> str:
    """Return a human-readable duration string, or empty string."""
    if not started or not completed:
        return ""
    try:
        from datetime import datetime
        fmt = "%Y-%m-%dT%H:%M:%S"
        s = datetime.fromisoformat(started[:19])
        e = datetime.fromisoformat(completed[:19])
        secs = int((e - s).total_seconds())
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
    except Exception:
        return ""


def _render_run_history():
    st.subheader("Run History")

    ok, workflows = _api("get", "/workflows?active_only=false")
    wf_options = {wf["name"]: wf["id"] for wf in (workflows if ok and workflows else [])}

    filter_col, limit_col, refresh_col = st.columns([4, 1, 1])
    with filter_col:
        selected_wf = st.selectbox(
            "Filter by workflow",
            options=["All"] + list(wf_options.keys()),
            label_visibility="collapsed",
        )
    with limit_col:
        limit = st.number_input("Runs", min_value=5, max_value=100, value=20, step=5,
                                label_visibility="collapsed")
    with refresh_col:
        if st.button("🔄", help="Refresh", use_container_width=True):
            st.rerun()

    wf_id = wf_options.get(selected_wf) if selected_wf != "All" else None
    params = f"?limit={int(limit)}"
    if wf_id:
        params += f"&workflow_id={wf_id}"
    ok2, runs = _api("get", f"/workflows/runs{params}")
    if not ok2:
        st.error(f"Could not fetch runs: {runs}")
        return
    if not runs:
        st.info("No runs found.")
        return

    last_id = st.session_state.get("last_run_id")

    for run in runs:
        run_id  = run["id"]
        status  = run.get("status", "unknown")
        icon, _ = _STATUS_PILL.get(status, ("❓", status))
        wf_name = run.get("workflow_name", "?")
        started = (run.get("started_at") or "")[:19].replace("T", " ")
        tokens  = run.get("total_tokens_used", 0)
        dur     = _duration(run.get("started_at"), run.get("completed_at"))

        new_tag  = " 🆕" if run_id == last_id else ""
        dur_tag  = f"  ·  ⏱ {dur}" if dur else ""
        tok_tag  = f"  ·  🔢 {tokens:,} tokens" if tokens else ""
        label    = f"{icon} **{wf_name}**{new_tag}  ·  {started}{dur_tag}{tok_tag}"

        with st.expander(label, expanded=(run_id == last_id)):
            _render_run_detail(run_id, status)


def _render_run_detail(run_id: str, list_status: str):
    """Render the full step-by-step breakdown for a single run."""
    ok, full = _api("get", f"/workflows/runs/{run_id}")
    if not ok:
        st.warning(f"Could not fetch run details: {full}")
        return

    status = full.get("status", list_status)

    # ── Run-level summary bar ─────────────────────────────────────────────────
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Status",  _pill(status))
    m2.metric("Tokens",  f"{full.get('total_tokens_used', 0):,}")
    started   = (full.get("started_at")   or "")[:19].replace("T", " ")
    completed = (full.get("completed_at") or "")[:19].replace("T", " ")
    m3.metric("Started",    started   or "—")
    m4.metric("Completed",  completed or "—")

    if full.get("error_message"):
        st.error(f"**Run error:** {full['error_message']}")

    # ── Trigger payload ───────────────────────────────────────────────────────
    trigger_payload = full.get("trigger_payload") or {}
    if trigger_payload:
        with st.expander("📥 Trigger payload", expanded=False):
            import json as _json
            st.code(_json.dumps(trigger_payload, indent=2), language="json")

    # ── Steps ─────────────────────────────────────────────────────────────────
    steps = full.get("steps", [])
    if not steps:
        st.caption("No step data yet.")
    else:
        st.markdown("---")
        st.markdown("#### Steps")
        for step in steps:
            _render_step_card(run_id, step)

    # ── Pending checkpoints ───────────────────────────────────────────────────
    checkpoints = full.get("checkpoints", [])
    waiting = [c for c in checkpoints if c.get("status") == "waiting"]
    if waiting:
        st.markdown("---")
        st.subheader("⏳ Waiting for your input")
        for cp in waiting:
            with st.container(border=True):
                st.info(cp.get("prompt", "Please review and respond."))
                options = cp.get("options", [])
                if options:
                    response = st.radio("Choose a response", options,
                                        key=f"cp_radio_{cp['id']}")
                else:
                    response = st.text_input("Your response",
                                             key=f"cp_text_{cp['id']}")
                if st.button("✅ Submit response", key=f"cp_submit_{cp['id']}",
                             use_container_width=True):
                    ok2, res = _api(
                        "post",
                        f"/workflows/checkpoints/{cp['id']}/respond",
                        json={"response": response},
                    )
                    if ok2:
                        st.success("Response submitted — workflow will continue.")
                        st.rerun()
                    else:
                        st.error(f"Failed: {res}")

    # ── Refresh for active runs ───────────────────────────────────────────────
    if status in ("running", "pending", "waiting_human"):
        st.markdown("")
        if st.button("🔄 Refresh run", key=f"refresh_{run_id}", use_container_width=True):
            st.rerun()


def _render_step_card(run_id: str, step: dict):
    """Render a single step as a clean bordered card with full markdown output."""
    s_status   = step.get("status", "unknown")
    s_id       = step.get("step_id", "?")
    s_role     = step.get("agent_role") or ""
    s_type     = step.get("step_type", "agent")
    s_tokens   = step.get("tokens_used", 0)
    s_retries  = step.get("retry_count", 0)
    s_err      = step.get("error_message", "")
    s_started  = (step.get("started_at")   or "")[:19].replace("T", " ")
    s_done     = (step.get("completed_at") or "")[:19].replace("T", " ")
    dur        = _duration(step.get("started_at"), step.get("completed_at"))

    icon, _    = _STATUS_PILL.get(s_status, ("❓", s_status))
    type_label = _STEP_TYPE_LABEL.get(s_type, s_type)
    role_label = f" · **{s_role}**" if s_role else ""
    tok_label  = f" · 🔢 {s_tokens:,} tok" if s_tokens else ""
    dur_label  = f" · ⏱ {dur}" if dur else ""
    ret_label  = f" · 🔁 {s_retries} retries" if s_retries else ""

    header = f"{icon} `{s_id}` — {type_label}{role_label}{tok_label}{dur_label}{ret_label}"

    # Auto-expand failed steps; collapse completed ones to keep the page tidy
    auto_open = s_status in ("failed", "running")

    with st.expander(header, expanded=auto_open):
        # Timing row
        if s_started:
            t1, t2 = st.columns(2)
            t1.caption(f"Started:   {s_started}")
            t2.caption(f"Completed: {s_done or '—'}")

        # Error banner
        if s_err:
            st.error(f"**Error:** {s_err}")

        # ── Output ───────────────────────────────────────────────────────────
        output = step.get("output")
        if output:
            text = (
                output.get("text")
                or output.get("response")
                or output.get("answer")
                or None
            )
            if text is None and isinstance(output, dict):
                import json as _json
                text = _json.dumps(output, indent=2)

            if text:
                st.markdown("**Output**")
                # Render full markdown — no truncation, no disabled textarea
                st.markdown(
                    f'<div style="'
                    f'background:#0e1117;'
                    f'border:1px solid #2a2a3a;'
                    f'border-radius:8px;'
                    f'padding:1rem 1.25rem;'
                    f'font-size:0.88rem;'
                    f'line-height:1.6;'
                    f'overflow-wrap:break-word;'
                    f'max-height:none;'
                    f'">{_md_to_html(text)}</div>',
                    unsafe_allow_html=True,
                )

            # ── PDF download buttons ──────────────────────────────────────────
            documents = output.get("documents", []) if isinstance(output, dict) else []
            if documents:
                st.markdown("")
                for doc in documents:
                    doc_id = doc.get("doc_id", "")
                    filename = doc.get("filename", "document.pdf")
                    size_kb = (doc.get("size_bytes") or 0) // 1024
                    size_label = f" ({size_kb} KB)" if size_kb else ""
                    try:
                        pdf_resp = requests.get(
                            f"{_base()}/documents/{doc_id}/download",
                            timeout=30,
                        )
                        if pdf_resp.ok:
                            st.download_button(
                                label=f"📥 Download PDF — {filename}{size_label}",
                                data=pdf_resp.content,
                                file_name=filename,
                                mime="application/pdf",
                                key=f"dl_{doc_id}",
                            )
                        else:
                            st.warning(f"PDF not available: {filename}")
                    except Exception as _dl_exc:
                        st.warning(f"Could not fetch PDF {filename}: {_dl_exc}")

        elif s_status == "completed" and not output:
            st.caption("✓ Completed with no text output.")


def _md_to_html(text: str) -> str:
    """
    Convert markdown to HTML for rendering inside st.markdown(unsafe_allow_html=True).
    Falls back to pre-formatted plain text if the markdown library is not available.
    """
    try:
        import markdown as _markdown
        return _markdown.markdown(
            text,
            extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        )
    except ImportError:
        pass
    # Fallback: wrap in <pre> so whitespace/newlines are preserved
    escaped = (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"<pre style='white-space:pre-wrap;margin:0'>{escaped}</pre>"


# ── Utilities ─────────────────────────────────────────────────────────────────

def _extract_name_from_yaml(yaml_text: str) -> str:
    """Quick parse of the `name:` field without importing pyyaml."""
    for line in yaml_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            return stripped[5:].strip().strip("\"'")
    return ""
