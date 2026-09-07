"""Registration, login, refresh rotation, logout, profile."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update

from app import audit
from app.config import get_settings
from app.deps import CurrentUser, SessionDep, rate_limit
from app.http_status import HTTP_422_UNPROCESSABLE
from app.models import RefreshToken, Role, User, new_id, utcnow
from app.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenPair, UserOut
from app.security.passwords import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_password,
    verify_dummy,
    verify_password,
)
from app.security.tokens import (
    create_access_token,
    digest_token,
    fingerprint,
    generate_refresh_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
# One deliberately vague message for every credential failure mode.
INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
)


def _auth_limit():
    """Stricter bucket for credential endpoints, resolved at request time."""
    return Depends(rate_limit(scope="auth", preset="auth"))


def _client_fingerprint(request: Request) -> str:
    return fingerprint(
        request.client.host if request.client else "",
        request.headers.get("user-agent", "")[:200],
    )


async def _issue_pair(
    session,
    user: User,
    request: Request,
    *,
    family_id: str | None = None,
) -> TokenPair:
    settings = get_settings()
    family = family_id or new_id()
    access, ttl = create_access_token(subject=user.id, role=user.role.value, session=family)
    refresh = generate_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=family,
            token_digest=digest_token(refresh),
            expires_at=utcnow() + timedelta(seconds=settings.refresh_token_ttl_seconds),
            client_fingerprint=_client_fingerprint(request),
        )
    )
    await session.flush()
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=ttl)


async def _revoke_family(session, family_id: str) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit(requests=5, window=3600, scope="register"))],
)
async def register(payload: RegisterRequest, request: Request, session: SessionDep) -> User:
    email = payload.email.lower()
    try:
        validate_password(payload.password, email=email)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=HTTP_422_UNPROCESSABLE, detail=str(exc)) from exc

    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        # Do not confirm registration status of an address to an anonymous caller.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Registration could not be completed"
        )

    # First account bootstraps the admin; everyone after starts as viewer.
    is_first = (await session.scalar(select(User.id).limit(1))) is None
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=Role.admin if is_first else Role.viewer,
    )
    session.add(user)
    await session.flush()
    await audit.record(
        session,
        action="user.register",
        actor_id=user.id,
        entity="user",
        entity_id=user.id,
        request=request,
        meta={"role": user.role.value, "bootstrap_admin": is_first},
    )
    return user


@router.post("/login", response_model=TokenPair)
async def login(
    payload: LoginRequest,
    request: Request,
    session: SessionDep,
    _: None = _auth_limit(),
) -> TokenPair:
    email = payload.email.lower()
    user = await session.scalar(select(User).where(User.email == email))

    if user is None:
        verify_dummy()  # equalise timing against account enumeration
        await audit.record(
            session,
            action="auth.login",
            outcome="failure",
            request=request,
            meta={"reason": "unknown_account"},
        )
        await session.commit()
        raise INVALID_CREDENTIALS

    now = datetime.now(UTC)
    if user.locked_until and user.locked_until > now:
        await audit.record(
            session,
            action="auth.login",
            actor_id=user.id,
            outcome="failure",
            request=request,
            meta={"reason": "locked"},
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Account temporarily locked")

    if not verify_password(payload.password, user.password_hash) or not user.is_active:
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_logins = 0
        await audit.record(
            session,
            action="auth.login",
            actor_id=user.id,
            outcome="failure",
            request=request,
            meta={"reason": "bad_password", "locked": user.locked_until is not None},
        )
        # Commit before raising: the lockout counter and audit row must survive
        # the rollback that the error path would otherwise trigger.
        await session.commit()
        raise INVALID_CREDENTIALS

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    pair = await _issue_pair(session, user, request)
    await audit.record(
        session,
        action="auth.login",
        actor_id=user.id,
        entity="user",
        entity_id=user.id,
        request=request,
    )
    return pair


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    session: SessionDep,
    _: None = _auth_limit(),
) -> TokenPair:
    digest = digest_token(payload.refresh_token)
    stored = await session.scalar(select(RefreshToken).where(RefreshToken.token_digest == digest))
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    now = utcnow()
    if stored.revoked_at is not None:
        # Replay of an already-rotated token: assume theft, kill the whole family.
        await _revoke_family(session, stored.family_id)
        await audit.record(
            session,
            action="auth.refresh_reuse",
            actor_id=stored.user_id,
            outcome="failure",
            entity="refresh_token",
            entity_id=stored.id,
            request=request,
            meta={"family_id": stored.family_id},
        )
        await session.commit()  # the family revocation must outlive this 401
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token reuse detected"
        )
    if stored.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired"
        )

    user = await session.get(User, stored.user_id)
    if user is None or not user.is_active:
        await _revoke_family(session, stored.family_id)
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account unavailable")

    stored.revoked_at = now
    pair = await _issue_pair(session, user, request, family_id=stored.family_id)
    rotated = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_digest == digest_token(pair.refresh_token))
    )
    stored.replaced_by = rotated.id if rotated else None
    await audit.record(
        session,
        action="auth.refresh",
        actor_id=user.id,
        entity="refresh_token",
        entity_id=stored.id,
        request=request,
    )
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, request: Request, session: SessionDep) -> Response:
    digest = digest_token(payload.refresh_token)
    stored = await session.scalar(select(RefreshToken).where(RefreshToken.token_digest == digest))
    if stored is not None:
        await _revoke_family(session, stored.family_id)
        await audit.record(
            session,
            action="auth.logout",
            actor_id=stored.user_id,
            entity="refresh_token",
            entity_id=stored.id,
            request=request,
        )
    # Always 204: logout must not disclose whether the token was valid.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user


@router.post("/sessions/revoke-all", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_all(user: CurrentUser, request: Request, session: SessionDep) -> Response:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await audit.record(
        session,
        action="auth.revoke_all",
        actor_id=user.id,
        entity="user",
        entity_id=user.id,
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
