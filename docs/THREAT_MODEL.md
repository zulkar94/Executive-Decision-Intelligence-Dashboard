# Threat model

## Assets

1. **Decision content** — the options an organisation is weighing, the criteria it uses,
   and how it scores them. Often material non-public information: acquisitions, layoffs,
   vendor exits. Leakage is the primary harm.
2. **Credentials and sessions** — the path to everything above.
3. **The audit trail** — its integrity is what makes the rest defensible after the fact.
4. **Availability** — the analytics endpoints are the expensive ones and therefore the
   attractive ones to abuse.

## Adversaries

| Adversary | Capability | Goal |
|---|---|---|
| Unauthenticated internet | Reach any exposed endpoint | Enumerate accounts, credential-stuff, mine the API for data |
| Authenticated viewer | Valid low-privilege token | Reach decisions they do not own, escalate to analyst or admin |
| Malicious analyst | Valid write access | Read other teams' decisions, tamper with the trail |
| Network observer | Sees traffic | Harvest tokens in transit |
| Database reader | A dump, a backup, a replica | Replay credentials or sessions |

## Attacks and mitigations

**Account enumeration.** Registering an existing address returns a generic 409 that does
not confirm the address is taken. Login returns one message for every failure mode.
Unknown accounts still pay the Argon2 cost via a dummy verify, so the timing matches.

**Credential stuffing and brute force.** Argon2id makes each guess expensive. Five
failures lock the account for 15 minutes. Credential endpoints have their own tighter rate
limit bucket, keyed separately from general traffic.

**Token theft.** Access tokens expire in 15 minutes. Refresh tokens are opaque and rotate
on every use, so a stolen one is valid only until the legitimate client next refreshes —
at which point the replay is detected and the whole family is revoked. Both parties are
logged out, which is loud, and loud is correct here.

**Database compromise.** Passwords are Argon2id hashes. Refresh tokens exist only as
SHA-256 digests, so they cannot be replayed against the API. Audit rows carry HMAC client
digests rather than IP addresses, so a dump does not hand over a movement log.

**Forged tokens.** Signature verification uses an explicit algorithm allowlist, so
`alg: none` and algorithm-confusion attempts fail. Issuer, audience, and expiry are all
enforced, and a token whose `kind` is not `access` is rejected even if correctly signed.

**Privilege escalation via mass assignment.** Every inbound model forbids unknown fields,
so `{"email": ..., "password": ..., "role": "admin"}` is a 422. Roles change only through
the admin endpoint, which is behind the admin dependency, records an audit row, and
revokes the target's sessions.

**Insecure direct object reference.** Ownership is checked in a dependency, not in
handlers, so no route can forget it. Score writes are validated cell by cell against the
target decision's own options and criteria — the specific mistake that would otherwise let
an analyst write into someone else's matrix by guessing an id.

**Stale privileges.** A demoted admin's unexpired access token still claims `admin`. The
authentication dependency compares the claim against the stored role and rejects the
mismatch, so demotion takes effect on the next request rather than up to 15 minutes later.

**Cross-site scripting.** The frontend never assigns `innerHTML` from server data; every
write goes through `textContent` or explicit element creation. CSP has no `unsafe-inline`
and no third-party sources. Tokens are held in module scope, not browser storage, so even
a successful injection cannot read a persisted session back.

**Cross-site request forgery.** There is no cookie authentication. CORS is an explicit
allowlist with credentials disabled.

**Resource exhaustion.** Simulation runs, forecast horizons, option counts, and criterion
counts all have ceilings. Request bodies are capped before parsing. Heavy NumPy work runs
in a thread so it cannot stall the event loop, and Monte Carlo draws are chunked so a large
run cannot balloon memory.

**Log injection and secret leakage.** Audit metadata is redacted against a key denylist
before it is stored. Validation errors report field paths, never values. The catch-all
handler returns a request id, not a stack trace.

## Out of scope

Physical access to the host, a compromised base image, a malicious maintainer, and
side-channel attacks against the Argon2 implementation itself. Multi-tenant isolation
beyond per-user ownership is not modelled: this is a single-organisation tool.

## Residual risk

An admin account is a full compromise of the data by design; protect it with a strong
password and rotate it. The in-process rate limiter is bypassable by distributing requests
across replicas. Audit integrity relies on database access control rather than
cryptographic chaining.
