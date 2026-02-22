"""
Codebase Intelligence — Admin Dashboard

Pages:
  🏠 Health        — index stats, per-repo breakdown, auto-refresh
  🔍 Query Tester  — live search UI with results + assembled context
  📡 Activity Feed — last 20 webhook events with status + timing
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import streamlit as st

# ── Path setup (works when run from repo root or src/ui/) ─────────────────────
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

# ── Sidebar navigation ────────────────────────────────────────────────────────
st.sidebar.title("🧠 Codebase Intelligence")
st.sidebar.caption("MCP Knowledge Server")
page = st.sidebar.radio(
    "Navigate",
    ["🏠 Health", "🔍 Query Tester", "📡 Activity Feed"],
    label_visibility="collapsed",
)
st.sidebar.divider()

API_URL = st.sidebar.text_input(
    "API base URL",
    value=os.environ.get("API_URL", "http://localhost:8000"),
)
st.sidebar.caption(f"MCP endpoint: `{API_URL}/mcp`")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _api(path: str, method: str = "GET", json=None, timeout: int = 30):
    import httpx
    url = f"{API_URL}{path}"
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
        delta = datetime.now(timezone.utc) - ts
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
# PAGE 1 — Health
# ══════════════════════════════════════════════════════════════════════════════

if page == "🏠 Health":
    st.title("🏠 Index Health")

    # Auto-refresh toggle
    col_r1, col_r2 = st.columns([3, 1])
    with col_r2:
        auto_refresh = st.toggle("Auto-refresh (10s)", value=False)

    health, err = _api("/health")
    if err:
        st.error(f"Cannot reach API: {err}")
        st.info("Start the API server: `uvicorn src.api.app:app --port 8000`")
        st.stop()

    # ── Top KPI row ───────────────────────────────────────────────────────────
    st.subheader("Index Summary")
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Chunks", f"{health.get('chunks', 0):,}")
    k2.metric("Symbols", f"{health.get('symbols', 0):,}")
    k3.metric("Files", f"{health.get('files', 0):,}")
    k4.metric("Repos", f"{health.get('repos', 0):,}")
    k5.metric("Last indexed", _time_ago(health.get("last_indexed")))

    st.caption(f"Status: **{health.get('status', '?').upper()}**  |  "
               f"Last indexed: `{health.get('last_indexed', 'N/A')}`")

    st.divider()

    # ── Per-repo breakdown (from DB) ──────────────────────────────────────────
    st.subheader("Per-Repository Breakdown")
    try:
        import asyncio
        from sqlalchemy import text
        from src.storage.db import AsyncSessionLocal

        async def _repo_stats():
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(text("""
                    SELECT
                        repo_owner, repo_name,
                        COUNT(*) FILTER (WHERE is_deleted = FALSE) AS active_chunks,
                        COUNT(*) FILTER (WHERE is_deleted = TRUE)  AS deleted_chunks,
                        COUNT(DISTINCT file_path)
                            FILTER (WHERE is_deleted = FALSE)       AS files,
                        MAX(indexed_at)                             AS last_indexed
                    FROM chunks
                    GROUP BY repo_owner, repo_name
                    ORDER BY active_chunks DESC
                """))).mappings().all()
                return [dict(r) for r in rows]

        rows = asyncio.run(_repo_stats())
        if rows:
            import pandas as pd
            df = pd.DataFrame(rows)
            df["repo"] = df["repo_owner"] + "/" + df["repo_name"]
            df["last_indexed"] = df["last_indexed"].apply(
                lambda x: x.isoformat() if x else None
            )
            df = df[["repo", "active_chunks", "deleted_chunks", "files", "last_indexed"]]
            df.columns = ["Repository", "Active Chunks", "Soft-Deleted", "Files", "Last Indexed"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No repositories indexed yet. Run `scripts/full_index.py` to index a repo.")
    except Exception as e:
        st.warning(f"Could not load per-repo stats: {e}")

    st.divider()

    # ── Recent files ──────────────────────────────────────────────────────────
    st.subheader("Recently Indexed Files")
    try:
        async def _recent_files():
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(text("""
                    SELECT file_path, repo_owner, repo_name,
                           language, token_count, commit_sha, indexed_at
                    FROM chunks
                    WHERE is_deleted = FALSE
                    ORDER BY indexed_at DESC
                    LIMIT 20
                """))).mappings().all()
                return [dict(r) for r in rows]

        files = asyncio.run(_recent_files())
        if files:
            import pandas as pd
            df = pd.DataFrame(files)
            df["repo"] = df["repo_owner"] + "/" + df["repo_name"]
            df["commit"] = df["commit_sha"].apply(lambda x: x[:7] if x else "")
            df["ago"] = df["indexed_at"].apply(
                lambda x: _time_ago(x.isoformat() if x else None)
            )
            df = df[["file_path", "repo", "language", "token_count", "commit", "ago"]]
            df.columns = ["File", "Repo", "Lang", "Tokens", "Commit", "Indexed"]
            st.dataframe(df, use_container_width=True, hide_index=True)
    except Exception as e:
        st.warning(f"Could not load recent files: {e}")

    # ── Chunk distribution chart ──────────────────────────────────────────────
    st.divider()
    st.subheader("Chunk Size Distribution")
    try:
        async def _chunk_dist():
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(text("""
                    SELECT
                        CASE
                            WHEN token_count < 100  THEN '<100'
                            WHEN token_count < 200  THEN '100-199'
                            WHEN token_count < 300  THEN '200-299'
                            WHEN token_count < 400  THEN '300-399'
                            WHEN token_count < 512  THEN '400-511'
                            ELSE '512+'
                        END AS bucket,
                        COUNT(*) AS count
                    FROM chunks
                    WHERE is_deleted = FALSE AND token_count IS NOT NULL
                    GROUP BY bucket
                    ORDER BY bucket
                """))).mappings().all()
                return [dict(r) for r in rows]

        buckets = asyncio.run(_chunk_dist())
        if buckets:
            import pandas as pd
            import plotly.express as px
            df = pd.DataFrame(buckets)
            fig = px.bar(df, x="bucket", y="count", title="Token Count per Chunk",
                         color_discrete_sequence=["#4f8ff7"],
                         labels={"bucket": "Token Range", "count": "Chunk Count"})
            fig.update_layout(showlegend=False, height=300)
            st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.caption(f"Chart unavailable: {e}")

    if auto_refresh:
        time.sleep(10)
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 2 — Query Tester
# ══════════════════════════════════════════════════════════════════════════════

elif page == "🔍 Query Tester":
    st.title("🔍 Query Tester")
    st.caption("Test semantic, keyword, or hybrid search against your indexed codebase.")

    # ── Demo queries ──────────────────────────────────────────────────────────
    DEMO_QUERIES = [
        "what handles Shopify authentication and session management?",
        "where is the GraphQL product mutation defined?",
        "webhook handler for uninstalled app event",
        "chat widget toggle button and UI rendering",
        "how does the app configure Shopify API version and scopes?",
        "Prisma database session storage setup",
    ]

    with st.expander("📋 Showcase demo queries (click to load)"):
        for i, q in enumerate(DEMO_QUERIES, 1):
            if st.button(f"{i}. {q}", key=f"demo_{i}", use_container_width=True):
                st.session_state["query"] = q

    st.divider()

    # ── Search form ───────────────────────────────────────────────────────────
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
        submitted = st.form_submit_button("🔍 Search", use_container_width=True)

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
            st.warning("No results found. Try a different query or mode.")
        else:
            results = data["results"]
            st.success(
                f"**{len(results)} results** in **{elapsed:.2f}s** "
                f"— {data.get('tokens_used', 0):,} tokens assembled"
            )

            # Results table
            st.subheader("Results")
            import pandas as pd
            rows = []
            for r in results:
                rows.append({
                    "Score": f"{r.get('score', 0):.4f}",
                    "File": r.get("file", ""),
                    "Lines": r.get("lines", ""),
                    "Symbol": r.get("symbol") or "—",
                    "Kind": r.get("kind") or "—",
                    "Language": r.get("language", ""),
                    "Commit": r.get("commit", ""),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            # Code previews
            st.subheader("Source Previews")
            for i, r in enumerate(results):
                lang = r.get("language", "text")
                label = f"[{r.get('score', 0):.4f}] {r.get('file', '')}  L{r.get('lines', '')}  —  {r.get('symbol') or '<module>'}"
                with st.expander(label, expanded=(i == 0)):
                    st.code(r.get("preview", ""), language=lang)

            # Assembled context
            st.subheader("Assembled Context")
            st.caption(
                f"Retrieval log:\n```\n{data.get('retrieval_log', '')}\n```"
            )
            if data.get("context"):
                st.text_area(
                    "Ready-to-inject context string",
                    value=data["context"],
                    height=300,
                    label_visibility="collapsed",
                )


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 3 — Activity Feed
# ══════════════════════════════════════════════════════════════════════════════

elif page == "📡 Activity Feed":
    st.title("📡 Webhook Activity Feed")
    st.caption("Last 20 GitHub push events received and their indexing status.")

    col_left, col_right = st.columns([3, 1])
    with col_right:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    try:
        import asyncio
        from sqlalchemy import text
        from src.storage.db import AsyncSessionLocal

        async def _events():
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(text("""
                    SELECT
                        delivery_id, event_type,
                        repo_owner, repo_name, commit_sha,
                        files_changed, status, error_message,
                        received_at, processed_at
                    FROM webhook_events
                    ORDER BY received_at DESC
                    LIMIT 20
                """))).mappings().all()
                return [dict(r) for r in rows]

        events = asyncio.run(_events())

        if not events:
            st.info("No webhook events received yet. Push a commit to your GitHub repo "
                    "(with a webhook pointing here) to see activity.")
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
                received = _time_ago(ev["received_at"].isoformat() if ev["received_at"] else None)

                with st.container(border=True):
                    c1, c2, c3, c4, c5 = st.columns([1, 3, 2, 2, 2])
                    c1.markdown(f"## {icon}")
                    c2.markdown(f"**{repo}**  \n`{commit}` — {ev['event_type']}")
                    c3.metric("Files changed", ev["files_changed"])
                    c4.markdown(f"**Status:** `{ev['status']}`  \n{received}")
                    if ev["processed_at"] and ev["received_at"]:
                        duration = (ev["processed_at"] - ev["received_at"]).total_seconds()
                        c5.metric("Duration", f"{duration:.1f}s")
                    else:
                        c5.caption("—")
                    if ev["error_message"]:
                        st.error(f"Error: {ev['error_message']}")
                    st.caption(f"Delivery ID: `{ev['delivery_id'] or '—'}`")

    except Exception as e:
        st.error(f"Could not load events: {e}")
        st.exception(e)
