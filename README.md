# Executive Decision Intelligence

A decision-support platform for the choices that are too expensive to make on instinct.
Score options against weighted criteria, find out which weight the answer actually hangs
on, and simulate the outcome under uncertainty before committing.

Full-stack: FastAPI + SQLAlchemy 2.0 async backend, a dependency-free dashboard, hardened
auth, Docker, and CI that gates on lint, types, coverage, and three security scanners.

```
make install && make run     # http://localhost:8000
```

---

## What it does

**Ranks options two ways.** Weighted sum (SAW) over min-max normalised criteria, and
TOPSIS over vector-normalised criteria with ideal/anti-ideal distances. Each criterion is
declared a *benefit* (higher is better) or a *cost* (lower is better) and the direction is
applied during normalisation, so raw units never need pre-flipping.

**Tells you how fragile the winner is.** Sensitivity sweeps one weight at a time across
[0, 1] while the remaining weights hold their proportions, and reports the swing in the
winner's score plus the exact weight at which the ranking flips. A decision whose winner
survives every sweep is reported as robust; one that flips at w = 0.34 is not.

**Prices in uncertainty.** Every matrix cell carries an optional spread. Monte Carlo
resamples the matrix from those spreads, optionally perturbs the weight vector with a
Dirichlet draw, and re-ranks each run. The output is a win probability, a rank
distribution, and a p05/p50/p95 band per option — not a single misleadingly precise score.

**Derives weights from pairwise judgements.** AHP with power-iteration eigenvector
extraction and a Saaty consistency ratio. Weights are refused if CR > 0.10, because
inconsistent judgements produce arithmetic that looks authoritative and means nothing.

**Watches the indicators behind the decisions.** KPI series get a Holt linear-trend
forecast with grid-searched smoothing parameters, a widening prediction band, an
out-of-sample backtest, and robust median/MAD anomaly detection that survives a
majority-constant series.

**Rolls it up.** One portfolio view: indicator health against targets, and every decision
whose winner leads by less than 0.05 — the ones a small change in judgement would reverse.

## Screenshot of the flow

```
Portfolio  ->  Decisions  ->  Decision matrix  ->  Ranking  ->  Sensitivity  ->  Simulation
   |                                                                                  |
   +------------------------ fragile winners surface here <--------------------------+
```

## Stack

| Layer      | Choice                                                                 |
|------------|------------------------------------------------------------------------|
| API        | FastAPI, Pydantic v2 (`extra="forbid"` on every inbound model)          |
| Data       | SQLAlchemy 2.0 async, SQLite for local work, PostgreSQL via asyncpg     |
| Analytics  | NumPy — MCDA, AHP, Monte Carlo, Holt smoothing, MAD anomalies           |
| Auth       | Argon2id, JWT access tokens, rotating opaque refresh tokens             |
| Frontend   | Vanilla ES modules and canvas. No build step, no bundler, no CDN        |
| Ops        | Docker multi-stage, compose, GitHub Actions, devcontainer               |

The frontend is deliberately dependency-free: three static files, no npm install, and a
Content-Security-Policy with no `unsafe-inline` because there is no inline script to allow.

## Quick start

```bash
git clone https://github.com/<you>/executive-decision-intelligence.git
cd executive-decision-intelligence
cp .env.example .env          # then: make secret  ->  EDID_SECRET_KEY
make install
make run
```

Open http://localhost:8000 and create an account. **The first account registered becomes
the admin**; every later account starts as `viewer` and must be promoted.

Want data to look at immediately? Set `EDID_SEED_DEMO_DATA=true` before `make run`.

### Docker

```bash
export EDID_SECRET_KEY=$(make -s secret)
export POSTGRES_PASSWORD=$(make -s secret)
docker compose up --build -d
```

The API container runs as UID 10001 with a read-only root filesystem, all capabilities
dropped, and `no-new-privileges`. Postgres is not published to the host.

## Roles

| Role     | Can                                                                |
|----------|--------------------------------------------------------------------|
| viewer   | read decisions they own, read KPIs, run analytics                  |
| analyst  | everything above, plus create and edit decisions, criteria, KPIs   |
| admin    | everything above, plus user roles, deactivation, and the audit log |

Changing a role revokes that user's refresh tokens immediately, and their outstanding
access tokens are rejected on the next request because the role claim no longer matches.

## API

`GET /docs` in development. The surface:

