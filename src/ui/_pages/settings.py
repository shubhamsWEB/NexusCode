import streamlit as st
from src.ui.helpers import api_get, api_post


def render():
    st.title("Settings")
    st.markdown("Configure the Codebase Intelligence MCP Server.")

    # --- Section 1: Service Health Badges ---
    st.subheader("Service Health")

    health_data, health_err = api_get("/health", timeout=5)
    config_data, config_err = api_get("/config", timeout=5)

    db_ok = health_data is not None and not health_err
    github_ok = None
    voyage_ok = None
    redis_ok = None

    if config_data and not config_err:
        github_section = config_data.get("github", {})
        token_val = github_section.get("token", "not set")
        github_ok = token_val not in ("not set", "", None)

        embeddings_section = config_data.get("embeddings", {})
        voyage_key = embeddings_section.get("voyage_api_key", "not set")
        voyage_ok = voyage_key not in ("not set", "", None)

        redis_section = config_data.get("redis", {})
        redis_url = redis_section.get("url", "")
        redis_ok = bool(redis_url and redis_url.strip())

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if db_ok:
            st.markdown("**PostgreSQL**\n\n:white_check_mark: Connected")
        else:
            st.markdown("**PostgreSQL**\n\n:x: Unavailable")

    with col2:
        if github_ok is True:
            st.markdown("**GitHub API**\n\n:white_check_mark: Token set")
        elif github_ok is False:
            st.markdown("**GitHub API**\n\n:warning: No token")
        else:
            st.markdown("**GitHub API**\n\n:warning: Unknown")

    with col3:
        if voyage_ok is True:
            st.markdown("**Voyage AI**\n\n:white_check_mark: Key set")
        elif voyage_ok is False:
            st.markdown("**Voyage AI**\n\n:x: Key missing")
        else:
            st.markdown("**Voyage AI**\n\n:x: Unknown")

    with col4:
        if redis_ok is True:
            st.markdown("**Redis**\n\n:white_check_mark: URL set")
        elif redis_ok is False:
            st.markdown("**Redis**\n\n:x: No URL")
        else:
            st.markdown("**Redis**\n\n:x: Unknown")

    st.divider()

    # --- Section 2: Current Configuration ---
    st.subheader("Current Configuration")

    if config_err:
        st.error(f"Could not load configuration: {config_err}")
    elif config_data:
        def show_section(title, section_key, fields):
            section = config_data.get(section_key, {})
            with st.expander(title, expanded=False):
                rows = {k: str(section.get(k, "not set")) for k in fields}
                table_data = {"Key": list(rows.keys()), "Value": list(rows.values())}
                st.table(table_data)

        show_section(
            "GitHub",
            "github",
            ["token", "webhook_secret", "default_branch", "app_id"],
        )

        show_section(
            "Embeddings",
            "embeddings",
            ["voyage_api_key", "model", "dimensions", "batch_size"],
        )

        show_section(
            "Auth",
            "auth",
            ["jwt_secret", "jwt_expiry_hours"],
        )

        show_section(
            "Indexing",
            "indexing",
            ["chunk_target_tokens", "chunk_overlap_tokens", "supported_extensions", "ignore_patterns"],
        )

        show_section(
            "Database",
            "database",
            ["url", "pool_size"],
        )

        show_section(
            "Optional",
            "optional",
            ["anthropic_api_key"],
        )
    else:
        st.info("No configuration data available.")

    st.divider()

    # --- Section 3: Edit Configuration ---
    st.subheader("Edit Configuration")

    with st.form("settings_form"):
        st.markdown("**GitHub**")
        github_token = st.text_input("GITHUB_TOKEN", type="password", placeholder="ghp_...")
        st.caption("Leave blank to keep current value.")

        github_webhook_secret = st.text_input("GITHUB_WEBHOOK_SECRET", type="password", placeholder="your-webhook-secret")
        st.caption("Leave blank to keep current value.")

        github_default_branch = st.text_input("GITHUB_DEFAULT_BRANCH", value="", placeholder="main")

        st.markdown("**Embeddings**")
        voyage_api_key = st.text_input("VOYAGE_API_KEY", type="password", placeholder="pa-...")
        st.caption("Leave blank to keep current value.")

        st.markdown("**Optional**")
        anthropic_api_key = st.text_input("ANTHROPIC_API_KEY", type="password", placeholder="sk-ant-...")
        st.caption("Leave blank to keep current value.")

        st.markdown("**Auth**")
        jwt_secret = st.text_input("JWT_SECRET", type="password", placeholder="your-jwt-secret")
        st.caption("Leave blank to keep current value.")

        jwt_expiry_hours = st.number_input(
            "JWT_EXPIRY_HOURS",
            min_value=1,
            max_value=72,
            value=8,
            step=1,
        )

        st.markdown("**Indexing**")
        chunk_target_tokens = st.number_input(
            "CHUNK_TARGET_TOKENS",
            min_value=128,
            max_value=2048,
            value=512,
            step=1,
        )

        chunk_overlap_tokens = st.number_input(
            "CHUNK_OVERLAP_TOKENS",
            min_value=0,
            max_value=512,
            value=128,
            step=1,
        )

        supported_extensions = st.text_input(
            "SUPPORTED_EXTENSIONS",
            placeholder=".py,.ts,.go,...",
        )

        ignore_patterns = st.text_input(
            "IGNORE_PATTERNS",
            placeholder="node_modules,.git,...",
        )

        submitted = st.form_submit_button("Save Configuration")

    if submitted:
        raw_fields = {
            "GITHUB_TOKEN": github_token,
            "GITHUB_WEBHOOK_SECRET": github_webhook_secret,
            "GITHUB_DEFAULT_BRANCH": github_default_branch,
            "VOYAGE_API_KEY": voyage_api_key,
            "ANTHROPIC_API_KEY": anthropic_api_key,
            "JWT_SECRET": jwt_secret,
            "JWT_EXPIRY_HOURS": str(int(jwt_expiry_hours)),
            "CHUNK_TARGET_TOKENS": str(int(chunk_target_tokens)),
            "CHUNK_OVERLAP_TOKENS": str(int(chunk_overlap_tokens)),
            "SUPPORTED_EXTENSIONS": supported_extensions,
            "IGNORE_PATTERNS": ignore_patterns,
        }

        updates = {k: v for k, v in raw_fields.items() if isinstance(v, str) and v.strip()}

        if not updates:
            st.info("No changes to save.")
        else:
            result, err = api_post("/config/env", json={"updates": updates}, timeout=10)
            if err:
                st.error(f"Failed to save configuration: {err}")
            else:
                st.success("Configuration saved successfully.")

    st.divider()

    # --- Section 4: Danger Zone ---
    st.subheader("Danger Zone")
    st.warning("Restart the API server after saving for changes to take effect.")
    st.code("PYTHONPATH=. uvicorn src.api.app:app --port 8000 --reload", language="bash")
