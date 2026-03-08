"""
Codebase Intelligence — Admin Dashboard

Pages:
  🚀 Get Started    — onboarding checklist
  ⚙️  Settings       — API keys, service health, env config
  📦 Repositories   — register, index, manage repos
  🔗 Webhook Setup  — step-by-step webhook configuration wizard
  🔑 MCP Tokens     — issue tokens + agent connection snippets
  🏠 Health         — index stats, per-repo breakdown, auto-refresh
  🔍 Query Tester   — live search UI with results + assembled context
  📡 Activity Feed  — webhook events with status + timing
  🧩 Planning Mode  — generate implementation plans for bugs/features
  💬 Ask Mode       — chat with your codebase, get cited mentor answers
  📜 History        — browse past Ask sessions and Planning plans
  📚 Documentation  — full reference docs (all doc/*.md files)
"""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime

import streamlit as st

# ── Path setup ────────────────────────────────────────────────────────────────
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Codebase Intelligence",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── API URL (shared via session_state so page modules can read it) ─────────────
if "api_url" not in st.session_state:
    st.session_state["api_url"] = os.environ.get("API_URL", "http://localhost:8000")

# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.title("🧠 Codebase Intelligence")
st.sidebar.caption("MCP Knowledge Server")

PAGES = [
    "🚀 Get Started",
    "⚙️  Settings",
    "📦 Repositories",
    "🕸️ Knowledge Graph",
    "🔗 Webhook Setup",
    "🔑 MCP Tokens",
    "🏠 Health",
    "🔍 Query Tester",
    "📡 Activity Feed",
    "🧩 Planning Mode",
    "💬 Ask Mode",
    "📜 History",
    "🔌 MCP Servers",
    "🤖 Agent Roles",
    "⚡ Workflows",
    "📚 Documentation",
]


page = st.sidebar.radio("Navigate", PAGES, label_visibility="collapsed")
st.sidebar.divider()

st.session_state["api_url"] = st.sidebar.text_input(
    "API base URL",
    value=st.session_state["api_url"],
)
st.sidebar.caption(f"MCP: `{st.session_state['api_url']}/mcp`")

# Mini status bar in sidebar
try:
    import httpx as _httpx

    _h = _httpx.get(f"{st.session_state['api_url']}/health", timeout=2).json()
    st.sidebar.caption(f"**{_h.get('repos', 0)} repo(s)** · **{_h.get('chunks', 0):,} chunks**")
except Exception:
    st.sidebar.caption("API offline")


# ── Shared helpers (used by the legacy Health / Query Tester / Activity pages) ─


def _api(path: str, method: str = "GET", json=None, timeout: int = 30):
    import httpx

    url = f"{st.session_state['api_url']}{path}"
    try:
        if method == "GET":
            resp = httpx.get(url, timeout=timeout)
        else:
            resp = httpx.post(url, json=json, timeout=timeout)
        resp.raise_for_status()
        return resp.json(), None
    except httpx.TimeoutException:
        return None, "Request timed out — is the API server running?"
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return None, str(e)


def _time_ago(ts_str: str | None) -> str:
    if not ts_str:
        return "never"
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
        return ts_str


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Get Started
# ══════════════════════════════════════════════════════════════════════════════


