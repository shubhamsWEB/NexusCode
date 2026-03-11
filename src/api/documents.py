"""
Documents API — download generated PDF documents.

GET /documents/{doc_id}/download  → streams the PDF bytes to the browser.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("/{doc_id}/download")
async def download_document(doc_id: str) -> Response:
    """Stream a generated PDF document by its ID."""
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT filename, pdf_bytes
                    FROM generated_documents
                    WHERE id = :id
                """),
                {"id": doc_id},
            )
        ).mappings().first()

    if not row:
        return Response(
            content=b'{"detail":"Document not found"}',
            status_code=404,
            media_type="application/json",
        )

    filename = row["filename"]
    if not filename.endswith(".pdf"):
        filename = filename + ".pdf"

    logger.info("documents: serving doc_id=%s filename=%s", doc_id, filename)

    return Response(
        content=bytes(row["pdf_bytes"]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
