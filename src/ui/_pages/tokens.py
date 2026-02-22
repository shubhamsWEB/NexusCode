import json

import streamlit as st

from src.ui.helpers import api_get, api_post


def render():
    st.title("MCP Tokens")
    st.markdown("Issue bearer tokens for AI agents and connect them to the MCP server.")

    # ── Section 1: Issue New Token ────────────────────────────────────────────
    st.subheader("Issue New MCP Token")

    repos_data, repos_err = api_get("/repos", timeout=10)
    repo_options = []
    if repos_data and not repos_err:
        repo_options = [r["repo"] for r in repos_data if r.get("repo")]

    with st.form("issue_token_form"):
        sub = st.text_input(
            "Agent name / identifier",
            placeholder="my-claude-agent",
            help="Identifies who this token belongs to (e.g. claude-desktop, ci-bot).",
        )
        selected_repos = st.multiselect(
            "Scope to repositories (leave empty for all repos)",
            options=repo_options,
            help="Leave empty to grant access to all indexed repositories.",
        )
        expiry_hours = st.number_input(
            "Expiry (hours)", min_value=1, max_value=168, value=8, step=1,
            help="Token will expire after this many hours. Max 7 days (168h).",
        )
        submitted = st.form_submit_button("Issue Token", type="primary")

    if submitted:
        if not sub.strip():
            st.error("Agent name is required.")
        else:
            with st.spinner("Issuing token..."):
                data, err = api_post(
                    "/auth/token",
                    json={"sub": sub.strip(), "repos": selected_repos},
                    timeout=10,
                )
            if err:
                st.error(f"Failed to issue token: {err}")
            else:
                token = data.get("access_token", "")
                st.session_state["last_token"] = token
                st.session_state["last_token_sub"] = sub.strip()
                st.success("Token issued successfully!")

    # Show the most recently issued token if present
    if st.session_state.get("last_token"):
        token = st.session_state["last_token"]
        sub_name = st.session_state.get("last_token_sub", "")
        st.warning("Copy this token now — it will not be shown again after you navigate away.")
        st.text_area(
            f"Access Token ({sub_name})",
            value=token,
            height=100,
        )
        scope_label = ", ".join(selected_repos) if submitted and selected_repos else "all repos"
        st.info(
            f"**Sub:** `{sub_name}`  |  "
            f"**Scope:** {scope_label}  |  "
            f"**Expires in:** {int(expiry_hours)}h"
        )
        if st.button("Clear token from view"):
            del st.session_state["last_token"]
            del st.session_state["last_token_sub"]
            st.rerun()

    st.divider()

    # ── Section 2: Connection Snippets ────────────────────────────────────────
    st.subheader("Connect Your AI Agent")

    api_url = st.session_state.get("api_url", "http://localhost:8000")

    tab_claude, tab_python, tab_rest = st.tabs(
        ["Claude Desktop", "Python MCP Client", "REST API"]
    )

    with tab_claude:
        st.markdown(
            "Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`:"
        )
        claude_config = {
            "mcpServers": {
                "codebase": {
                    "command": "curl",
                    "args": [
                        "-N",
                        "-H",
                        "Accept: text/event-stream",
                        f"{api_url}/mcp/sse",
                    ],
                }
            }
        }
        st.code(json.dumps(claude_config, indent=2), language="json")
        st.caption("Restart Claude Desktop after saving this file.")

    with tab_python:
        python_snippet = f'''\
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client("{api_url}/mcp/sse") as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()

        # Search the codebase
        result = await session.call_tool(
            "search_codebase",
            {{"query": "what handles authentication?", "top_k": 5}}
        )
        print(result.content[0].text)

        # Pre-assembled context for a coding task
        result = await session.call_tool(
            "get_agent_context",
            {{
                "task": "add rate limiting to the API",
                "focal_files": ["src/api/app.py"],
            }}
        )
        print(result.content[0].text)
'''
        st.code(python_snippet, language="python")
        st.caption("Install: `pip install mcp`")

    with tab_rest:
        rest_snippet = f'''\
# Search (no auth required on default config)
curl -s -X POST {api_url}/search \\
  -H "Content-Type: application/json" \\
  -d \'{{"query": "what handles authentication?", "top_k": 5, "rerank": true}}\'

# Health check
curl -s {api_url}/health

# Issue a token
curl -s -X POST {api_url}/auth/token \\
  -H "Content-Type: application/json" \\
  -d \'{{"sub": "my-agent", "repos": []}}\'
'''
        st.code(rest_snippet, language="bash")

    st.divider()

    # ── Section 3: MCP Tools Reference ───────────────────────────────────────
    st.subheader("Available MCP Tools")

    st.markdown(
        """
| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `search_codebase` | Hybrid semantic + keyword search with reranking | `query`, `repo`, `top_k`, `mode` |
| `get_symbol` | Fuzzy symbol lookup — like "Go to Definition" | `name`, `repo` |
| `find_callers` | Who calls this function? | `symbol`, `depth`, `repo` |
| `get_file_context` | Full structural map of a file | `path`, `repo`, `include_deps` |
| `get_agent_context` | Pre-assembled context for a coding task | `task`, `focal_files`, `token_budget` |
"""
    )

    st.divider()

    # ── Section 4: Test Connection ────────────────────────────────────────────
    st.subheader("Test MCP Server")

    st.markdown("**MCP SSE endpoint:**")
    st.code(f"{api_url}/mcp/sse", language="text")

    if st.button("Test Connection", type="secondary"):
        with st.spinner("Checking server..."):
            data, err = api_get("/health", timeout=5)
        if err:
            st.error(f"Cannot reach server: {err}")
        else:
            chunks = data.get("chunks", 0)
            repos = data.get("repos", 0)
            st.success(
                f"MCP server is reachable at `{api_url}/mcp/sse`  \n"
                f"{repos} repo(s) indexed · {chunks:,} active chunks"
            )
