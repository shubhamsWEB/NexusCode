"""
Scoped API Key Management page.
Allows creating, listing, and deleting API key scopes that restrict
which repos a team can search via MCP or REST API.
"""

from __future__ import annotations

import streamlit as st

from src.ui.helpers import api_delete, api_get, api_post


def render():
    st.header("API Key Scopes")
    st.caption(
        "Scoped API keys restrict which repositories a team or client can search. "
        "Use them to give the frontend team access to only their repos, "
        "while the platform team has a different scoped key."
    )

    _col_title, col_refresh = st.columns([6, 1])
    with col_refresh:
        st.write("")
        if st.button("🔄 Refresh", key="api_keys_refresh"):
            st.rerun()

    # ── Newly created key banner (persists until dismissed) ───────────────────
    if st.session_state.get("new_api_key_value"):
        st.success(f"API key **{st.session_state.get('new_api_key_name', '')}** created successfully.")
        st.warning("Copy this key now — it will **not** be shown again.")
        st.code(st.session_state["new_api_key_value"], language=None)
        if st.button("✅ I've copied the key", key="dismiss_new_key"):
            st.session_state.pop("new_api_key_value", None)
            st.session_state.pop("new_api_key_name", None)
            st.rerun()
        st.divider()

    # ── Section 1: Existing Keys ──────────────────────────────────────────────
    st.subheader("Existing Keys")
    st.caption("Raw keys are never stored — only their SHA-256 hash. If you've lost a key, delete it and create a new one.")
    keys_data, keys_err = api_get("/api-keys", timeout=10)

    if keys_err:
        st.error(f"Failed to load API keys: {keys_err}")
    elif not keys_data:
        st.info("No API key scopes configured yet.")
    else:
        for key in keys_data:
            scope_id = key.get("id")
            name = key.get("name", "—")
            description = key.get("description", "")
            allowed_repos = key.get("allowed_repos", [])
            created_at = key.get("created_at", "—")
            last_used_at = key.get("last_used_at") or "Never"
            confirm_key = f"confirm_delete_key_{scope_id}"

            with st.container(border=True):
                left_col, right_col = st.columns([4, 1])
                with left_col:
                    st.markdown(f"**{name}**" + (f" — {description}" if description else ""))
                    if allowed_repos:
                        repo_list = ", ".join(f"`{r}`" for r in allowed_repos)
                        st.markdown(f"Allowed repos: {repo_list}")
                    else:
                        st.markdown("Allowed repos: **all** (admin key)")
                    st.caption(f"Created: {created_at[:19] if created_at else '—'}  |  Last used: {last_used_at[:19] if last_used_at and last_used_at != 'Never' else last_used_at}")

                with right_col:
                    if not st.session_state.get(confirm_key, False):
                        if st.button("🗑️ Delete", key=f"del_key_{scope_id}"):
                            st.session_state[confirm_key] = True
                            st.rerun()
                    else:
                        st.warning("Confirm delete?")
                        c1, c2 = st.columns(2)
                        with c1:
                            if st.button("Yes", key=f"yes_del_{scope_id}", type="primary"):
                                _, err = api_delete(f"/api-keys/{scope_id}")
                                if err:
                                    st.error(f"Delete failed: {err}")
                                else:
                                    st.session_state.pop(confirm_key, None)
                                    st.rerun()
                        with c2:
                            if st.button("No", key=f"no_del_{scope_id}"):
                                st.session_state.pop(confirm_key, None)
                                st.rerun()

    st.divider()

    # ── Section 2: Create New Key ─────────────────────────────────────────────
    st.subheader("Create New API Key")

    # Fetch indexed repos for the multiselect (outside the form so it renders immediately)
    repos_data, repos_err = api_get("/repos", timeout=10)
    if repos_err:
        repo_options: list[str] = []
        st.warning(f"Could not load repo list: {repos_err}")
    else:
        repo_options = sorted(
            r.get("repo", f"{r.get('owner','')}/{r.get('name','')}") for r in (repos_data or [])
        )

    with st.form("create_api_key_form"):
        name_input = st.text_input("Name *", placeholder="frontend-team")
        desc_input = st.text_input("Description", placeholder="Frontend squad — 3 repos")
        selected_repos = st.multiselect(
            "Allowed Repositories",
            options=repo_options,
            default=[],
            help="Select which repos this key can search. Leave empty to create an admin key with access to all repos.",
        )
        submitted = st.form_submit_button("Create Key", type="primary")

    if submitted:
        if not name_input.strip():
            st.error("Name is required.")
        else:
            payload = {
                "name": name_input.strip(),
                "description": desc_input.strip(),
                "allowed_repos": selected_repos,
            }
            result, err = api_post("/api-keys", json=payload)
            if err:
                st.error(f"Failed to create key: {err}")
            else:
                # Store in session state so the key survives st.rerun()
                st.session_state["new_api_key_value"] = result.get("raw_key", "")
                st.session_state["new_api_key_name"] = result.get("name", "")
                st.rerun()

    st.divider()

    # ── Section 3: Usage Instructions ────────────────────────────────────────
    with st.expander("How to use scoped API keys with MCP clients"):
        api_url = st.session_state.get("api_url", "http://localhost:8000")
        st.markdown(
            f"""
Configure your MCP client (e.g. Claude Code `~/.claude/settings.json`) with the key in the SSE URL:

```json
{{
  "mcpServers": {{
    "nexuscode": {{
      "type": "sse",
      "url": "{api_url}/mcp/sse?api_key=YOUR_KEY_HERE"
    }}
  }}
}}
```

Or pass it as an HTTP header:

```
X-Api-Key: YOUR_KEY_HERE
```

**How scoping works:**
- Keys with `allowed_repos` only ever search those repos — no other repos are considered.
- Keys with no `allowed_repos` (admin keys) search all indexed repos.
- The cross-repo router only considers repos within the allowed set.
- Invalid or missing keys return HTTP 401.
"""
        )
