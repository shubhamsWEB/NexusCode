"""
Shared utilities for all dashboard pages.
Import API_URL from st.session_state in each page (set by the sidebar in dashboard.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import streamlit as st


def api_get(path: str, timeout: int = 15):
    """GET request to the API server. Returns (data, error)."""
    url = f"{st.session_state.get('api_url', 'http://localhost:8000')}{path}"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json(), None
    except httpx.TimeoutException:
        return None, "Request timed out — is the API server running?"
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        return None, str(e)


def api_post(path: str, json=None, timeout: int = 30):
    """POST request to the API server. Returns (data, error)."""
    url = f"{st.session_state.get('api_url', 'http://localhost:8000')}{path}"
    try:
        resp = httpx.post(url, json=json, timeout=timeout)
        resp.raise_for_status()
        return resp.json(), None
    except httpx.TimeoutException:
        return None, "Request timed out."
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        return None, str(e)


def api_patch(path: str, json=None, timeout: int = 15):
    """PATCH request to the API server. Returns (data, error)."""
    url = f"{st.session_state.get('api_url', 'http://localhost:8000')}{path}"
    try:
        resp = httpx.patch(url, json=json, timeout=timeout)
        resp.raise_for_status()
        return resp.json(), None
    except httpx.TimeoutException:
        return None, "Request timed out."
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        return None, str(e)


def api_delete(path: str, timeout: int = 15):
    """DELETE request to the API server. Returns (data, error)."""
    url = f"{st.session_state.get('api_url', 'http://localhost:8000')}{path}"
    try:
        resp = httpx.delete(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json(), None
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        return None, str(e)


def time_ago(ts_str: str | None) -> str:
    """Convert an ISO timestamp string to a human-readable 'X ago' string."""
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
        return ts_str or "—"


def status_badge(status: str) -> str:
    """Return a coloured emoji badge for a repo/job status string."""
    return {
        "pending": "⬜ pending",
        "indexing": "🔵 indexing",
        "ready": "✅ ready",
        "error": "❌ error",
        "queued": "🟡 queued",
        "started": "🔵 started",
        "finished": "✅ finished",
        "failed": "❌ failed",
        "done": "✅ done",
        "skipped": "⬜ skipped",
    }.get(status, f"❓ {status}")
