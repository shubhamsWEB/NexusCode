"""
Agent Roles page — view, create, edit, and delete agent role configurations.

Each role defines:
  - system_prompt    : the agent's core identity and instructions
  - instructions     : extra rules appended after the system prompt
  - default_tools    : which codebase retrieval tools are available
  - require_search   : grounding gate (must search before answering)
  - max_iterations   : agent loop turns before forcing final answer
  - token_budget     : cumulative tool-result token limit
"""
from __future__ import annotations

import requests
import streamlit as st

_BASE = "http://localhost:8000"

_ROLE_ICONS = {
    "searcher":   "🔍",
    "planner":    "📋",
    "reviewer":   "🔎",
    "coder":      "💻",
    "tester":     "🧪",
    "supervisor": "🎯",
}


# ── API helpers ───────────────────────────────────────────────────────────────


def _api(method: str, path: str, **kwargs):
    try:
        resp = getattr(requests, method)(f"{_BASE}{path}", timeout=15, **kwargs)
        if resp.ok:
            return True, resp.json()
        return False, resp.json().get("detail", resp.text)
    except Exception as exc:
        return False, str(exc)


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_tools() -> dict:
    """Fetch tool catalogue from API. Returns {internal: [...], external: [...]}."""
    ok, data = _api("get", "/agent-roles/tools")
    if ok and isinstance(data, dict):
        return data
    return {"internal": [], "external": []}


def _tool_lookup(tools_data: dict) -> dict[str, dict]:
    """Build a flat name→{description, source, server_url} map for quick access."""
    lookup: dict[str, dict] = {}
    for t in tools_data.get("internal", []):
        lookup[t["name"]] = t
    for t in tools_data.get("external", []):
        lookup[t["name"]] = t
    return lookup


# ── Main render ───────────────────────────────────────────────────────────────


def render():
    st.title("🤖 Agent Roles")
    st.caption(
        "Each workflow step runs under an agent role. "
        "A role defines the system prompt, available tools, and loop constraints. "
        "Built-in roles can be **overridden** without losing the original defaults. "
        "Custom roles can be freely created and used in workflow YAML with `role: my-role-name`."
    )

    ok, roles = _api("get", "/agent-roles")
    if not ok:
        st.error(f"Could not load agent roles: {roles}")
        st.info("Make sure the API is running and the DB migration `012_agent_roles.sql` has been applied.")
        return

    tab_all, tab_new = st.tabs(["🤖 All Agents", "➕ New Agent"])

    with tab_all:
        _render_role_list(roles)

    with tab_new:
        _render_new_role_form()


# ── Role list ─────────────────────────────────────────────────────────────────


def _render_role_list(roles: list):
    if not roles:
        st.info("No agent roles found.")
        return

    builtins = [r for r in roles if r.get("is_builtin")]
    custom   = [r for r in roles if not r.get("is_builtin")]

    st.markdown("#### Built-in Roles")
    st.caption("Provided by default. Override to customise without losing the originals.")
    for role in builtins:
        _render_role_card(role)

    if custom:
        st.markdown("#### Custom Roles")
        st.caption("Fully managed by you. Can be deleted when no longer needed.")
        for role in custom:
            _render_role_card(role)


def _render_role_card(role: dict):
    name          = role["name"]
    display_name  = role.get("display_name") or name.replace("-", " ").title()
    is_builtin    = role.get("is_builtin", False)
    is_overridden = role.get("is_overridden", False)
    is_active     = role.get("is_active", True)

    icon = _ROLE_ICONS.get(name, "🤖")
    tags = []
    if is_builtin and not is_overridden:
        tags.append("🔒 built-in")
    elif is_overridden:
        tags.append("✏️ overridden")
    else:
        tags.append("⚙️ custom")
    if not is_active:
        tags.append("⚫ inactive")
    tag_str = "  ·  " + "  ·  ".join(tags) if tags else ""

    label    = f"{icon} **{display_name}**{tag_str}"
    edit_key = f"edit_role_{name}"

    with st.expander(label, expanded=st.session_state.get(edit_key, False)):
        if not st.session_state.get(edit_key, False):
            _render_view_mode(role, edit_key)
        else:
            _render_edit_form(role, edit_key)


# ── View mode ─────────────────────────────────────────────────────────────────


