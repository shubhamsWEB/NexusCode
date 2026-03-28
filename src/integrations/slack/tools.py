"""MCP-compatible tool schemas for Slack integration."""

SLACK_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "slack_send_message",
        "description": (
            "Send a message to a Slack channel. Use this to notify the team of workflow "
            "completions, alerts, or status updates. Returns the message timestamp.\n"
            "channel can be a channel name (#general) or channel ID (C01234567)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name or ID"},
                "text": {"type": "string", "description": "Message text (supports Slack markdown: *bold*, _italic_, `code`)"},
                "thread_ts": {"type": "string", "description": "Thread timestamp to reply to (optional)"},
            },
            "required": ["channel", "text"],
        },
    },
    {
        "name": "slack_get_channel_history",
        "description": (
            "Fetch recent messages from a Slack channel. Useful for reading team discussions, "
            "incident reports, or decision threads."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name or ID"},
                "limit": {"type": "integer", "default": 20, "description": "Max messages to fetch (1-100)"},
            },
            "required": ["channel"],
        },
    },
    {
        "name": "slack_list_channels",
        "description": "List available Slack channels. Use this to find the right channel ID before sending a message.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]
