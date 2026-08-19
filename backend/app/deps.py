"""FastAPI auth dependencies.

Tiers (docs/04-api-contract.md):
  * none              -> caller session creation
  * caller session    -> Authorization: Bearer <caller_session_jwt>
  * support user      -> Authorization: Bearer <idp_jwt> (Firebase/Google)
  * service           -> X-Service-Key: <SERVICE_API_KEY>
  * webhook           -> LiveKit signature
"""

from __future__ import annotations

import hmac
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings
from .core.firebase_auth import AuthError, FirebaseTokenVerifier, SupportPrincipal

settings = get_settings()

security = HTTPBearer(auto_error=False)


class CallerTokenPayload:
    def __init__(self, session_id: uuid.UUID, identity: str) -> None:
        self.session_id = session_id
        self.identity = identity


def _bearer_token(credentials: HTTPAuthorizationCredentials | None) -> str | None:
    if credentials is None:
        return None
    return credentials.credentials


def bearer_from_request(request: Request) -> str | None:
    """Extract a bearer token straight from the request headers.

    Use this in plain (non-Depends) call paths. ``security(request)`` is a
    coroutine -- calling it synchronously returns an un-awaited coroutine
    object, and the resulting attribute access fails silently in a try/except,
    so the auth path never matches. Reading the header avoids that trap.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    return token or None


def issue_caller_token(session_id: uuid.UUID, identity: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": identity,
        "session_id": str(session_id),
        "iat": now,
        "exp": now + timedelta(seconds=settings.caller_jwt_ttl_seconds),
        "type": "caller_session",
    }
    return jwt.encode(payload, settings.caller_jwt_secret, algorithm="HS256")


def require_caller_session(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> CallerTokenPayload:
    token = _bearer_token(credentials)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_token", "message": "Authorization header required"},
        )
    try:
        payload = jwt.decode(
            token, settings.caller_jwt_secret, algorithms=["HS256"], options={"require": ["exp"]}
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "token_expired", "message": "Caller session token expired"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Invalid caller session token"},
        ) from exc

    if payload.get("type") != "caller_session":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Wrong token type"},
        )

    try:
        session_id = uuid.UUID(payload["session_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Malformed session id in token"},
        ) from exc

    return CallerTokenPayload(session_id=session_id, identity=payload.get("sub", ""))


# Support-token verification lives in core/firebase_auth.py, which is covered by
# tests/test_support_auth.py. Do not re-inline it here -- a second copy will
# drift, and this is the boundary between "any Google account" and the trusted
# support role.
SupportTokenPayload = SupportPrincipal


@lru_cache
def get_support_verifier() -> FirebaseTokenVerifier:
    """Built lazily so tests can patch settings before construction."""
    return FirebaseTokenVerifier(
        project_id=settings.firebase_project_id,
        allowed_domains=settings.support_allowed_domain_set,
        allowed_emails=settings.support_allowed_email_set,
        admin_emails=settings.support_admin_email_set,
    )


# Back-compat alias for existing call sites.
_get_verifier = get_support_verifier


# AuthError codes that mean "not this account" rather than "bad token".
_FORBIDDEN_CODES = frozenset({"domain_not_allowed", "no_allowlist_configured"})


def require_support_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> SupportPrincipal:
    token = _bearer_token(credentials)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_token", "message": "Authorization header required"},
        )
    try:
        return _get_verifier().verify(token)
    except AuthError as exc:
        code = str(exc).split(":", 1)[0]
        forbidden = code in _FORBIDDEN_CODES
        raise HTTPException(
            status_code=(
                status.HTTP_403_FORBIDDEN if forbidden else status.HTTP_401_UNAUTHORIZED
            ),
            detail={
                "code": code,
                "message": (
                    "Support account not authorized"
                    if forbidden
                    else "Support authentication failed"
                ),
            },
        ) from exc


def require_service_key(x_service_key: Annotated[str | None, Header()] = None) -> None:
    # Constant-time compare: a plain `!=` leaks the key prefix via timing.
    if x_service_key is None or not hmac.compare_digest(
        x_service_key, settings.service_api_key
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_service_key", "message": "X-Service-Key header required"},
        )


def require_admin(
    support: Annotated[SupportPrincipal, Depends(require_support_user)],
) -> SupportPrincipal:
    if not support.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Admin claim required"},
        )
    return support


async def require_session_access(request: Request, session_id: uuid.UUID) -> None:
    """Accept caller session token (scoped to the session), support token, or service key."""
    auth_header = request.headers.get("Authorization", "")
    token: str | None = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    if token:
        # Caller session token.
        try:
            payload = jwt.decode(
                token,
                settings.caller_jwt_secret,
                algorithms=["HS256"],
                options={"require": ["exp"]},
            )
            if (
                payload.get("type") == "caller_session"
                and uuid.UUID(payload["session_id"]) == session_id
            ):
                return
        except jwt.InvalidTokenError:
            pass

        # Support user token.
        try:
            _get_verifier().verify(token)
            return
        except AuthError:
            pass

    # Service key.
    if hmac.compare_digest(
        request.headers.get("X-Service-Key", ""), settings.service_api_key
    ):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "Valid authorization required"},
    )


# A small helper for rate-limit telemetry (IP + device id) without Redis.
def caller_rate_limit_context(request: Request, device_id: str | None = None) -> dict:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    elif request.client:
        ip = request.client.host
    else:
        ip = "unknown"
    return {"ip": ip, "device_id": device_id}
