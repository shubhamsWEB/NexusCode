"""
🔌 External MCP Servers — dashboard management page.

Allows users to register, test, enable/disable, and remove external MCP
servers. The bridge is reloaded live via POST /mcp-servers/reload so
changes take effect without restarting the API.
"""

from __future__ import annotations

import streamlit as st

from src.ui.helpers import api_delete, api_get, api_patch, api_post, time_ago


def _mask_auth(header: str | None) -> str:
    """Show only the last 4 characters of an auth header value."""
    if not header:
        return "—"
    return f"...{header[-4:]}" if len(header) > 4 else "****"


def _status_icon(server: dict) -> str:
    """Green dot if last seen and no error; red cross otherwise."""
    if server.get("last_error") is None and server.get("last_seen_at"):
        return "🟢"
    if server.get("last_seen_at") is None and server.get("last_error") is None:
        return "⬜"
    return "🔴"


def render():
    # ── Header ────────────────────────────────────────────────────────────────
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
        "Their tools are merged into the agent's tool palette automatically."
    )
    st.divider()

    # ── List existing servers ─────────────────────────────────────────────────
    servers, err = api_get("/mcp-servers", timeout=10)

    if err:
        st.error(f"Failed to load servers: {err}")
        servers = []
    elif not servers:
        st.info("No external MCP servers registered yet. Add one below.")

    for server in servers or []:
        sid = server["id"]
        icon = _status_icon(server)
        enabled = server.get("enabled", True)
        tool_count = server.get("tool_count", 0)
        last_seen = time_ago(server.get("last_seen_at"))
        last_error = server.get("last_error")
        tools_key = f"tools_expanded_{sid}"

        with st.container(border=True):
            top_left, top_right = st.columns([4, 2])

            with top_left:
                transport = server.get("transport", "auto")
                transport_badge = {
                    "streamable_http": "🌐 Streamable HTTP",
                    "sse": "📡 SSE",
                    "auto": "⚡ Auto",
                }.get(transport, transport)
                st.markdown(
                    f"**{icon} {server['name']}**  &nbsp; `{server['url']}`"
                    f"  <span style='font-size:11px;color:#8b949e'>{transport_badge}</span>",
                    unsafe_allow_html=True,
                )
                if server.get("description"):
                    st.caption(server["description"])
                if last_error:
                    st.caption(f"❌ error: {last_error[:120]}")
                else:
                    st.caption(f"**{tool_count}** tools · last seen {last_seen}")
                if server.get("auth_header"):
                    st.caption(f"Auth: `{_mask_auth(server['auth_header'])}`")

            with top_right:
                btn_cols = st.columns(3)

                # Test button
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

                # Enable / Disable toggle
                with btn_cols[1]:
                    if enabled:
                        if st.button("Disable", key=f"disable_{sid}", use_container_width=True):
                            _, upd_err = api_patch(f"/mcp-servers/{sid}", json={"enabled": False})
                            if upd_err:
                                st.error(f"Disable failed: {upd_err}")
                            else:
                                st.rerun()
                    else:
                        if st.button("Enable", key=f"enable_{sid}", use_container_width=True):
                            _, upd_err = api_patch(f"/mcp-servers/{sid}", json={"enabled": True})
                            if upd_err:
                                st.error(f"Enable failed: {upd_err}")
                            else:
                                st.rerun()

                # Delete button
                with btn_cols[2]:
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
                        if st.button("✕ Delete", key=f"del_{sid}", use_container_width=True):
                            st.session_state[confirm_key] = True
                            st.rerun()

            # Collapsed tool list expander
            if tool_count > 0:
                with st.expander(f"▼ Tools ({tool_count})", expanded=False):
                    with st.spinner("Loading tool list…"):
                        test_result, _ = api_post(f"/mcp-servers/{sid}/test")
                    if test_result and test_result.get("ok"):
                        tool_names = test_result.get("tools", [])
                        st.markdown(", ".join(f"`{t}`" for t in tool_names))
                    elif test_result:
                        st.caption(f"Could not fetch tools: {test_result.get('error', 'unknown error')}")

    st.divider()

    # ── Add new server ─────────────────────────────────────────────────────────
    st.subheader("Add New MCP Server")

    with st.form("add_mcp_server_form", clear_on_submit=True):
        new_name = st.text_input("Name", placeholder="Context7 / My Package MCP")
        new_url = st.text_input(
            "URL",
            placeholder="https://mcp.context7.com/mcp  or  http://localhost:3100/sse",
        )
        col_transport, col_auth = st.columns([1, 2])
        with col_transport:
            new_transport = st.selectbox(
                "Transport",
                options=["auto", "streamable_http", "sse"],
                format_func=lambda x: {
                    "auto": "⚡ Auto-detect (recommended)",
                    "streamable_http": "🌐 Streamable HTTP (Context7, cloud servers)",
                    "sse": "📡 SSE (legacy / self-hosted)",
                }[x],
                help="Auto-detect tries Streamable HTTP first, then SSE. "
                     "Pick a specific transport if auto-detect is slow or unreliable.",
            )
        with col_auth:
            new_auth = st.text_input(
                "Auth header (optional)",
                placeholder="Bearer sk-...",
                type="password",
            )
        new_desc = st.text_input("Description (optional)", placeholder="Up-to-date library docs")
        new_enabled = st.checkbox("Enable immediately", value=True)

        col_test, col_save = st.columns(2)
        with col_test:
            test_clicked = st.form_submit_button("Test Connection", use_container_width=True)
        with col_save:
            save_clicked = st.form_submit_button("Save", use_container_width=True, type="primary")

    if test_clicked and new_url.strip():
        with st.spinner("Testing connection…"):
            result, err = api_post(
                "/mcp-servers/test-url",
                json={
                    "url": new_url.strip(),
                    "auth_header": new_auth.strip() or None,
                    "transport": new_transport,
                },
            )
        if err:
            st.error(f"Test failed: {err}")
        elif result and result.get("ok"):
            names = ", ".join(result.get("tools", []))
            used = result.get("transport_used", "")
            transport_note = f" via **{used}**" if used else ""
            st.success(f"✅ Connection OK{transport_note} — {len(result['tools'])} tool(s): {names}")
        else:
            st.error(result.get("error", "Connection failed") if result else "No response")

    elif test_clicked:
        st.warning("Please enter a URL before testing.")

    if save_clicked:
        if not new_name.strip() or not new_url.strip():
            st.error("Name and URL are required.")
        else:
            data, err = api_post(
                "/mcp-servers",
                json={
                    "name": new_name.strip(),
                    "url": new_url.strip(),
                    "auth_header": new_auth.strip() or None,
                    "description": new_desc.strip() or None,
                    "enabled": new_enabled,
                    "transport": new_transport,
                },
            )
            if err:
                st.error(f"Failed to save: {err}")
            else:
                st.success(f"Server **{new_name}** added (id={data.get('id')}).")
                st.rerun()
