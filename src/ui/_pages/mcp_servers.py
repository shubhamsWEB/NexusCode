"""
🔌 External MCP Servers — dashboard management page.

Cursor-style UX: type-aware forms, guided auth, stdio subprocess support,
OAuth browser flow with PKCE.
"""

from __future__ import annotations

import streamlit as st
from typing import Any

from src.ui.helpers import api_delete, api_get, api_patch, api_post, time_ago

# ── Quick presets ──────────────────────────────────────────────────────────────

_PRESETS: list[dict[str, Any]] = [
    {
        "label": "GitHub",
        "icon": "🐙",
        "server_type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env_hint": "GITHUB_PERSONAL_ACCESS_TOKEN",
        "description": "GitHub repos, issues, PRs via MCP",
        "auth_type": "none",
    },
    {
        "label": "Filesystem",
        "icon": "📁",
        "server_type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
        "env_hint": "",
        "description": "Read/write local files",
        "auth_type": "none",
    },
    {
        "label": "PostgreSQL",
        "icon": "🐘",
        "server_type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
        "env_hint": "DATABASE_URL",
        "description": "Query PostgreSQL via MCP",
        "auth_type": "none",
    },
    {
        "label": "Context7",
        "icon": "📚",
        "server_type": "remote",
        "url": "https://mcp.context7.com/mcp",
        "transport": "streamable_http",
        "auth_type": "bearer",
        "description": "Up-to-date library documentation",
    },
    {
        "label": "Atlassian",
        "icon": "🔷",
        "server_type": "remote",
        "url": "https://mcp.atlassian.com/v1/mcp",
        "transport": "streamable_http",
        "auth_type": "basic",
        "description": "Jira + Confluence (email:api_token)",
    },
    {
        "label": "Linear",
        "icon": "⚡",
        "server_type": "remote",
        "url": "https://mcp.linear.app/mcp",
        "transport": "streamable_http",
        "auth_type": "bearer",
        "description": "Linear issues and projects",
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────


def _mask_auth(value: str | None) -> str:
    if not value:
        return "—"
    return f"...{value[-4:]}" if len(value) > 4 else "****"


def _status_icon(server: dict) -> str:
    if server.get("last_error") is None and server.get("last_seen_at"):
        return "🟢"
    if server.get("last_seen_at") is None and server.get("last_error") is None:
        return "⬜"
    return "🔴"


def _type_badge(server: dict) -> str:
    return "⚡ stdio" if server.get("server_type") == "stdio" else "🌐 Remote"


def _auth_badge(server: dict) -> str:
    t = server.get("auth_type", "header")
    return {
        "bearer": "🔑 Bearer",
        "basic": "👤 Basic",
        "oauth": "🔗 OAuth",
        "none": "🔓 None",
        "header": "⚿ Custom",
    }.get(t, "⚿ Custom")


def _server_identifier(server: dict) -> str:
    if server.get("server_type") == "stdio":
        cmd = server.get("command") or ""
        args = server.get("args") or []
        preview = cmd
        if args:
            preview += f" {args[0]}"
        return f"`{preview}`"
    return f"`{server.get('url', '')}`"


def _env_editor(key_prefix: str, initial: dict | None = None) -> dict:
    """Render a key-value env var editor, return current dict."""
    if f"{key_prefix}_rows" not in st.session_state:
        initial_rows = list((initial or {}).items()) if initial else []
        st.session_state[f"{key_prefix}_rows"] = initial_rows if initial_rows else [("", "")]

    rows: list[tuple[str, str]] = st.session_state[f"{key_prefix}_rows"]
    updated_rows: list[tuple[str, str]] = []

    for i, (k, v) in enumerate(rows):
        c1, c2, c3 = st.columns([3, 4, 0.5])
        with c1:
            new_k = st.text_input("Key", value=k, key=f"{key_prefix}_k_{i}", label_visibility="collapsed", placeholder="ENV_VAR")
        with c2:
            new_v = st.text_input("Value", value=v, key=f"{key_prefix}_v_{i}", label_visibility="collapsed", placeholder="value", type="password")
        with c3:
            if st.button("−", key=f"{key_prefix}_rm_{i}"):
                st.session_state[f"{key_prefix}_rows"] = [(kk, vv) for j, (kk, vv) in enumerate(rows) if j != i]
                st.rerun()
        updated_rows.append((new_k, new_v))

    st.session_state[f"{key_prefix}_rows"] = updated_rows

    if st.button("+ Add variable", key=f"{key_prefix}_add"):
        st.session_state[f"{key_prefix}_rows"].append(("", ""))
        st.rerun()

    return {k: v for k, v in updated_rows if k.strip()}


# ── Server list item ───────────────────────────────────────────────────────────


def _render_server(server: dict) -> None:
    sid = server["id"]
    icon = _status_icon(server)
    enabled = server.get("enabled", True)
    tool_count = server.get("tool_count", 0)
    last_seen = time_ago(server.get("last_seen_at"))
    last_error = server.get("last_error")
    auth_type = server.get("auth_type", "header")

    with st.container(border=True):
        top_left, top_right = st.columns([4, 2])

        with top_left:
            transport = server.get("transport", "auto")
            transport_badge = {
                "streamable_http": "🌐 HTTP",
                "sse": "📡 SSE",
                "auto": "⚡ Auto",
                "stdio": "⚡ stdio",
            }.get(transport, transport)

            type_badge = _type_badge(server)
            auth_badge = _auth_badge(server)

            st.markdown(
                f"**{icon} {server['name']}**  &nbsp; "
                f"<span style='font-size:11px;background:#1f2937;padding:2px 6px;border-radius:4px'>{type_badge}</span> "
                f"<span style='font-size:11px;background:#1f2937;padding:2px 6px;border-radius:4px'>{auth_badge}</span> "
                f"<span style='font-size:11px;color:#8b949e'>{transport_badge}</span>",
                unsafe_allow_html=True,
            )
            st.caption(_server_identifier(server))
            if server.get("description"):
                st.caption(server["description"])
            if last_error:
                st.caption(f"❌ {last_error[:120]}")
            else:
                st.caption(f"**{tool_count}** tools · last seen {last_seen}")

            if auth_type in ("bearer", "basic", "header") and server.get("auth_header"):
                st.caption(f"Auth value: `{_mask_auth(server['auth_header'])}`")
            if auth_type == "oauth":
                if server.get("oauth_token"):
                    exp = server.get("oauth_expires_at")
                    exp_txt = f" · expires {exp[:10]}" if exp else ""
                    st.caption(f"✅ OAuth connected{exp_txt}")
                else:
                    st.caption("⚠️ OAuth not yet authorized")

        with top_right:
            btn_cols = st.columns(4)

            with btn_cols[0]:
                if st.button("Test", key=f"test_{sid}", use_container_width=True):
                    with st.spinner("Testing…"):
                        result, test_err = api_post(f"/mcp-servers/{sid}/test")
                    if test_err:
                        st.error(f"Test failed: {test_err}")
                    elif result and result.get("ok"):
                        names = ", ".join(result.get("tools", []))
                        st.success(f"✅ {len(result['tools'])} tool(s): {names}")
                    else:
                        st.error(result.get("error", "Connection failed") if result else "No response")

            with btn_cols[1]:
                if enabled:
                    if st.button("Disable", key=f"disable_{sid}", use_container_width=True):
                        _, err = api_patch(f"/mcp-servers/{sid}", json={"enabled": False})
                        if err:
                            st.error(f"Disable failed: {err}")
                        else:
                            st.rerun()
                else:
                    if st.button("Enable", key=f"enable_{sid}", use_container_width=True):
                        _, err = api_patch(f"/mcp-servers/{sid}", json={"enabled": True})
                        if err:
                            st.error(f"Enable failed: {err}")
                        else:
                            st.rerun()

            with btn_cols[2]:
                edit_key = f"edit_{sid}"
                if st.button("Edit", key=f"editbtn_{sid}", use_container_width=True):
                    st.session_state[edit_key] = not st.session_state.get(edit_key, False)
                    st.rerun()

            with btn_cols[3]:
                confirm_key = f"confirm_del_{sid}"
                if st.session_state.get(confirm_key):
                    if st.button("Confirm ✕", key=f"confirm_del_btn_{sid}", use_container_width=True, type="primary"):
                        _, del_err = api_delete(f"/mcp-servers/{sid}")
                        if del_err:
                            st.error(f"Delete failed: {del_err}")
                        else:
                            st.session_state.pop(confirm_key, None)
                            st.rerun()
                else:
                    if st.button("✕", key=f"del_{sid}", use_container_width=True):
                        st.session_state[confirm_key] = True
                        st.rerun()

        # OAuth refresh button
        if auth_type == "oauth" and server.get("oauth_token"):
            if st.button("🔄 Refresh OAuth Token", key=f"oauth_refresh_{sid}"):
                with st.spinner("Refreshing…"):
                    resp, err = api_post(f"/mcp-servers/{sid}/oauth/refresh")
                if err:
                    st.error(f"Refresh failed: {err}")
                else:
                    st.success("Token refreshed.")
                    st.rerun()

        # Inline edit form
        if st.session_state.get(f"edit_{sid}"):
            _render_edit_form(server)

        # Tool list expander
        if tool_count > 0:
            with st.expander(f"▼ Tools ({tool_count})", expanded=False):
                with st.spinner("Loading tool list…"):
                    test_result, _ = api_post(f"/mcp-servers/{sid}/test")
                if test_result and test_result.get("ok"):
                    st.markdown(", ".join(f"`{t}`" for t in test_result.get("tools", [])))
                elif test_result:
                    st.caption(f"Could not fetch tools: {test_result.get('error', 'unknown error')}")


def _render_edit_form(server: dict) -> None:
    sid = server["id"]
    with st.expander("✏️ Edit Server", expanded=True):
        with st.form(f"edit_form_{sid}"):
            new_name = st.text_input("Name", value=server.get("name", ""))
            new_desc = st.text_input("Description", value=server.get("description") or "")
            new_enabled = st.checkbox("Enabled", value=server.get("enabled", True))

            server_type = server.get("server_type", "remote")
            if server_type == "stdio":
                new_cmd = st.text_input("Command", value=server.get("command") or "")
                new_args_raw = st.text_area(
                    "Args (one per line)",
                    value="\n".join(server.get("args") or []),
                )
            else:
                new_transport = st.selectbox(
                    "Transport",
                    options=["auto", "streamable_http", "sse"],
                    index=["auto", "streamable_http", "sse"].index(server.get("transport", "auto")),
                )
                new_auth_type = st.selectbox(
                    "Auth type",
                    options=["none", "bearer", "basic", "header"],
                    index=["none", "bearer", "basic", "header"].index(server.get("auth_type", "header")),
                )
                new_auth = st.text_input(
                    "Auth value",
                    value=server.get("auth_header") or "",
                    type="password",
                    help="Token (bearer/basic) or full header value (custom)",
                )

            save = st.form_submit_button("Save Changes", type="primary")

        if save:
            payload: dict = {
                "name": new_name or server["name"],
                "description": new_desc or None,
                "enabled": new_enabled,
            }
            if server_type == "stdio":
                payload["command"] = new_cmd
                payload["args"] = [a for a in new_args_raw.splitlines() if a.strip()]
            else:
                payload["transport"] = new_transport
                payload["auth_type"] = new_auth_type
                payload["auth_header"] = new_auth or None

            _, err = api_patch(f"/mcp-servers/{sid}", json=payload)
            if err:
                st.error(f"Update failed: {err}")
            else:
                st.session_state.pop(f"edit_{sid}", None)
                st.success("Updated.")
                st.rerun()


# ── Add Server tabs ────────────────────────────────────────────────────────────


def _render_add_remote_tab() -> None:
    st.markdown("**Register a remote HTTP MCP server.**")
    new_name = st.text_input("Name", key="add_remote_name", placeholder="Context7")
    new_url = st.text_input("URL", key="add_remote_url", placeholder="https://mcp.context7.com/mcp")

    transport_opt = st.selectbox(
        "Transport",
        options=["auto", "streamable_http", "sse"],
        format_func=lambda x: {
            "auto": "⚡ Auto-detect (recommended)",
            "streamable_http": "🌐 Streamable HTTP",
            "sse": "📡 SSE (legacy)",
        }[x],
        key="add_remote_transport",
    )

    auth_type = st.radio(
        "Authentication",
        options=["none", "bearer", "basic", "header"],
        format_func=lambda x: {
            "none": "🔓 None",
            "bearer": "🔑 Bearer Token",
            "basic": "👤 Basic Auth (email:token)",
            "header": "⚿ Custom Header",
        }[x],
        horizontal=True,
        key="add_remote_auth_type",
    )

    auth_value: str | None = None
    if auth_type == "bearer":
        auth_value = st.text_input("Token", key="add_remote_token", type="password", placeholder="sk-...")
    elif auth_type == "basic":
        col_email, col_tok = st.columns(2)
        with col_email:
            email = st.text_input("Email", key="add_remote_email")
        with col_tok:
            api_tok = st.text_input("API Token", key="add_remote_api_token", type="password")
        auth_value = f"{email}:{api_tok}" if email or api_tok else None
    elif auth_type == "header":
        auth_value = st.text_input(
            "Authorization header value",
            key="add_remote_header",
            placeholder="Bearer sk-...   or   Basic dXNlcjpwYXNz",
        )

    new_desc = st.text_input("Description (optional)", key="add_remote_desc")
    new_enabled = st.checkbox("Enable immediately", value=True, key="add_remote_enabled")

    # OAuth discovery section
    if new_url.strip() and auth_type == "none":
        with st.expander("🔗 Discover OAuth", expanded=False):
            if st.button("Discover OAuth Metadata", key="oauth_discover_btn"):
                with st.spinner("Probing…"):
                    resp, err = api_post("/mcp-servers/oauth/discover", json={"url": new_url.strip()})
                if err or (resp and resp.get("error")):
                    st.warning(resp.get("error") if resp else err)
                else:
                    st.session_state["oauth_discover_result"] = resp
                    st.success("OAuth metadata found!")

            if st.session_state.get("oauth_discover_result"):
                meta = st.session_state["oauth_discover_result"]
                st.json(meta)
                st.caption("To use OAuth: save this server with auth_type=oauth, then use Initiate OAuth.")

    col_test, col_save = st.columns(2)
    with col_test:
        test_clicked = st.button("Test Connection", key="add_remote_test", use_container_width=True)
    with col_save:
        save_clicked = st.button("Save Server", key="add_remote_save", use_container_width=True, type="primary")

    if test_clicked:
        url = new_url.strip()
        if not url:
            st.warning("Enter a URL first.")
        else:
            with st.spinner("Testing…"):
                result, err = api_post("/mcp-servers/test-url", json={
                    "url": url,
                    "auth_header": auth_value or None,
                    "auth_type": auth_type,
                    "transport": transport_opt,
                    "server_type": "remote",
                })
            if err:
                st.error(f"Test failed: {err}")
            elif result and result.get("ok"):
                used = result.get("transport_used", "")
                st.success(f"✅ {len(result['tools'])} tool(s) via **{used}**: {', '.join(result['tools'])}")
            else:
                st.error(result.get("error", "Connection failed") if result else "No response")

    if save_clicked:
        name = new_name.strip()
        url = new_url.strip()
        if not name or not url:
            st.error("Name and URL are required.")
        else:
            data, err = api_post("/mcp-servers", json={
                "name": name,
                "server_type": "remote",
                "url": url,
                "transport": transport_opt,
                "auth_type": auth_type,
                "auth_header": auth_value or None,
                "description": new_desc.strip() or None,
                "enabled": new_enabled,
            })
            if err:
                st.error(f"Failed to save: {err}")
            else:
                st.success(f"Server **{name}** added (id={data.get('id')}).")
                st.rerun()


def _render_add_stdio_tab() -> None:
    st.markdown("**Register a local stdio MCP server** (npx, python, docker, etc.).")
    new_name = st.text_input("Name", key="add_stdio_name", placeholder="GitHub MCP")
    new_cmd = st.text_input("Command", key="add_stdio_cmd", placeholder="npx")
    new_args_raw = st.text_area(
        "Args (one per line)",
        key="add_stdio_args",
        placeholder="-y\n@modelcontextprotocol/server-github",
        height=100,
    )

    st.markdown("**Environment Variables**")
    env_dict = _env_editor("add_stdio_env")

    new_desc = st.text_input("Description (optional)", key="add_stdio_desc")
    new_enabled = st.checkbox("Enable immediately", value=True, key="add_stdio_enabled")

    col_test, col_save = st.columns(2)
    with col_test:
        test_clicked = st.button("Test", key="add_stdio_test", use_container_width=True)
    with col_save:
        save_clicked = st.button("Save Server", key="add_stdio_save", use_container_width=True, type="primary")

    if test_clicked:
        cmd = new_cmd.strip()
        if not cmd:
            st.warning("Enter a command first.")
        else:
            args = [a for a in new_args_raw.splitlines() if a.strip()]
            with st.spinner("Spawning process…"):
                result, err = api_post("/mcp-servers/test-url", json={
                    "server_type": "stdio",
                    "command": cmd,
                    "args": args,
                    "env": env_dict,
                })
            if err:
                st.error(f"Test failed: {err}")
            elif result and result.get("ok"):
                st.success(f"✅ {len(result['tools'])} tool(s): {', '.join(result['tools'])}")
            else:
                st.error(result.get("error", "Connection failed") if result else "No response")

    if save_clicked:
        name = new_name.strip()
        cmd = new_cmd.strip()
        if not name or not cmd:
            st.error("Name and Command are required.")
        else:
            args = [a for a in new_args_raw.splitlines() if a.strip()]
            data, err = api_post("/mcp-servers", json={
                "name": name,
                "server_type": "stdio",
                "command": cmd,
                "args": args,
                "env": env_dict,
                "description": new_desc.strip() or None,
                "enabled": new_enabled,
                "auth_type": "none",
            })
            if err:
                st.error(f"Failed to save: {err}")
            else:
                st.success(f"Server **{name}** added (id={data.get('id')}).")
                st.rerun()


def _render_presets_tab() -> None:
    st.markdown("**Click a preset to pre-fill the add form.**")
    cols = st.columns(3)
    for i, preset in enumerate(_PRESETS):
        with cols[i % 3]:
            with st.container(border=True):
                st.markdown(f"### {preset['icon']} {preset['label']}")
                st.caption(preset.get("description", ""))
                if preset["server_type"] == "stdio":
                    st.code(f"{preset['command']} {' '.join(preset.get('args', []))}", language="bash")
                    if preset.get("env_hint"):
                        st.caption(f"Needs: `{preset['env_hint']}`")
                else:
                    st.code(preset.get("url", ""), language="text")
                    st.caption(f"Auth: {preset.get('auth_type', 'none')}")

                if st.button(f"Use {preset['label']}", key=f"preset_{preset['label']}"):
                    st.session_state["preset_selected"] = preset
                    st.session_state["add_tab_index"] = 0 if preset["server_type"] == "remote" else 1
                    st.rerun()

    # If a preset was selected, show a quick-save form below
    if "preset_selected" in st.session_state:
        preset = st.session_state["preset_selected"]
        st.divider()
        st.subheader(f"Quick-save: {preset['icon']} {preset['label']}")

        with st.form("preset_save_form"):
            ps_name = st.text_input("Name", value=preset["label"])
            ps_desc = st.text_input("Description", value=preset.get("description", ""))
            ps_enabled = st.checkbox("Enable immediately", value=True)

            if preset["server_type"] == "stdio":
                ps_cmd = st.text_input("Command", value=preset.get("command", ""))
                ps_args_raw = st.text_area("Args (one per line)", value="\n".join(preset.get("args", [])))
                env_hint = preset.get("env_hint", "")
                if env_hint:
                    ps_env_val = st.text_input(f"{env_hint}", type="password", help=f"Required env var for {preset['label']}")
                    preset_env = {env_hint: ps_env_val} if ps_env_val else {}
                else:
                    preset_env = {}
            else:
                ps_url = st.text_input("URL", value=preset.get("url", ""))
                ps_auth_type = st.selectbox(
                    "Auth type",
                    options=["none", "bearer", "basic", "header"],
                    index=["none", "bearer", "basic", "header"].index(preset.get("auth_type", "none")),
                )
                ps_auth_val = st.text_input("Auth value", type="password") if ps_auth_type != "none" else None

            col_cancel, col_save = st.columns(2)
            with col_cancel:
                cancel = st.form_submit_button("Cancel")
            with col_save:
                save = st.form_submit_button("Save", type="primary")

        if cancel:
            del st.session_state["preset_selected"]
            st.rerun()

        if save:
            if preset["server_type"] == "stdio":
                args = [a for a in ps_args_raw.splitlines() if a.strip()]
                payload = {
                    "name": ps_name,
                    "server_type": "stdio",
                    "command": ps_cmd,
                    "args": args,
                    "env": preset_env,
                    "description": ps_desc or None,
                    "enabled": ps_enabled,
                    "auth_type": "none",
                }
            else:
                payload = {
                    "name": ps_name,
                    "server_type": "remote",
                    "url": ps_url,
                    "transport": preset.get("transport", "auto"),
                    "auth_type": ps_auth_type,
                    "auth_header": ps_auth_val or None,
                    "description": ps_desc or None,
                    "enabled": ps_enabled,
                }
            data, err = api_post("/mcp-servers", json=payload)
            if err:
                st.error(f"Failed to save: {err}")
            else:
                st.success(f"Server **{ps_name}** added (id={data.get('id')}).")
                del st.session_state["preset_selected"]
                st.rerun()


# ── Main render ────────────────────────────────────────────────────────────────


def render():
    # Header
    col_title, col_refresh, col_reload = st.columns([5, 1, 1.5])
    with col_title:
        st.header("🔌 External MCP Servers")
    with col_refresh:
        st.write("")
        if st.button("🔄 Refresh", key="mcp_refresh"):
            st.rerun()
    with col_reload:
        st.write("")
        if st.button("↺ Reload Bridge", key="mcp_reload", type="primary"):
            data, err = api_post("/mcp-servers/reload")
            if err:
                st.error(f"Reload failed: {err}")
            else:
                st.success(data.get("message", "Bridge reloaded"))
                st.rerun()

    st.caption(
        "External MCP servers registered here are connected at startup. "
        "Supports remote HTTP servers and local stdio processes."
    )

    # OAuth success/error feedback from callback redirect
    qp = st.query_params
    if qp.get("oauth_success"):
        st.success(f"✅ OAuth connected for server {qp.get('server_id', '')}!")
        st.query_params.clear()
    if qp.get("oauth_error"):
        st.error(f"OAuth error: {qp.get('oauth_error')}")
        st.query_params.clear()

    st.divider()

    # Existing servers list
    servers, err = api_get("/mcp-servers", timeout=10)
    if err:
        st.error(f"Failed to load servers: {err}")
        servers = []
    elif not servers:
        st.info("No external MCP servers registered yet. Add one below.")

    for server in servers or []:
        _render_server(server)

    st.divider()

    # Add server — tabbed
    st.subheader("Add New MCP Server")
    tab_remote, tab_stdio, tab_presets = st.tabs(["🌐 Remote HTTP", "⚡ Local Process (stdio)", "🚀 Quick Presets"])

    with tab_remote:
        _render_add_remote_tab()

    with tab_stdio:
        _render_add_stdio_tab()

    with tab_presets:
        _render_presets_tab()
