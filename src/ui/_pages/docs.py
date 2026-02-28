"""
📚 Documentation — renders all doc/*.md files in the Streamlit dashboard.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

# ── Doc file registry ──────────────────────────────────────────────────────────
# (label, filename) — order matches the logical reading flow
_DOC_FILES: list[tuple[str, str]] = [
    ("Overview", "README.md"),
    ("Getting Started", "getting-started.md"),
    ("Connecting GitHub", "connecting-github.md"),
    ("MCP Server & Auth", "mcp-access.md"),
    ("Search, Ask & Planning", "search-and-ask.md"),
    ("Custom Skills", "custom-skills.md"),
    ("API Reference", "api-reference.md"),
    ("Configuration", "configuration.md"),
    ("Deployment", "deployment.md"),
]

_DOCS_DIR = Path(__file__).resolve().parents[3] / "doc"


def _read_doc(filename: str) -> str | None:
    path = _DOCS_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as exc:
        return f"⚠️ Could not load `{filename}`: {exc}"


def render() -> None:
    st.title("📚 Documentation")
    st.caption("Full reference docs for NexusCode — select a section from the sidebar below.")

    # Build list of available docs (skip missing files gracefully)
    available: list[tuple[str, str, str]] = []  # (label, filename, content)
    for label, filename in _DOC_FILES:
        content = _read_doc(filename)
        if content is not None:
            available.append((label, filename, content))

    if not available:
        st.error(
            f"No documentation files found in `{_DOCS_DIR}`. "
            "Make sure the `doc/` directory exists at the project root."
        )
        return

    labels = [label for label, _, _ in available]

    # Section selector — radio in sidebar area via st.selectbox for compactness
    col_nav, col_content = st.columns([1, 3])

    with col_nav:
        st.markdown("### Sections")
        selected_label = st.radio(
            "docs_section",
            labels,
            label_visibility="collapsed",
        )
        st.divider()
        # Quick-links to doc source files
        st.caption("**Source files**")
        for label, filename, _ in available:
            icon = "→" if label == selected_label else " "
            st.caption(f"`{icon} doc/{filename}`")

    with col_content:
        # Find selected content
        content = next((c for lbl, _, c in available if lbl == selected_label), "")

        # Render the markdown content
        st.markdown(content, unsafe_allow_html=False)
