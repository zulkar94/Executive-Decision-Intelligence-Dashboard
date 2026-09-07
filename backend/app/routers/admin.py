"""Administrative surface: user roles, sessions, audit trail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select, update

from app import audit
from app.deps import RequireAdmin, SessionDep, rate_limit
from app.models import AuditLog, RefreshToken, Role, User, utcnow
from app.schemas import AuditOut, RoleUpdate, UserOut

router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(rate_limit(scope="admin"))]
)


@router.get("/users", response_model=list[UserOut])
async def list_users(
    admin: RequireAdmin,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[User]:
    return list(
        (
            await session.scalars(
                select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
            )
        ).all()
    )


@router.patch("/users/{user_id}/role", response_model=UserOut)
async def set_role(
    user_id: str,
    payload: RoleUpdate,
    admin: RequireAdmin,
    request: Request,
    session: SessionDep,
) -> User:
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == admin.id and payload.role is not Role.admin:
        # Prevents an org locking itself out of its own admin surface.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Admins cannot demote themselves"
        )
    previous = target.role
    target.role = payload.role
    # Privilege change invalidates every outstanding session for that user.
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == target.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await audit.record(
        session,
        action="user.role_change",
        actor_id=admin.id,
        entity="user",
        entity_id=target.id,
        request=request,
        meta={"from": previous.value, "to": payload.role.value},
    )
    return target


@router.post("/users/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate(
    user_id: str, admin: RequireAdmin, request: Request, session: SessionDep
) -> Response:
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Admins cannot deactivate themselves"
        )
    target.is_active = False
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == target.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await audit.record(
        session,
        action="user.deactivate",
        actor_id=admin.id,
        entity="user",
        entity_id=target.id,
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/audit", response_model=list[AuditOut])
async def audit_trail(
    admin: RequireAdmin,
    session: SessionDep,
    action: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[AuditLog]:
    query = select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit).offset(offset)
    if action:
        query = query.where(AuditLog.action == action)
    return list((await session.scalars(query)).all())
