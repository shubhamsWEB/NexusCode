import os
import sys

# Ensure repo root is on the path (no-op when launched via dashboard.py)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

import streamlit as st

from src.ui.helpers import api_get, api_post, time_ago


def _fetch_events(owner, name):
    """Fetch recent webhook events via the API (avoids asyncio.run / asyncpg conflicts)."""
    path = "/events?limit=5"
    if owner:
        path += f"&repo_owner={owner}"
    if name:
        path += f"&repo_name={name}"
    data, err = api_get(path, timeout=10)
    if err or not data:
        return []
    return data


def render():
    st.title("Webhook Setup Wizard")
    st.markdown(
        "Follow these steps to connect your GitHub repository to the Codebase Intelligence MCP Server."
    )

    # ── Step 1 — Your Webhook URL ────────────────────────────────────────────
    st.subheader("Step 1 — Your Webhook URL")

    api_url = st.session_state.get("api_url", "http://localhost:8000")

    custom_base = st.text_input(
        "Public base URL (override for ngrok / Railway / etc.)",
        value=api_url,
        help="Change this if your server is exposed via a tunnel or cloud deployment.",
    )

    webhook_url = custom_base.rstrip("/") + "/webhook"

    st.markdown("**Your webhook endpoint:**")
    st.code(webhook_url, language="text")

    st.caption("For local development, expose port 8000 with: `ngrok http 8000`")

    st.divider()

    # ── Step 2 — Webhook Secret ───────────────────────────────────────────────
    st.subheader("Step 2 — Webhook Secret")

    config_data, config_err = api_get("/config", timeout=10)

    if config_err:
        st.warning(f"Could not load configuration: {config_err}")
        masked_secret = "unavailable"
    else:
        masked_secret = (
            config_data.get("github", {}).get("webhook_secret", "not set")
            if config_data
            else "not set"
        )

    st.markdown("**Current webhook secret (masked):**")
    st.code(masked_secret, language="text")

    st.caption(
        "Set GITHUB_WEBHOOK_SECRET in your .env to change this value. "
        "It must match exactly what you enter in GitHub."
    )

    st.markdown("**Add to your `.env` file:**")
    st.code("GITHUB_WEBHOOK_SECRET=your-secret-here", language="bash")

    st.divider()

    # ── Step 3 — Configure GitHub ─────────────────────────────────────────────
    st.subheader("Step 3 — Configure GitHub")

    st.markdown(
        f"""
1. Go to your GitHub repository → **Settings** → **Webhooks** → **Add webhook**
2. Set **Payload URL** to: `{webhook_url}`
3. Set **Content type** to: `application/json`
4. Set **Secret** to your `GITHUB_WEBHOOK_SECRET` value
5. Under "Which events...", select **"Just the push event"**
6. Ensure **Active** is checked
7. Click **Add webhook**
"""
    )

    st.info("GitHub will immediately send a ping event. Use Step 4 to verify it was received.")

    with st.expander("GitHub App alternative (higher rate limits)"):
        st.markdown(
            """
**GitHub App** authentication gives you **15,000 requests/hour** instead of the standard
5,000 req/hr you get with a personal `GITHUB_TOKEN`.

To switch to a GitHub App:

1. Create a GitHub App in your organization or account settings.
2. Grant it **Contents** (read) and **Webhooks** (read & write) permissions.
3. Install the App on the repositories you want to index.
4. Set the following environment variables in your `.env`:

```bash
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_PATH=/path/to/private-key.pem
```

Leave `GITHUB_TOKEN` unset (or remove it) so the server prefers App authentication.
"""
        )

    st.divider()

    # ── Step 4 — Test the Connection ──────────────────────────────────────────
    st.subheader("Step 4 — Test the Connection")

    if st.button("Send Test Ping", type="primary"):
        with st.spinner("Sending ping to /webhook..."):
            ping_data, ping_err = api_post("/webhook/ping", json={}, timeout=15)

        if ping_err:
            st.error(f"Ping failed: {ping_err}")
            st.markdown(
                "Make sure the server is running and reachable at "
                f"`{api_url}`. If you changed the public base URL above, "
                "the ping still targets the configured API URL."
            )
        elif ping_data and ping_data.get("ok"):
            st.success("Webhook is live! The server received the ping.")
            delivery_id = ping_data.get("delivery_id", "unknown")
            st.caption(f"Delivery ID: `{delivery_id}`")
        else:
            status_code = ping_data.get("status_code") if ping_data else "unknown"
            st.error(f"Ping returned an unexpected response (status {status_code}).")
            st.json(ping_data or {})

    st.divider()

    # ── Step 5 — Recent Events ────────────────────────────────────────────────
    st.subheader("Step 5 — Recent Events (confirmation)")

    repos_data, repos_err = api_get("/repos", timeout=10)

    repo_options = ["All repos"]
    repo_map = {}

    if repos_err:
        st.warning(f"Could not load repository list: {repos_err}")
    elif repos_data:
        for repo in repos_data:
            owner = repo.get("owner", "")
            name = repo.get("name", "")
            if owner and name:
                label = f"{owner}/{name}"
                repo_options.append(label)
                repo_map[label] = (owner, name)

    selected_label = st.selectbox("Filter by repository", options=repo_options)

    if selected_label == "All repos":
        filter_owner, filter_name = None, None
    else:
        filter_owner, filter_name = repo_map.get(selected_label, (None, None))

    STATUS_ICONS = {
        "done": "✅",
        "error": "❌",
        "queued": "🟡",
        "processing": "🔵",
    }

    events = _fetch_events(filter_owner, filter_name)

    if not events:
        repo_label = selected_label if selected_label != "All repos" else "this repo"
        st.info(f"No webhook events received yet for {repo_label}.")
    else:
        for event in events:
            status = event.get("status", "unknown")
            icon = STATUS_ICONS.get(status, "⬜")
            event_type = event.get("event_type", "unknown")
            files_changed = event.get("files_changed")
            received_at = event.get("received_at")
            processed_at = event.get("processed_at")
            error_message = event.get("error_message")

            received_str = time_ago(str(received_at)) if received_at else "unknown"
            processed_str = time_ago(str(processed_at)) if processed_at else None

            with st.container():
                col1, col2, col3 = st.columns([1, 3, 3])
                with col1:
                    st.markdown(f"**{icon} {status}**")
                with col2:
                    st.markdown(f"`{event_type}`")
                    if files_changed is not None:
                        st.caption(f"{files_changed} file(s) changed")
                with col3:
                    st.caption(f"Received: {received_str}")
                    if processed_str:
                        st.caption(f"Processed: {processed_str}")

                if error_message:
                    st.error(f"Error: {error_message}")

                st.markdown("---")
