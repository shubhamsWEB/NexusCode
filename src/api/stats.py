from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.storage.db import get_index_stats
from src.utils.logging import get_secure_logger

router = APIRouter()
logger = get_secure_logger(__name__)


@router.get("/stats")
async def get_stats() -> JSONResponse:
    """
    Return aggregated database statistics.

    Returns:
        {
            "repos": int,
            "files": int,
            "chunks": int,
            "last_indexed": str (ISO 8601) or null
        }
    """
    try:
        stats = await get_index_stats()
        # Filter to spec: only repos, files, chunks, last_indexed
        filtered = {
            "repos": stats["repos"],
            "files": stats["files"],
            "chunks": stats["chunks"],
            "last_indexed": stats["last_indexed"],
        }
        logger.debug("GET /stats called successfully")
        return JSONResponse(filtered)
    except Exception as e:
        logger.exception("get_stats failed: %s", e)
        return JSONResponse(
            {"error": str(e)},
            status_code=500,
        )
