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

    Parallelized (v2): the original-vector semantic search and keyword search
    fire immediately while the LLM generates the hypothetical document.
    The HyDE-vector semantic search runs after embedding the generated doc.
    This reduces total latency by ~(LLM generation time) on cache misses.
    """
    import asyncio

    candidates = top_k * settings.retrieval_candidate_multiplier

    from src.retrieval.searcher import (
        _keyword_search,
        _reciprocal_rank_fusion,
        _semantic_search,
        embed_query,
        search,
    )

    # Fire non-HyDE searches concurrently with HyDE doc generation
    parallel_tasks = [generate_hypothetical_code(query, language)]
    if mode in ("semantic", "hybrid"):
        parallel_tasks.append(
            _semantic_search(query_vector, candidates, repo_owner, repo_name, language)
        )
    if mode in ("keyword", "hybrid"):
        parallel_tasks.append(
            _keyword_search(query, candidates, repo_owner, repo_name, language)
        )

    results = await asyncio.gather(*parallel_tasks, return_exceptions=True)
    hyde_doc = results[0] if not isinstance(results[0], BaseException) else ""

    if not hyde_doc:
        logger.info("HyDE document generation failed or empty, falling back to standard search")
        return await search(query, query_vector, top_k, mode, repo_owner, repo_name, language)

    # Collect parallel search results
    lists_to_merge: list = []
    for r in results[1:]:
        if not isinstance(r, BaseException):
            lists_to_merge.append(r)

    # Embed the hypothetical doc and run the HyDE semantic search
    hyde_vector = await embed_query(hyde_doc)
    if mode in ("semantic", "hybrid"):
        semantic_hyde = await _semantic_search(
            hyde_vector, candidates, repo_owner, repo_name, language
        )
        lists_to_merge.append(semantic_hyde)

    if not lists_to_merge:
        return await search(query, query_vector, top_k, mode, repo_owner, repo_name, language)

    merged = _reciprocal_rank_fusion(*lists_to_merge)
    return merged[:top_k]
