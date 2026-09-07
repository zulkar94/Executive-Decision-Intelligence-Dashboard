"""Append-only audit trail helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.models import AuditLog
from app.security.tokens import fingerprint

# Values that must never reach the audit trail verbatim.
_REDACT_KEYS = {"password", "token", "refresh_token", "secret", "authorization"}


def client_digest(request: Request | None) -> str:
    if request is None:
        return ""
    host = request.client.host if request.client else ""
    return fingerprint(host)


def redact(meta: dict[str, Any] | None) -> dict[str, Any]:
    if not meta:
        return {}
    return {
        key: ("[redacted]" if key.lower() in _REDACT_KEYS else value) for key, value in meta.items()
    }


async def record(
    session: AsyncSession,
    *,
    action: str,
    actor_id: str | None = None,
    entity: str = "",
    entity_id: str | None = None,
    outcome: str = "success",
    request: Request | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Write one audit row. Never raises into the request path."""
    entry = AuditLog(
        actor_id=actor_id,
        action=action[:64],
        entity=entity[:64],
        entity_id=entity_id,
        outcome=outcome[:16],
        client_digest=client_digest(request),
        request_id=getattr(getattr(request, "state", None), "request_id", "") or "",
        meta=redact(meta),
    )
    session.add(entry)
    await session.flush()
