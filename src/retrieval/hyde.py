"""
HyDE (Hypothetical Document Embeddings) Query Expansion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from src.config import settings
from src.llm.registry import get_provider

if TYPE_CHECKING:
    from src.retrieval.searcher import SearchResult

logger = structlog.get_logger(__name__)


async def generate_hypothetical_code(query: str, language: str | None = None) -> str:
    """Generate a hypothetical code snippet that answers the query."""
    provider = get_provider(settings.default_model)

    lang_hint = f" in {language}" if language else ""
    system = "You are an expert developer. You write code that answers questions."
    prompt = f"""
Write a hypothetical code snippet{lang_hint} that perfectly answers the following query.
Do not write explanations, just the code. The code doesn't need to be complete, just structurally representative.

Query: {query}
"""
    try:
        response = await provider.generate(
            model=settings.default_model,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
        )
        # Strip markdown backticks if present
        text = response.text_content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if len(lines) > 2:
                text = "\n".join(lines[1:-1])
        return text
    except Exception as exc:
        logger.warning("Failed to generate hypothetical code", error=str(exc))
        return ""


async def hyde_search(
    query: str,
    query_vector: list[float],
    top_k: int = 5,
    mode: str = "hybrid",
    repo_owner: str | None = None,
    repo_name: str | None = None,
    language: str | None = None,
) -> list[SearchResult]:
    """
    Perform a search augmented with a Hypothetical Document Embedding (HyDE).
    """
    candidates = top_k * settings.retrieval_candidate_multiplier

    hyde_doc = await generate_hypothetical_code(query, language)
    if not hyde_doc:
        logger.info("HyDE document generation failed or empty, falling back to standard search")
        from src.retrieval.searcher import search

        return await search(query, query_vector, top_k, mode, repo_owner, repo_name, language)

    from src.retrieval.searcher import embed_query

    hyde_vector = await embed_query(hyde_doc)

    # We conduct three parallel-ish searches based on mode
    lists_to_merge = []

    if mode in ("semantic", "hybrid"):
        from src.retrieval.searcher import _semantic_search

        semantic_orig = await _semantic_search(
            query_vector, candidates, repo_owner, repo_name, language
        )
        semantic_hyde = await _semantic_search(
            hyde_vector, candidates, repo_owner, repo_name, language
        )
        lists_to_merge.extend([semantic_orig, semantic_hyde])

    if mode in ("keyword", "hybrid"):
        from src.retrieval.searcher import _keyword_search

        keyword = await _keyword_search(query, candidates, repo_owner, repo_name, language)
        lists_to_merge.append(keyword)

    from src.retrieval.searcher import _reciprocal_rank_fusion

    merged = _reciprocal_rank_fusion(*lists_to_merge)
    return merged[:top_k]
