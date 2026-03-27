from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.storage.db import get_index_stats
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)
router = APIRouter(tags=["stats"])


class StatsResponse(BaseModel):
    """Response model for /stats endpoint."""

    repos: int = Field(..., description="Total number of indexed repositories")
    files: int = Field(..., description="Total number of indexed files")
    chunks: int = Field(..., description="Total number of indexed chunks")
    last_indexed: Optional[str] = Field(
        None, description="ISO 8601 timestamp of last index update, or null"
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    """
    Get indexing statistics.

    Returns total counts of indexed repos, files, and chunks from the database.
    No authentication required.

    **Response:**
    ```json
    {
      "repos": 5,
      "files": 150,
      "chunks": 3000,
      "last_indexed": "2024-01-15T10:30:00Z"
    }
    ```
    """
    stats = await get_index_stats()
    return StatsResponse(
        repos=stats.get("repos", 0),
        files=stats.get("files", 0),
        chunks=stats.get("chunks", 0),
        last_indexed=stats.get("last_indexed"),
    )
