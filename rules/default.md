# Agent Rules — Global

## Citation Format
- Always cite files as `path/to/file.py:L42-L80` (file path + line range)
- Only cite files you found via tool calls — never invent paths

## Search Strategy
- Use search_codebase first; use get_symbol when you know the exact name
- Search 2–3 times with different angles before concluding something doesn't exist
- Use find_callers to assess blast radius before planning any change

## Answer Quality
- Ground every factual claim in a specific file + line number
- If something is unclear after 3 searches, say so explicitly — do not guess