```
POST   /api/v1/auth/register              first account becomes admin
POST   /api/v1/auth/login                 -> access + refresh pair
POST   /api/v1/auth/refresh               rotates; replay revokes the family
POST   /api/v1/auth/logout                always 204
POST   /api/v1/auth/sessions/revoke-all

GET    /api/v1/decisions
POST   /api/v1/decisions
PATCH  /api/v1/decisions/{id}
DELETE /api/v1/decisions/{id}
POST   /api/v1/decisions/{id}/criteria
POST   /api/v1/decisions/{id}/options
PUT    /api/v1/decisions/{id}/scores      bulk idempotent upsert

GET    /api/v1/decisions/{id}/ranking?method=weighted_sum|topsis
GET    /api/v1/decisions/{id}/sensitivity?steps=41
POST   /api/v1/decisions/{id}/simulate    {runs, seed, weight_jitter}
POST   /api/v1/decisions/{id}/weights/ahp {matrix, apply_weights}

POST   /api/v1/kpis
POST   /api/v1/kpis/{id}/points           idempotent by timestamp
GET    /api/v1/kpis/{id}/forecast?horizon=7
GET    /api/v1/kpis/{id}/anomalies?threshold=3.5&window=0

GET    /api/v1/analytics/executive-summary
GET    /api/v1/admin/users | /admin/audit
GET    /api/v1/health
```

Example — is the winner safe?

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$HOST/api/v1/decisions/$ID/simulate" \
  -H 'Content-Type: application/json' \
  -d '{"runs": 20000, "seed": 7, "weight_jitter": 0.2}' | jq '.decision_confidence'
```

`0.51` means the ranked winner wins barely half the time once uncertainty is admitted.
That is the number worth taking into the room.

## Security

Summarised here, detailed in [`SECURITY.md`](SECURITY.md) and
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md).

- **Passwords**: Argon2id (t=3, m=64 MiB, p=2), rehash on login when parameters change,
  a dummy verify on unknown accounts so response time does not leak account existence.
- **Sessions**: 15-minute JWTs with an algorithm allowlist, audience and issuer checks;
  refresh tokens are opaque, stored only as SHA-256 digests, rotated on every use, and a
  replayed token revokes its entire family.
- **Lockout**: five failed attempts, then a 15-minute lock; the counter and its audit row
  are committed before the 401 so the error path cannot roll back the evidence.
- **Authorisation**: role ladder enforced by dependency, and other people's records return
  404 rather than 403 so identifiers cannot be enumerated by status code.
- **Input**: every request model forbids unknown fields; score writes are checked against
  the decision's own options and criteria, which is the IDOR boundary.
- **Transport**: strict CSP, HSTS when HTTPS is enforced, frame denial, no-referrer, a
  CORS allowlist, and a body-size cap applied before parsing.
- **Abuse**: sliding-window rate limits, tighter on credential endpoints, with
  `Retry-After` on refusal.
- **Compute**: option, criterion, simulation-run, and forecast-horizon ceilings; heavy
  NumPy work is dispatched to a worker thread so one request cannot stall the event loop.
- **Audit**: append-only trail with redacted metadata and salted client digests — never
  raw IP addresses.
- **Boot**: production refuses to start with a default secret, a wildcard origin, a
  wildcard host, HTTPS disabled, or SQLite as the database.

`bandit -r app` reports zero findings with zero `# nosec` suppressions.

## Testing

```bash
make test      # 89 tests
make cov       # 91% line coverage
make smoke     # 36 end-to-end checks, no network needed
make lint type security audit
```

The test suite covers the numeric core directly — direction handling, constant columns,
tie ranks, AHP consistency detection, forecast band monotonicity, MAD-zero fallback — and
the HTTP surface for auth, rotation, RBAC, IDOR, and every analytics route.

Coverage runs with `concurrency = ["thread", "greenlet"]`; without it SQLAlchemy's async
greenlet switching loses the tracer and reports live request handlers as dead code.

## Layout

```
backend/app/
  analytics/    mcda.py  risk.py  forecast.py  anomaly.py     pure NumPy, no I/O
  routers/      auth  decisions  analytics  kpis  admin
  security/     passwords  tokens  rate_limit  headers
  matrix.py     ORM rows -> NumPy matrices
  models.py     schemas.py  deps.py  audit.py  config.py  main.py
backend/tests/  unit + API + security
frontend/       index.html  assets/app.js  assets/styles.css
scripts/        smoke.py
docs/           ARCHITECTURE.md  THREAT_MODEL.md  METHODS.md
```

The analytics package imports nothing from the web or database layers, so the maths is
testable, portable, and reviewable on its own.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — request path, layering, data model
- [`docs/METHODS.md`](docs/METHODS.md) — the maths, its assumptions, and its limits
- [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) — assets, adversaries, mitigations
- [`SECURITY.md`](SECURITY.md) — controls and disclosure policy
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to work on it

## License

MIT. See [`LICENSE`](LICENSE).
