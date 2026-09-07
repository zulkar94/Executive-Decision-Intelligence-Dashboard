"""ORM models. All timestamps are timezone-aware UTC."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetimes that survive backends without tz support.

    SQLite discards the offset, so values would come back naive and blow up on
    the first comparison against ``utcnow()``. Everything is normalised to UTC
    on the way in and re-tagged as UTC on the way out.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Role(enum.StrEnum):
    """Least privilege ladder: viewer < analyst < admin."""

    viewer = "viewer"
    analyst = "analyst"
    admin = "admin"


ROLE_RANK: dict[Role, int] = {Role.viewer: 0, Role.analyst: 1, Role.admin: 2}


class DecisionStatus(enum.StrEnum):
    draft = "draft"
    review = "review"
    decided = "decided"
    archived = "archived"


class Direction(enum.StrEnum):
    benefit = "benefit"  # higher is better
    cost = "cost"  # lower is better


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False), default=Role.viewer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)

    decisions: Mapped[list[Decision]] = relationship(back_populates="owner")


class RefreshToken(Base):
    """Opaque refresh token; only its digest is persisted.

    ``family_id`` groups every token descended from one login, enabling
    whole-family revocation the moment a rotated token is replayed.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_family_user", "family_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    family_id: Mapped[str] = mapped_column(String(32), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    replaced_by: Mapped[str | None] = mapped_column(String(32), default=None)
    client_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)


class Decision(Base, TimestampMixin):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[DecisionStatus] = mapped_column(
        Enum(DecisionStatus, native_enum=False), default=DecisionStatus.draft, nullable=False
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    owner: Mapped[User] = relationship(back_populates="decisions")
    criteria: Mapped[list[Criterion]] = relationship(
        back_populates="decision", cascade="all, delete-orphan", lazy="selectin"
    )
    options: Mapped[list[Option]] = relationship(
        back_populates="decision", cascade="all, delete-orphan", lazy="selectin"
    )


class Criterion(Base):
    __tablename__ = "criteria"
    __table_args__ = (
        UniqueConstraint("decision_id", "name", name="uq_criterion_name"),
        CheckConstraint("weight >= 0", name="ck_criterion_weight_nonneg"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    decision_id: Mapped[str] = mapped_column(
        ForeignKey("decisions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction, native_enum=False), default=Direction.benefit, nullable=False
    )
    description: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    decision: Mapped[Decision] = relationship(back_populates="criteria")


class Option(Base):
    __tablename__ = "options"
    __table_args__ = (UniqueConstraint("decision_id", "name", name="uq_option_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    decision_id: Mapped[str] = mapped_column(
        ForeignKey("decisions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    decision: Mapped[Decision] = relationship(back_populates="options")
    scores: Mapped[list[Score]] = relationship(
        back_populates="option", cascade="all, delete-orphan", lazy="selectin"
    )


class Score(Base):
    """One cell of the decision matrix, with optional uncertainty (std dev)."""

    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint("option_id", "criterion_id", name="uq_score_cell"),
        CheckConstraint("uncertainty >= 0", name="ck_score_uncertainty_nonneg"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    option_id: Mapped[str] = mapped_column(
        ForeignKey("options.id", ondelete="CASCADE"), index=True, nullable=False
    )
    criterion_id: Mapped[str] = mapped_column(
        ForeignKey("criteria.id", ondelete="CASCADE"), index=True, nullable=False
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)
    uncertainty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    option: Mapped[Option] = relationship(back_populates="scores")


class Kpi(Base, TimestampMixin):
    __tablename__ = "kpis"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    target: Mapped[float | None] = mapped_column(Float, default=None)
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction, native_enum=False), default=Direction.benefit, nullable=False
    )

    points: Mapped[list[KpiPoint]] = relationship(
        back_populates="kpi", cascade="all, delete-orphan"
    )


class KpiPoint(Base):
    __tablename__ = "kpi_points"
    __table_args__ = (UniqueConstraint("kpi_id", "ts", name="uq_kpi_ts"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kpi_id: Mapped[str] = mapped_column(
        ForeignKey("kpis.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)

    kpi: Mapped[Kpi] = relationship(back_populates="points")


class AuditLog(Base):
    """Append-only trail. Never expose raw client IPs; store salted digests."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts_action", "ts", "action"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(32), default=None)
    outcome: Mapped[str] = mapped_column(String(16), default="success", nullable=False)
    client_digest: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    request_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
