"""Request/response contracts. Every inbound model forbids unknown fields."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import DecisionStatus, Direction, Role

Name = Annotated[str, Field(min_length=1, max_length=120, strip_whitespace=True)]
Title = Annotated[str, Field(min_length=3, max_length=200, strip_whitespace=True)]
Body = Annotated[str, Field(default="", max_length=2000)]
Score01 = Annotated[float, Field(ge=-1e9, le=1e9)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth ------------------------------------------------------------------
class RegisterRequest(StrictModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=12, max_length=128)]
    full_name: Annotated[str, Field(default="", max_length=120)]


class LoginRequest(StrictModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RefreshRequest(StrictModel):
    refresh_token: Annotated[str, Field(min_length=16, max_length=512)]


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    scheme: Literal["bearer"] = "bearer"
    expires_in: int


class UserOut(ORMModel):
    id: str
    email: EmailStr
    full_name: str
    role: Role
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class RoleUpdate(StrictModel):
    role: Role


# --- decisions -------------------------------------------------------------
class CriterionIn(StrictModel):
    name: Name
    weight: Annotated[float, Field(ge=0.0, le=1000.0)] = 1.0
    direction: Direction = Direction.benefit
    description: Annotated[str, Field(default="", max_length=500)]


class CriterionOut(ORMModel):
    id: str
    name: str
    weight: float
    direction: Direction
    description: str


class OptionIn(StrictModel):
    name: Name
    description: Annotated[str, Field(default="", max_length=500)]


class OptionOut(ORMModel):
    id: str
    name: str
    description: str


class DecisionIn(StrictModel):
    title: Title
    description: Body = ""
    status: DecisionStatus = DecisionStatus.draft


class DecisionPatch(StrictModel):
    title: Title | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    status: DecisionStatus | None = None


class DecisionOut(ORMModel):
    id: str
    title: str
    description: str
    status: DecisionStatus
    owner_id: str
    created_at: datetime
    updated_at: datetime
    criteria: list[CriterionOut] = []
    options: list[OptionOut] = []


class ScoreIn(StrictModel):
    option_id: Annotated[str, Field(min_length=1, max_length=32)]
    criterion_id: Annotated[str, Field(min_length=1, max_length=32)]
    value: Score01
    uncertainty: Annotated[float, Field(ge=0.0, le=1e9)] = 0.0


class ScoreBulkIn(StrictModel):
    scores: Annotated[list[ScoreIn], Field(min_length=1, max_length=1500)]


class ScoreOut(ORMModel):
    id: str
    option_id: str
    criterion_id: str
    value: float
    uncertainty: float


# --- analytics -------------------------------------------------------------
class RankedOptionOut(BaseModel):
    option_id: str
    name: str
    score: float
    rank: int
    contributions: dict[str, float]


class RankingOut(BaseModel):
    decision_id: str
    method: Literal["weighted_sum", "topsis"]
    ranked: list[RankedOptionOut]
    weights: dict[str, float]
    winner_margin: float
    generated_at: datetime


class SensitivityRowOut(BaseModel):
    criterion_id: str
    criterion: str
    base_weight: float
    swing: float
    flip_weight: float | None
    flip_to_option: str | None
    robust: bool


class SensitivityOut(BaseModel):
    decision_id: str
    method: str
    incumbent_option: str
    rows: list[SensitivityRowOut]


class SimulationRequest(StrictModel):
    runs: Annotated[int, Field(ge=100, le=50_000)] = 5_000
    seed: Annotated[int, Field(ge=0, le=2**31 - 1)] | None = None
    weight_jitter: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    default_uncertainty: Annotated[float, Field(ge=0.0, le=1e6)] = 0.0


class SimulatedOptionOut(BaseModel):
    option_id: str
    name: str
    win_probability: float
    mean_score: float
    p05: float
    p50: float
    p95: float
    rank_distribution: dict[int, float]


class SimulationOut(BaseModel):
    decision_id: str
    runs: int
    seed: int
    decision_confidence: float
    options: list[SimulatedOptionOut]


class AhpRequest(StrictModel):
    matrix: Annotated[list[list[float]], Field(min_length=2, max_length=30)]
    apply_weights: bool = False

    @field_validator("matrix")
    @classmethod
    def _square_and_positive(cls, matrix: list[list[float]]) -> list[list[float]]:
        size = len(matrix)
        for row in matrix:
            if len(row) != size:
                raise ValueError("Pairwise matrix must be square")
            if any(value <= 0 or value > 1e6 for value in row):
                raise ValueError("Pairwise entries must be positive and bounded")
        return matrix


class AhpOut(BaseModel):
    weights: dict[str, float]
    lambda_max: float
    consistency_ratio: float
    consistent: bool
    applied: bool


# --- KPIs ------------------------------------------------------------------
class KpiIn(StrictModel):
    name: Name
    unit: Annotated[str, Field(default="", max_length=32)]
    target: float | None = None
    direction: Direction = Direction.benefit


class KpiOut(ORMModel):
    id: str
    name: str
    unit: str
    target: float | None
    direction: Direction


class KpiPointIn(StrictModel):
    ts: datetime
    value: Annotated[float, Field(ge=-1e12, le=1e12)]


class KpiPointsIn(StrictModel):
    points: Annotated[list[KpiPointIn], Field(min_length=1, max_length=5000)]


class KpiPointOut(ORMModel):
    ts: datetime
    value: float


class ForecastOut(BaseModel):
    kpi_id: str
    horizon: int
    alpha: float
    beta: float
    forecast: list[float]
    lower: list[float]
    upper: list[float]
    trend_per_period: float
    mape: float | None
    backtest: dict[str, float | None] | None


class AnomalyOut(BaseModel):
    ts: datetime
    value: float
    score: float
    kind: str


class KpiHealthOut(BaseModel):
    kpi_id: str
    name: str
    latest: float | None
    change_pct: float | None
    to_target_pct: float | None
    volatility: float
    anomalies: int
    status: Literal["on_track", "watch", "off_track", "insufficient_data"]


class ExecutiveSummaryOut(BaseModel):
    generated_at: datetime
    kpi_health: list[KpiHealthOut]
    portfolio_score: float
    open_decisions: int
    fragile_decisions: list[dict[str, object]]


class AuditOut(ORMModel):
    id: str
    ts: datetime
    actor_id: str | None
    action: str
    entity: str
    entity_id: str | None
    outcome: str
    request_id: str
    meta: dict


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: str