def _render_view_mode(role: dict, edit_key: str):
    name          = role["name"]
    description   = role.get("description") or ""
    is_builtin    = role.get("is_builtin", False)
    is_overridden = role.get("is_overridden", False)
    tools         = role.get("default_tools") or []

    if description:
        st.caption(description)

    m1, m2, m3 = st.columns(3)
    m1.metric("Max iterations", role.get("max_iterations", 5))
    m2.metric("Token budget",   f"{role.get('token_budget', 80_000):,}")
    m3.metric("Require search", "✅ Yes" if role.get("require_search", True) else "❌ No")

    if tools:
        # Annotate external tools with a server tag
        tools_data  = _fetch_tools()
        tool_lookup = _tool_lookup(tools_data)
        badges = []
        for t in tools:
            info = tool_lookup.get(t, {})
            if info.get("source") == "external":
                badges.append(f"`{t}` 🔌")
            else:
                badges.append(f"`{t}`")
        st.markdown("**Tools:** " + "  ".join(badges))
    else:
        st.caption("No tools assigned.")

    sp = role.get("system_prompt", "")
    if sp:
        with st.expander("System prompt", expanded=False):
            st.markdown(sp)

    instructions = role.get("instructions", "")
    if instructions:
        with st.expander("Additional instructions", expanded=False):
            st.markdown(instructions)

    st.markdown("")
    b1, b2, b3 = st.columns(3)

    with b1:
        if st.button("✏️ Edit", key=f"view_edit_{name}", use_container_width=True):
            st.session_state[edit_key] = True
            st.rerun()

    with b2:
        if is_overridden:
            with st.popover("↩ Reset to default", use_container_width=True):
                st.warning("This removes your customisations and restores the built-in defaults.")
                if st.button("Confirm reset", key=f"reset_confirm_{name}",
                             use_container_width=True):
                    ok2, _ = _api("post", f"/agent-roles/{name}/reset")
                    if ok2:
                        st.success("Reset to defaults.")
                        st.rerun()
                    else:
                        st.error("Reset failed.")

    with b3:
        if not is_builtin:
            with st.popover("🗑 Delete", use_container_width=True):
                st.warning("Permanently deletes this custom agent role.")
                if st.button("Confirm delete", key=f"del_confirm_{name}",
                             use_container_width=True):
                    ok3, _ = _api("delete", f"/agent-roles/{name}")
                    if ok3:
                        st.success("Deleted.")
                        st.rerun()
                    else:
                        st.error("Delete failed.")


# ── Tool selector (shared by edit + new forms) ────────────────────────────────


def _render_tool_selector(current_tools: set[str], key_prefix: str) -> list[str]:
    """
    Render tool checkboxes split into Internal and External (MCP) sections.
    Returns the list of selected tool names.
    """
    tools_data  = _fetch_tools()
    internal    = tools_data.get("internal", [])
    external    = tools_data.get("external", [])
    selected: list[str] = []

    # Internal tools
    st.markdown("**Internal tools** — always available codebase retrieval")
    if internal:
        icols = st.columns(2)
        for i, t in enumerate(internal):
            with icols[i % 2]:
                if st.checkbox(
                    f"`{t['name']}`",
                    value=(t["name"] in current_tools),
                    key=f"{key_prefix}_{t['name']}",
                    help=t.get("description", ""),
                ):
                    selected.append(t["name"])
    else:
        st.caption("No internal tools found.")

    # External tools
    st.markdown("**External tools** — MCP servers 🔌")
    if external:
        ecols = st.columns(2)
        for i, t in enumerate(external):
            server_url = t.get("server_url") or ""
            short_url  = server_url.split("//")[-1][:40] if server_url else ""
            label_help = f"{t.get('description', '')}  \n_Server: {short_url}_" if short_url else t.get("description", "")
            with ecols[i % 2]:
                if st.checkbox(
                    f"`{t['name']}` 🔌",
                    value=(t["name"] in current_tools),
                    key=f"{key_prefix}_{t['name']}",
                    help=label_help,
                ):
                    selected.append(t["name"])
    else:
        st.caption(
            "No external MCP tools connected. "
            "Add servers on the **🔌 MCP Servers** page to use them here."
        )

    return selected


# ── Edit form ─────────────────────────────────────────────────────────────────


