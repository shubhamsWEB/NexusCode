"""
LLM module to generate file summaries for retrieval.
"""

from __future__ import annotations

import structlog

from src.config import settings
from src.pipeline.chunker import RawChunk

logger = structlog.get_logger(__name__)


async def generate_file_summary(file_path: str, raw_content: str) -> RawChunk | None:
    """
    Generate a high-level summary of a file's purpose and key symbols.
    Returns a RawChunk representing the summary, or None if disabled/failed.
    """
    if not settings.enable_file_summaries:
        return None

    if not settings.anthropic_api_key:
        logger.warning("File summaries enabled but ANTHROPIC_API_KEY is not set")
        return None

    model = settings.default_model

    system = "You are an expert software engineer. Summarize the provided file for a semantic search engine."

    # Truncate raw content to avoid spending too much on very large files
    # ~20,000 chars is roughly 5k tokens
    truncated_content = raw_content[:20000]

    prompt = f"""File Path: {file_path}

Please provide a concise summary (max 3-4 sentences) of this file. Focus on:
1. The primary purpose of the file.
2. The core concepts or features it implements.
3. The most important classes or functions.

Do not wrap the whole response in markdown backticks. Be direct and concise. Start immediately with the summary.

Content:
```
{truncated_content}
```
"""

    try:
        from src.llm.client import get_client

        client = get_client()
        resp = await client.messages.create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
        )
        text = resp.content[0].text if resp.content else None

        if text:
            from src.pipeline.chunker import count_tokens

            lines = raw_content.count("\n") + 1
            return RawChunk(
                file_path=file_path,
                language="markdown",  # Treat summary chunks as markdown
                start_line=1,
                end_line=lines,
                raw_content=text,
                symbol_name="File Summary",
                symbol_kind="file_summary",
                scope_chain=None,
                parent_symbol_name=None,
                imports=[],
                token_count=count_tokens(text),
            )
    except Exception as exc:
        logger.warning("Failed to generate file summary", file_path=file_path, error=str(exc))

    return None
