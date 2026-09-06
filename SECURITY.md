# Security

## Reporting a vulnerability

Open a GitHub security advisory on this repository, or email the maintainer listed in the
repository profile. Please do not open a public issue for an exploitable finding. Expect an
acknowledgement within three working days.

Include: affected endpoint or module, reproduction steps, and impact. A proof of concept
against your own local instance is welcome; do not test against anyone else's deployment.

## Controls

### Credentials
- Argon2id, `t=3, m=64 MiB, p=2, 32-byte output, 16-byte salt`, above the OWASP minimum.
- Rehash on successful login when the stored parameters fall behind the current policy.
- Policy: 12-128 characters, at least three character classes, and the account name may
  not appear in the password.
- Unknown accounts still pay an Argon2 verify against a fixed dummy hash, so login latency
  does not distinguish "no such account" from "wrong password".
- Five consecutive failures lock the account for 15 minutes. The counter and its audit row
  are committed before the 401 is raised, so the error path cannot roll back the evidence.

### Sessions
- Access tokens: JWT, 15 minutes, HS256 with an explicit algorithm allowlist (an `alg:none`
  token is rejected), issuer and audience validated, required claims enforced.
- Refresh tokens: 48 random bytes, opaque, stored only as a SHA-256 digest.
- Rotation on every refresh. Presenting a revoked token revokes the entire token family,
  on the assumption that a replay is theft.
- Role changes and deactivation revoke outstanding refresh tokens immediately; outstanding
  access tokens fail on the next request because the role claim no longer matches.

### Authorisation
- Role ladder `viewer < analyst < admin`, enforced by dependency rather than in handlers.
- Records belonging to another user return **404, not 403**, so identifiers cannot be
  enumerated by comparing status codes.
- Bulk score writes are validated against the target decision's own options and criteria.
  This is the IDOR boundary and it has a dedicated regression test.
- Admins cannot demote or deactivate themselves, so an organisation cannot lock itself out.

### Input and output
- Every inbound Pydantic model sets `extra="forbid"`: an unexpected `role` field in a
  registration payload is a 422, not a silent privilege grant.
- Lengths, ranges, and collection sizes are bounded on every field.
- Validation errors echo field paths only, never submitted values.
- The catch-all exception handler returns a generic body plus a request id; stack traces
  and driver messages never reach a client.

### Transport
- CSP without `unsafe-inline` — possible because the frontend has no inline script — plus
  `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'`.
- HSTS with preload when `EDID_FORCE_HTTPS=true`; HTTP is redirected unless a proxy has
  already set `X-Forwarded-Proto: https`.
- CORS is an explicit origin allowlist with credentials disabled: the API is bearer-token
  only, so there is no cookie surface to forge against.
- `TrustedHostMiddleware` with an allowlist; wildcards are rejected in production.
- Request bodies are capped on `Content-Length` before parsing.

### Availability
- Sliding-window rate limits per client, with a tighter bucket on credential endpoints and
  `Retry-After` on refusal.
- Hard ceilings on options, criteria, simulation runs, and forecast horizon.
- CPU-bound analytics run in a worker thread; Monte Carlo draws are chunked to bound peak
  memory.

### Auditability
- Append-only audit log of registration, login success and failure, refresh, replay
  detection, logout, role change, deactivation, and every write.
- Client addresses are stored as HMAC digests, never raw.
- Metadata is redacted against a key denylist before it is written.

### Deployment
- Production boot fails on a default secret key, a secret under 32 characters, a wildcard
  CORS origin, a wildcard trusted host, HTTPS disabled, or SQLite as the database.
- OpenAPI and `/docs` are disabled in production.
- The container runs as UID 10001 with a read-only root filesystem, all capabilities
  dropped, and `no-new-privileges`. CI fails if the image runs as root.

## Verification

```bash
make security   # bandit: 0 findings, 0 nosec suppressions
make audit      # pip-audit against the pinned dependency ranges
make test       # includes the auth, RBAC, IDOR, and rotation regression tests
```

CI additionally runs gitleaks for committed secrets, Trivy against the built image, and
CodeQL with `security-extended` on both Python and JavaScript.

## Known limitations

- The rate limiter is per process. Behind multiple replicas, back it with Redis; the
  interface is two methods.
- There is no email verification or password reset flow. Both need a mail transport, which
  this project deliberately does not ship.
- Audit rows are append-only by convention and access control, not by a database trigger or
  a hash chain. For a regulated deployment, add one.
- A page refresh in the dashboard requires signing in again. This is the intended
  consequence of keeping tokens out of browser storage.
