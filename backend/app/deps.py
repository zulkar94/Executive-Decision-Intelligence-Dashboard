"""Shared dependencies: authentication, RBAC, rate limiting, ownership checks."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import ROLE_RANK, Decision, Role, User
from app.security.rate_limit import limiter
from app.security.tokens import TokenError, decode_access_token

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def client_key(request: Request) -> str:
    """Rate-limit key. Trusts X-Forwarded-For only when a proxy is configured."""
    settings = get_settings()
    if settings.force_https:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "anonymous"


def rate_limit(
    *,
    requests: int | None = None,
    window: int | None = None,
    scope: str = "global",
    preset: str | None = None,
) -> Callable[[Request], Coroutine[Any, Any, None]]:
    """Build a rate-limit dependency.

    Limits are resolved per request, not at import time, so configuration (and
    tests that patch it) take effect without rebuilding the router.
    """

    async def dependency(request: Request) -> None:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return
        if preset == "auth":
            limit = settings.auth_rate_limit_requests
            seconds = settings.auth_rate_limit_window_seconds
        else:
            limit = requests or settings.rate_limit_requests
            seconds = window or settings.rate_limit_window_seconds
        decision = limiter.check(f"{scope}:{client_key(request)}", limit=limit, window=seconds)
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={
                    "Retry-After": str(decision.retry_after),
                    "X-RateLimit-Limit": str(decision.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

    return dependency


async def current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UNAUTHENTICATED
    try:
        claims = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = await session.get(User, claims.subject)
    if user is None or not user.is_active:
        raise UNAUTHENTICATED
    if user.locked_until and user.locked_until > datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Account locked")
    # Role changes take effect immediately, even for tokens minted beforehand.
    if user.role.value != claims.role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Stale token, re-authenticate"
        )
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_role(minimum: Role) -> Callable[[User], User]:
    def dependency(user: CurrentUser) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {minimum.value} role or higher",
            )
        return user

    return dependency


RequireAnalyst = Annotated[User, Depends(require_role(Role.analyst))]
RequireAdmin = Annotated[User, Depends(require_role(Role.admin))]


async def owned_decision(decision_id: str, session: AsyncSession, user: User) -> Decision:
    """Fetch a decision the user may read.

    Returns 404 rather than 403 for other people's records so that identifiers
    cannot be enumerated by comparing status codes.
    """
    result = await session.execute(select(Decision).where(Decision.id == decision_id))
    decision = result.scalar_one_or_none()
    if decision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Decision not found")
    if decision.owner_id != user.id and user.role is not Role.admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Decision not found")
    return decision


async def writable_decision(decision_id: str, session: AsyncSession, user: User) -> Decision:
    decision = await owned_decision(decision_id, session, user)
    if ROLE_RANK[user.role] < ROLE_RANK[Role.analyst]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requires analyst role")
    return decision