def _render_get_started():
    st.title("🚀 Get Started")
    st.markdown(
        "Follow this checklist to set up your Codebase Intelligence server from scratch. "
        "Each step links to the relevant page."
    )

    # Collect state for each step
    config_data, _ = _api("/config")
    repos_data, _ = _api("/repos")
    health_data, _ = _api("/health")

    def _check(condition: bool) -> str:
        return "✅" if condition else "⬜"

    # Step checks
    api_ok = health_data is not None
    keys_ok = (
        config_data is not None
        and config_data.get("github", {}).get("token", "not set") != "not set"
        and config_data.get("embeddings", {}).get("voyage_api_key", "not set") != "not set"
    )
    repos_ok = bool(repos_data)
    # Webhook: at least one webhook_event exists
    webhook_ok = False
    if api_ok:
        events_data, _ = _api("/events?limit=1", timeout=5)
        webhook_ok = bool(events_data)

    token_ok = bool(st.session_state.get("last_token"))

    steps = [
        (_check(api_ok), "API server is running", "Go to **⚙️ Settings** to verify service health"),
        (_check(keys_ok), "API keys configured", "Go to **⚙️ Settings** → Edit Configuration"),
        (
            _check(repos_ok),
            "At least one repo registered",
            "Go to **📦 Repositories** → Add Repository",
        ),
        (
            _check(webhook_ok),
            "Webhook received at least one event",
            "Go to **🔗 Webhook Setup** for instructions",
        ),
        (_check(token_ok), "MCP token issued", "Go to **🔑 MCP Tokens** → Issue New Token"),
    ]

    all_done = all(s[0] == "✅" for s in steps)

    if all_done:
        st.success("All steps complete — your server is fully configured!")
    else:
        pending = sum(1 for s in steps if s[0] == "⬜")
        st.info(f"{pending} step(s) remaining.")

    st.divider()

    for icon, label, hint in steps:
        col_icon, col_label = st.columns([1, 10])
        with col_icon:
            st.markdown(f"## {icon}")
        with col_label:
            st.markdown(f"**{label}**")
            st.caption(hint)

    st.divider()
    st.subheader("Quick Reference")
    st.markdown("**Start all services:**")
    st.code(
        "# Terminal 1 — API server\n"
        "PYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES "
        "uvicorn src.api.app:app --port 8000 --reload\n\n"
        "# Terminal 2 — indexing worker\n"
        "PYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES "
        "rq worker indexing --url redis://localhost:6379\n\n"
        "# Terminal 3 — this dashboard\n"
        "PYTHONPATH=. API_URL=http://localhost:8000 "
        "streamlit run src/ui/dashboard.py --server.port 8501",
        language="bash",
    )


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Health  (original, preserved)
# ══════════════════════════════════════════════════════════════════════════════


def _render_health():
    st.title("🏠 Index Health")

    _, col_r2 = st.columns([3, 1])
    with col_r2:
        auto_refresh = st.toggle("Auto-refresh (10s)", value=False)

    health, err = _api("/health")
    if err:
        st.error(f"Cannot reach API: {err}")
        st.info("Start the API server: `uvicorn src.api.app:app --port 8000`")
        st.stop()

    st.subheader("Index Summary")
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Chunks", f"{health.get('chunks', 0):,}")
    k2.metric("Symbols", f"{health.get('symbols', 0):,}")
    k3.metric("Files", f"{health.get('files', 0):,}")
    k4.metric("Repos", f"{health.get('repos', 0):,}")
    k5.metric("Last indexed", _time_ago(health.get("last_indexed")))

    st.caption(
        f"Status: **{health.get('status', '?').upper()}**  |  "
        f"Last indexed: `{health.get('last_indexed', 'N/A')}`"
    )
    st.divider()

    st.subheader("Per-Repository Breakdown")
    rows, err = _api("/stats/repos", timeout=10)
    if err:
        st.warning(f"Could not load per-repo stats: {err}")
    elif rows:
        import pandas as pd

        df = pd.DataFrame(rows)
        df["repo"] = df["repo_owner"] + "/" + df["repo_name"]
        df = df[["repo", "active_chunks", "deleted_chunks", "files", "last_indexed"]]
        df.columns = ["Repository", "Active Chunks", "Soft-Deleted", "Files", "Last Indexed"]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No repositories indexed yet.")

    st.divider()
    st.subheader("Recently Indexed Files")
    files, err = _api("/stats/recent-files?limit=20", timeout=10)
    if err:
        st.warning(f"Could not load recent files: {err}")
    elif files:
        import pandas as pd

        df = pd.DataFrame(files)
        df["repo"] = df["repo_owner"] + "/" + df["repo_name"]
        df["commit"] = df["commit_sha"].apply(lambda x: x[:7] if x else "")
        df["ago"] = df["indexed_at"].apply(_time_ago)
        df = df[["file_path", "repo", "language", "token_count", "commit", "ago"]]
        df.columns = ["File", "Repo", "Lang", "Tokens", "Commit", "Indexed"]
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Chunk Size Distribution")
    buckets, err = _api("/stats/chunk-distribution", timeout=10)
    if buckets:
        try:
            import pandas as pd
            import plotly.express as px

            df = pd.DataFrame(buckets)
            fig = px.bar(
                df,
                x="bucket",
                y="count",
                title="Token Count per Chunk",
                color_discrete_sequence=["#4f8ff7"],
                labels={"bucket": "Token Range", "count": "Chunk Count"},
            )
            fig.update_layout(showlegend=False, height=300)
            st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.caption(f"Chart unavailable: {e}")

    if auto_refresh:
        time.sleep(10)
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Query Tester  (original, preserved)
# ══════════════════════════════════════════════════════════════════════════════