def _render_edit_form(role: dict, edit_key: str):
    name         = role["name"]
    display_name = role.get("display_name") or name.replace("-", " ").title()

    st.markdown(f"### ✏️ Edit — {display_name}")

    ca, cb = st.columns([2, 3])
    with ca:
        new_display = st.text_input(
            "Display name",
            value=display_name,
            key=f"ef_disp_{name}",
        )
    with cb:
        new_desc = st.text_input(
            "Description",
            value=role.get("description") or "",
            key=f"ef_desc_{name}",
            placeholder="One-line summary of what this agent does",
        )

    new_prompt = st.text_area(
        "System prompt",
        value=role.get("system_prompt") or "",
        height=340,
        key=f"ef_prompt_{name}",
        help="Core identity and instructions. Shapes everything the agent does.",
    )

    new_instructions = st.text_area(
        "Additional instructions",
        value=role.get("instructions") or "",
        height=120,
        key=f"ef_inst_{name}",
        placeholder="Optional extra rules appended after the system prompt (output format, constraints, etc.).",
        help="Appended to the system prompt with a section separator.",
    )

    st.markdown("**Available tools**")
    current_tools  = set(role.get("default_tools") or [])
    selected_tools = _render_tool_selector(current_tools, key_prefix=f"ef_tool_{name}")

    st.markdown("")
    cc1, cc2, cc3 = st.columns(3)
    new_rs = cc1.toggle(
        "Require search before answer",
        value=role.get("require_search", True),
        key=f"ef_rs_{name}",
        help="Must call a search tool before answering. Prevents training-data-only responses.",
    )
    new_iter = cc2.number_input(
        "Max iterations",
        min_value=1, max_value=20,
        value=int(role.get("max_iterations") or 5),
        key=f"ef_iter_{name}",
        help="Maximum agent loop turns (search → answer cycles).",
    )
    new_budget = cc3.number_input(
        "Token budget",
        min_value=5_000, max_value=500_000, step=5_000,
        value=int(role.get("token_budget") or 80_000),
        key=f"ef_budget_{name}",
        help="Cumulative tool-result token limit before forcing final answer.",
    )

    new_active = st.toggle(
        "Active",
        value=role.get("is_active", True),
        key=f"ef_active_{name}",
    )

    st.markdown("")
    sv, ca_ = st.columns(2)
    with sv:
        if st.button("💾 Save", key=f"ef_save_{name}", use_container_width=True,
                     type="primary"):
            if not new_prompt.strip():
                st.error("System prompt cannot be empty.")
            else:
                ok, result = _api(
                    "put",
                    f"/agent-roles/{name}",
                    json={
                        "display_name":   new_display,
                        "description":    new_desc,
                        "system_prompt":  new_prompt,
                        "instructions":   new_instructions,
                        "default_tools":  selected_tools,
                        "require_search": new_rs,
                        "max_iterations": int(new_iter),
                        "token_budget":   int(new_budget),
                        "is_active":      new_active,
                    },
                )
                if ok:
                    st.success(f"✅ **{name}** saved.")
                    st.session_state[edit_key] = False
                    st.rerun()
                else:
                    st.error(f"Save failed: {result}")
    with ca_:
        if st.button("Cancel", key=f"ef_cancel_{name}", use_container_width=True):
            st.session_state[edit_key] = False
            st.rerun()


# ── New role form ─────────────────────────────────────────────────────────────


def _render_new_role_form():
    st.subheader("Create a New Agent Role")
    st.caption(
        "Custom roles are available immediately in workflow YAML: `role: your-role-name`. "
        "Use lowercase, hyphen-separated names."
    )

    cn, cd = st.columns([2, 3])
    with cn:
        new_name = st.text_input(
            "Role name *",
            key="nr_name",
            placeholder="e.g. security-auditor",
            help="Lowercase + hyphens. Used in workflow YAML: `role: security-auditor`.",
        )
    with cd:
        new_display = st.text_input(
            "Display name",
            key="nr_display",
            placeholder="e.g. Security Auditor",
        )

    new_desc = st.text_input(
        "Description",
        key="nr_desc",
        placeholder="One-line summary of what this agent does",
    )

    new_prompt = st.text_area(
        "System prompt *",
        key="nr_prompt",
        height=280,
        placeholder=(
            "You are a specialized [Role Name] agent. Your mission is...\n\n"
            "- Core capability 1\n"
            "- Core capability 2\n"
            "- Core capability 3\n\n"
            "Always [key behaviour]. Cite every file you reference.\n\n"
            "Output format: ..."
        ),
    )

    new_instructions = st.text_area(
        "Additional instructions (optional)",
        key="nr_instructions",
        height=100,
        placeholder="Extra constraints or output format rules appended after the system prompt.",
    )

    # Fetch available tools and pre-select all internal ones by default
    tools_data   = _fetch_tools()
    internal     = tools_data.get("internal", [])
    default_on   = {t["name"] for t in internal}  # internal tools on by default

    st.markdown("**Select tools**")
    selected_new = _render_tool_selector(default_on, key_prefix="nr_tool")

    st.markdown("")
    nc1, nc2, nc3 = st.columns(3)
    nr_rs     = nc1.toggle("Require search", value=True, key="nr_rs")
    nr_iter   = nc2.number_input("Max iterations", min_value=1, max_value=20,
                                  value=5, key="nr_iter")
    nr_budget = nc3.number_input("Token budget", min_value=5_000, max_value=500_000,
                                  step=5_000, value=80_000, key="nr_budget")

    st.markdown("")
    if st.button("💾 Create Agent Role", type="primary", use_container_width=True,
                 key="nr_create"):
        name_clean = (new_name or "").strip().lower().replace(" ", "-")
        if not name_clean:
            st.error("Role name is required.")
        elif not new_prompt.strip():
            st.error("System prompt is required.")
        else:
            ok, result = _api(
                "put",
                f"/agent-roles/{name_clean}",
                json={
                    "display_name":   new_display or name_clean.replace("-", " ").title(),
                    "description":    new_desc,
                    "system_prompt":  new_prompt,
                    "instructions":   new_instructions,
                    "default_tools":  selected_new,
                    "require_search": nr_rs,
                    "max_iterations": int(nr_iter),
                    "token_budget":   int(nr_budget),
                    "is_active":      True,
                },
            )
            if ok:
                st.success(
                    f"✅ Agent role **{name_clean}** created! "
                    f"Use it in workflow YAML with `role: {name_clean}`."
                )
                st.rerun()
            else:
                st.error(f"Failed: {result}")
