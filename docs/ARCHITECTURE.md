# Architecture

## Layering

```
        HTTP
          |
   middleware      security headers -> HTTPS redirect -> trusted host ->
          |        CORS -> body-size cap -> gzip
      routers      auth  decisions  analytics  kpis  admin
          |
        deps       authentication, RBAC, rate limits, ownership
          |
       matrix      ORM rows -> NumPy arrays (the only bridge between the two worlds)
          |
    analytics      mcda  risk  forecast  anomaly     pure functions, no I/O
          |
      models       SQLAlchemy 2.0 async ORM
          |
    PostgreSQL / SQLite
```

The rule that keeps this maintainable: **`app/analytics/` imports nothing from FastAPI,
SQLAlchemy, or `app.config`.** It takes NumPy arrays and returns dataclasses. That makes
the numeric core unit-testable without a database, reusable from a notebook or a batch
job, and reviewable by someone who does not know the web framework.

`app/matrix.py` is the single translation point. It fetches criteria, options, and scores,
fills a dense `(n_options, n_criteria)` array, defaults missing cells to zero, reports how
many were missing, and raises the HTTP-level errors (409 for an unanalysable decision, 413
for one over the size ceiling) so routers stay thin.

## Request path

1. `SecurityHeadersMiddleware` assigns a request id (echoing an inbound `X-Request-ID`)
   and stamps the response with CSP, HSTS, frame denial, and referrer policy.
2. `HTTPSRedirectIfConfigured` and `TrustedHostMiddleware` reject wrong-scheme and
   wrong-host traffic before routing.
3. `BodySizeLimitMiddleware` rejects oversized payloads on `Content-Length`, before any
   parsing happens.
4. The route's `rate_limit` dependency checks a sliding window, resolved from settings at
   request time so configuration changes do not require a restart in tests.
5. `current_user` decodes the JWT with an algorithm allowlist, loads the user, checks
   active and unlocked status, and compares the token's role claim against the stored role
   so a demotion takes effect on the next request.
6. The handler does its work, `audit.record` appends a trail row, and the session
   dependency commits — or rolls back if the handler raised.

## Sessions

Access tokens are stateless 15-minute JWTs carrying `sub`, `role`, `sid` (the session
family), `jti`, `iss`, `aud`, and `kind`. Nothing sensitive rides in them.

Refresh tokens are opaque 48-byte random strings. Only `sha256(token)` is stored, so a
database dump cannot be replayed against the API. Every refresh rotates: the presented
token is marked revoked, a successor is issued into the same `family_id`, and the
predecessor records `replaced_by`.

If a revoked token is presented again, that is either theft or a client bug, and the
system cannot tell which — so it treats it as theft and revokes the whole family. The
legitimate holder is logged out and must re-authenticate, which is the correct trade.

## Data model

```
User 1---* Decision 1---* Criterion
                   1---* Option 1---* Score *---1 Criterion
User 1---* RefreshToken   (family_id groups one login's lineage)
Kpi  1---* KpiPoint       (unique on kpi_id + ts, so ingestion is idempotent)
AuditLog                  (append-only; salted client digests, redacted metadata)
```

`Score` is unique on `(option_id, criterion_id)`: one cell, one row. Bulk upsert reads the
existing cells once, then updates or inserts in memory, so re-sending a batch is safe.

All timestamps use a `UTCDateTime` type decorator that normalises to UTC on write and
re-tags as UTC on read. Without it, SQLite returns naive datetimes and the first
comparison against `utcnow()` raises `TypeError` — in the account-lockout path, of all
places.

## Concurrency

Sensitivity sweeps, Monte Carlo runs, and the forecast grid search are CPU-bound NumPy
work measured in hundreds of milliseconds. Each is dispatched with `asyncio.to_thread`, so
one expensive request does not block the event loop for everyone else. NumPy releases the
GIL for the array operations that dominate these paths.

Monte Carlo draws are generated in chunks of 2,000 runs to bound peak memory: a naive
50,000-run draw over a 50x30 matrix would allocate roughly 600 MB in one go.

## Frontend

Three static files served by the API itself: `index.html`, `assets/app.js`,
`assets/styles.css`. No bundler, no npm, no CDN. Charts are drawn on `<canvas>` with about
120 lines of plotting code, which is smaller than any charting library's loader.

Two consequences worth stating. First, the CSP can be strict — `script-src 'self'` with no
`unsafe-inline` — because there is nothing inline to allow. Second, tokens live in module
scope only: no `localStorage`, no `sessionStorage`, so injected script cannot read them
back after a reload. The cost is that a page refresh requires signing in again, which is
the right side of that trade for a tool that ranks acquisitions.

## Scaling notes

The rate limiter is in-process, which is correct for the single-container reference
deployment and wrong behind multiple replicas. `MemoryRateLimiter` exposes `check` and
`reset`; a Redis implementation of the same two methods drops in without touching the
routers.

Beyond that: move to PostgreSQL (already supported via asyncpg), put the API behind a
reverse proxy terminating TLS, and set `EDID_FORCE_HTTPS=true` so forwarded-proto handling
and HSTS switch on.