def _render_query_tester():
    st.title("🔍 Query Tester")
    st.caption("Test semantic, keyword, or hybrid search against your indexed codebase.")

    DEMO_QUERIES = [
        "what handles Shopify authentication and session management?",
        "where is the GraphQL product mutation defined?",
        "webhook handler for uninstalled app event",
        "chat widget toggle button and UI rendering",
        "how does the app configure Shopify API version and scopes?",
        "Prisma database session storage setup",
    ]

    with st.expander("Showcase demo queries (click to load)"):
        for i, q in enumerate(DEMO_QUERIES, 1):
            if st.button(f"{i}. {q}", key=f"demo_{i}", use_container_width=True):
                st.session_state["query"] = q

    st.divider()

    with st.form("search_form"):
        query = st.text_input(
            "Search query",
            value=st.session_state.get("query", ""),
            placeholder="e.g. 'what handles authentication?' or 'PaymentService.charge'",
        )
        c1, c2, c3, c4 = st.columns(4)
        mode = c1.selectbox("Mode", ["hybrid", "semantic", "keyword"], index=0)
        top_k = c2.number_input("Top K", min_value=1, max_value=20, value=5)
        rerank = c3.checkbox("Rerank", value=True)
        repo_filter = c4.text_input("Repo filter", placeholder="owner/name")
        submitted = st.form_submit_button("Search", use_container_width=True)

    if submitted and query.strip():
        payload = {
            "query": query,
            "mode": mode,
            "top_k": int(top_k),
            "rerank": rerank,
            "token_budget": 8000,
        }
        if repo_filter.strip():
            payload["repo"] = repo_filter.strip()

        with st.spinner(f"Searching ({mode} mode)…"):
            t0 = time.monotonic()
            data, err = _api("/search", method="POST", json=payload, timeout=60)
            elapsed = time.monotonic() - t0

        if err:
            st.error(f"Search failed: {err}")
        elif not data or not data.get("results"):
            st.warning("No results found.")
        else:
            results = data["results"]
            st.success(
                f"**{len(results)} results** in **{elapsed:.2f}s** "
                f"— {data.get('tokens_used', 0):,} tokens assembled"
            )

            import pandas as pd

            st.subheader("Results")
            rows = []
            for r in results:
                rows.append(
                    {
                        "Score": f"{r.get('score', 0):.4f}",
                        "File": r.get("file", ""),
                        "Lines": r.get("lines", ""),
                        "Symbol": r.get("symbol") or "—",
                        "Kind": r.get("kind") or "—",
                        "Language": r.get("language", ""),
                        "Commit": r.get("commit", ""),
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            st.subheader("Source Previews")
            for i, r in enumerate(results):
                lang = r.get("language", "text")
                label = (
                    f"[{r.get('score', 0):.4f}] {r.get('file', '')}  "
                    f"L{r.get('lines', '')}  —  {r.get('symbol') or '<module>'}"
                )
                with st.expander(label, expanded=(i == 0)):
                    st.code(r.get("preview", ""), language=lang)

            st.subheader("Assembled Context")
            st.caption(f"Retrieval log:\n```\n{data.get('retrieval_log', '')}\n```")
            if data.get("context"):
                st.text_area(
                    "Ready-to-inject context string",
                    value=data["context"],
                    height=300,
                    label_visibility="collapsed",
                )


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Activity Feed  (original, preserved)
# ══════════════════════════════════════════════════════════════════════════════


def _render_activity_feed():
    st.title("📡 Webhook Activity Feed")
    st.caption("Last 20 GitHub push events received and their indexing status.")

    _, col_right = st.columns([3, 1])
    with col_right:
        if st.button("Refresh", use_container_width=True):
            st.rerun()

    events, err = _api("/events?limit=20", timeout=10)

    if err:
        st.error(f"Could not load events: {err}")
    elif not events:
        st.info("No webhook events received yet.")
    else:
        _STATUS_ICON = {
            "queued": "🟡",
            "processing": "🔵",
            "done": "✅",
            "error": "❌",
            "skipped": "⬜",
        }
        for ev in events:
            icon = _STATUS_ICON.get(ev["status"], "❓")
            repo = f"{ev['repo_owner']}/{ev['repo_name']}" if ev["repo_owner"] else "—"
            commit = ev["commit_sha"][:7] if ev["commit_sha"] else "—"
            received = _time_ago(ev.get("received_at"))

            with st.container(border=True):
                c1, c2, c3, c4, c5 = st.columns([1, 3, 2, 2, 2])
                c1.markdown(f"## {icon}")
                c2.markdown(f"**{repo}**  \n`{commit}` — {ev['event_type']}")
                c3.metric("Files changed", ev["files_changed"])
                c4.markdown(f"**Status:** `{ev['status']}`  \n{received}")
                if ev.get("processed_at") and ev.get("received_at"):
                    from datetime import datetime

                    try:
                        t1 = datetime.fromisoformat(ev["received_at"])
                        t2 = datetime.fromisoformat(ev["processed_at"])
                        dur = (t2 - t1).total_seconds()
                        c5.metric("Duration", f"{dur:.1f}s")
                    except Exception:
                        c5.caption("—")
                else:
                    c5.caption("—")
                if ev.get("error_message"):
                    st.error(f"Error: {ev['error_message']}")
                st.caption(f"Delivery ID: `{ev.get('delivery_id') or '—'}`")


# ══════════════════════════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════════════════════════

if page == "🚀 Get Started":
    _render_get_started()

elif page == "⚙️  Settings":
    from src.ui._pages.settings import render

    render()

elif page == "📦 Repositories":
    from src.ui._pages.repos import render

    render()

elif page == "🕸️ Knowledge Graph":
    from src.ui._pages.knowledge_graph import render

    render()

elif page == "🔗 Webhook Setup":
    from src.ui._pages.webhook import render

    render()

elif page == "🔑 MCP Tokens":
    from src.ui._pages.tokens import render

    render()

elif page == "🏠 Health":
    _render_health()

elif page == "🔍 Query Tester":
    _render_query_tester()

elif page == "📡 Activity Feed":
    _render_activity_feed()

elif page == "🧩 Planning Mode":
    from src.ui._pages.planning import render

    render()

elif page == "💬 Ask Mode":
    from src.ui._pages.ask import render

    render()

elif page == "📜 History":
    from src.ui._pages.history import render

    render()

elif page == "🔌 MCP Servers":
    from src.ui._pages.mcp_servers import render

    render()

elif page == "🤖 Agent Roles":
    from src.ui._pages.agent_roles import render

    render()

elif page == "⚡ Workflows":
    from src.ui._pages.workflows import render

    render()

elif page == "📚 Documentation":
    from src.ui._pages.docs import render

    render()
