"""
HMAC-SHA256 webhook signature verification (FR-1 in BRD).

GitHub signs every webhook payload with:
    X-Hub-Signature-256: sha256=<hex_digest>

We verify using a constant-time comparison to prevent timing attacks.
"""

from __future__ import annotations

import hashlib
import hmac

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings


async def verify_github_signature(request: Request, secret: str) -> bytes:
    """
    Read the raw request body and verify the GitHub HMAC-SHA256 signature.

    Returns the raw body bytes so the caller doesn't need to re-read the stream.

    Raises:
        HTTPException(401) if the signature header is missing or invalid.
    """
    body = await request.body()

    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Hub-Signature-256 header.",
        )

    if not signature_header.startswith("sha256="):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed X-Hub-Signature-256 header (expected 'sha256=' prefix).",
        )

    received_sig = signature_header.removeprefix("sha256=")

    expected_sig = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(received_sig, expected_sig):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook signature verification failed.",
        )

    return body


security = HTTPBearer(auto_error=False)
settings = get_settings()


async def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    """
    Verify the JWT token from the Authorization header.
    In development, accepts "dev-token" as a valid bearer token bypass.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Missing Authorization header.",
        )

    if settings.app_env == "development" and credentials.credentials == "dev-token":
        return {"sub": "dev-user", "role": "admin"}

    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key,
            algorithms=["HS256"],
        )
        return payload
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
        )
