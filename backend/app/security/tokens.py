"""JWT access tokens and opaque, digest-stored refresh tokens.

Access tokens are stateless and short-lived. Refresh tokens are opaque random
strings; only a SHA-256 digest reaches the database, so a database leak cannot
be replayed against the API.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.config import get_settings

ACCESS_KIND = "access"
REFRESH_KIND = "refresh"
_ISSUER = "edid"
_AUDIENCE = "edid-api"


class TokenError(Exception):
    """Token missing, malformed, expired, or of the wrong kind."""


@dataclass(frozen=True, slots=True)
class AccessClaims:
    subject: str
    role: str
    session: str
    jti: str
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(*, subject: str, role: str, session: str) -> tuple[str, int]:
    settings = get_settings()
    issued = _now()
    ttl = settings.access_token_ttl_seconds
    payload = {
        "sub": subject,
        "role": role,
        "sid": session,
        # Claim name is "kind"; "type" is reserved-ish and confuses linters/scanners.
        "kind": ACCESS_KIND,
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "iat": int(issued.timestamp()),
        "nbf": int(issued.timestamp()),
        "exp": int((issued + timedelta(seconds=ttl)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return token, ttl


def decode_access_token(token: str) -> AccessClaims:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],  # allowlist: never trust header alg
            audience=_AUDIENCE,
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.PyJWTError as exc:  # expired, bad signature, wrong aud/iss
        raise TokenError(str(exc)) from exc
    if payload.get("kind") != ACCESS_KIND:
        raise TokenError("Wrong token kind")
    role = payload.get("role")
    session = payload.get("sid")
    if not isinstance(role, str) or not isinstance(session, str):
        raise TokenError("Malformed claims")
    return AccessClaims(
        subject=str(payload["sub"]),
        role=role,
        session=session,
        jti=str(payload["jti"]),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
    )


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def digests_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def fingerprint(*parts: str | None) -> str:
    """Salted digest of client attributes; used for binding, never for identity."""
    secret = get_settings().secret_key.encode("utf-8")
    material = "|".join(part or "" for part in parts).encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()
