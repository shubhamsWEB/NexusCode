"""
Shared utilities for all dashboard pages.
Import API_URL from st.session_state in each page (set by the sidebar in dashboard.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import streamlit as st


def _api_headers() -> dict[str, str]:
    """Build request headers, including X-Api-Key if one is configured in the sidebar."""
    key = st.session_state.get("api_key", "").strip()
    if key:
        return {"X-Api-Key": key}
    return {}


def api_get(path: str, timeout: int = 15):
    """GET request to the API server. Returns (data, error)."""
    url = f"{st.session_state.get('api_url', 'http://localhost:8000')}{path}"
    try:
        resp = httpx.get(url, headers=_api_headers(), timeout=timeout)
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
        resp = httpx.post(url, json=json, headers=_api_headers(), timeout=timeout)
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
        resp = httpx.patch(url, json=json, headers=_api_headers(), timeout=timeout)
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
        resp = httpx.delete(url, headers=_api_headers(), timeout=timeout)
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


# ── Agent execution timeline ──────────────────────────────────────────────────

AGENT_TOOL_ICONS: dict[str, str] = {
    "search_codebase":            "🔍",
    "get_symbol":                 "🎯",
    "find_callers":               "📡",
    "get_file_context":           "📄",
    "answer_question":            "💬",
    "output_implementation_plan": "📋",
    "analyze_and_improve":        "🔬",
}
AGENT_DEFAULT_ICON = "🔧"


def render_agent_timeline_html(steps: list[dict]) -> str:
    """
    Build a Cursor/Copilot-style execution timeline as styled HTML.

    Each step is a dict with these keys (flexible — both ask and plan formats):
      type:    "tool_call" | "thinking" | "error"  (or inferred from 'tool' key)
      tool:    str          tool name ("_thinking" for thinking blocks in ask.py)
      state:   "running" | "done" | "error"
      summary: str          human-readable input / preview
      tokens:  int | None
    """
    if not steps:
        return ""

    rows: list[str] = []
    for step in steps:
        stype = step.get("type", "tool_call")
        tool  = step.get("tool", "")

        # Normalise: ask.py stores thinking as tool="_thinking"
        is_thinking = stype == "thinking" or tool == "_thinking"
        is_error    = stype == "error"

        if is_thinking:
            preview = step.get("summary", step.get("text", ""))
            if len(preview) > 120:
                preview = preview[:120] + "…"
            rows.append(
                '<div class="tl-row tl-think">'
                '<span class="tl-ic">💭</span>'
                f'<span class="tl-lbl"><em>{preview}</em></span>'
                '</div>'
            )
            continue

        if is_error:
            msg = step.get("message", step.get("summary", ""))[:100]
            rows.append(
                '<div class="tl-row">'
                '<span class="tl-ic">❌</span>'
                f'<span class="tl-lbl" style="color:#f85149">{msg}</span>'
                '</div>'
            )
            continue

        # Tool call row
        state   = step.get("state", "done")
        summary = step.get("summary", "")
        tokens  = step.get("tokens")
        icon    = AGENT_TOOL_ICONS.get(tool, AGENT_DEFAULT_ICON)

        state_sym, state_color = {
            "running": ("⏳", "#d29922"),
            "done":    ("✓",  "#3fb950"),
            "error":   ("✗",  "#f85149"),
        }.get(state, ("✓", "#3fb950"))

        summary_html = (
            f'<span class="tl-sum">{summary[:65]}</span>' if summary else ""
        )
        token_html = (
            f'<span class="tl-tok">{tokens:,}t</span>' if tokens else ""
        )

        rows.append(
            '<div class="tl-row">'
            f'<span class="tl-st" style="color:{state_color}">{state_sym}</span>'
            f'<span class="tl-ic">{icon}</span>'
            f'<span class="tl-tool">{tool}</span>'
            f'{summary_html}'
            f'{token_html}'
            '</div>'
        )

    css = """<style>
.tl-wrap{font-family:ui-monospace,'Cascadia Code',Consolas,monospace;
  background:#0d1117;border-radius:8px;padding:6px 10px;
  border:1px solid #30363d;max-height:300px;overflow-y:auto}
.tl-row{display:flex;align-items:center;gap:7px;padding:4px 0;
  border-bottom:1px solid #21262d;min-width:0}
.tl-row:last-child{border-bottom:none}
.tl-think{align-items:flex-start}
.tl-st{font-size:11px;font-weight:700;flex-shrink:0;width:14px;text-align:center}
.tl-ic{font-size:13px;flex-shrink:0}
.tl-tool{font-size:12px;font-weight:600;color:#e6edf3;flex-shrink:0}
.tl-sum{font-size:11px;color:#8b949e;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:1;min-width:0}
.tl-tok{font-size:11px;color:#6e7681;margin-left:auto;flex-shrink:0}
.tl-lbl{font-size:12px;color:#8b949e;flex:1}
</style>"""
    return f'{css}<div class="tl-wrap">{"".join(rows)}</div>'


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
