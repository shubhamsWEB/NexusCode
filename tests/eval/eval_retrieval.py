"""
Retrieval Quality Evaluation Suite.
Run with: pytest tests/eval/ --run-eval
Requires a populated local index.
"""

import math
from typing import TypedDict

import pytest

from src.retrieval.searcher import embed_query, search


class EvalTestCase(TypedDict):
    query: str
    target_file: str


EVAL_CASES: list[EvalTestCase] = [
    {
        "query": "how is the webhook signature verified",
        "target_file": "src/github/webhook.py",
    },
    {
        "query": "database connection setup with sqlalchemy",
        "target_file": "src/storage/db.py",
    },
    {
        "query": "voyage ai embedding client configuration",
        "target_file": "src/pipeline/embedder.py",
    },
    {
        "query": "how are files parsed for symbols using tree-sitter",
        "target_file": "src/pipeline/parser.py",
    },
    {
        "query": "sliding window token chunking logic",
        "target_file": "src/pipeline/chunker.py",
    },
]


def calculate_mrr(rank: int) -> float:
    return 1.0 / rank if rank > 0 else 0.0


def calculate_ndcg(rank: int, max_rank: int = 10) -> float:
    if rank == 0 or rank > max_rank:
        return 0.0
    # ideal DCG for single relevant item is 1.0 (log2(2) = 1)
    # DCG is 1 / log2(rank + 1)
    return 1.0 / math.log2(rank + 1)


@pytest.mark.eval
@pytest.mark.asyncio
async def test_retrieval_quality() -> None:
    """
    Evaluates MRR@5 and NDCG@10 across a set of golden queries.
    Expects MRR@5 >= 0.6 and NDCG@10 >= 0.5.
    """
    # Simple check if there are chunks in the DB, if not, skip test
    from src.storage.db import get_index_stats

    stats = await get_index_stats()
    if stats.get("chunks", 0) == 0:
        pytest.skip("Index is empty. Populate it using scripts/full_index.py before evaluating.")

    mrr_sum = 0.0
    ndcg_sum = 0.0

    for case in EVAL_CASES:
        query = case["query"]
        target = case["target_file"]

        vec = await embed_query(query)
        results = await search(query, vec, top_k=10, mode="hybrid", hyde=True)

        rank = 0
        for i, r in enumerate(results, 1):
            if target in r.file_path:
                rank = i
                break

        mrr_sum += calculate_mrr(rank if rank <= 5 else 0)
        ndcg_sum += calculate_ndcg(rank, 10)

    avg_mrr = mrr_sum / len(EVAL_CASES)
    avg_ndcg = ndcg_sum / len(EVAL_CASES)

    print(f"\\nRetrieval Eval Results: MRR@5={avg_mrr:.3f}, NDCG@10={avg_ndcg:.3f}")
    assert avg_mrr >= 0.6, f"MRR@5 too low: {avg_mrr:.3f} < 0.6"
    assert avg_ndcg >= 0.5, f"NDCG@10 too low: {avg_ndcg:.3f} < 0.5"
