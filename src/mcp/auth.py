"""
JWT bearer token auth for the MCP server.

In dev mode: POST /auth/token with {"sub": "dev", "repos": ["owner/name"]}
             returns a signed JWT good for 8 hours.

In production: integrate with GitHub OAuth 2.1 + PKCE (future work).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_bearer = HTTPBearer(auto_error=False)

_ALGORITHM = "HS256"
_TOKEN_TTL_HOURS = 8


# ── Token issuance ────────────────────────────────────────────────────────────


def issue_token(sub: str, repos: list[str] | None = None) -> str:
    """Sign and return a JWT for the given subject."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": sub,
        "repos": repos or [],  # empty list = access all repos
        "iat": now,
        "exp": now + timedelta(hours=_TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def verify_token(token: str) -> dict[str, Any]:
    """
    Decode and validate a JWT.
    Raises HTTPException(401) on any failure.
    """
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        ) from None
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token."
        ) from None


# ── FastAPI dependency ────────────────────────────────────────────────────────


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any]:
    """
    FastAPI dependency — validates Bearer JWT and returns the decoded claims.
    Mount on protected routes: `Depends(require_auth)`.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return verify_token(credentials.credentials)


# ── Dev token endpoint ────────────────────────────────────────────────────────


class TokenRequest(BaseModel):
    sub: str = "dev-agent"
    repos: list[str] = []  # empty = access all repos


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = _TOKEN_TTL_HOURS * 3600


@router.post("/token", response_model=TokenResponse, summary="Issue a dev JWT for MCP access")
async def issue_dev_token(req: TokenRequest) -> TokenResponse:
    """
    Development-only endpoint to issue a signed JWT.
    In production, replace with GitHub OAuth 2.1 + PKCE flow.
    """
    token = issue_token(sub=req.sub, repos=req.repos)
    return TokenResponse(access_token=token)
