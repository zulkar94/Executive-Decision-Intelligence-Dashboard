"""Authentication, session rotation, RBAC, and hardening behaviour."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.security.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password,
    verify_password,
)
from app.security.rate_limit import MemoryRateLimiter
from app.security.tokens import TokenError, create_access_token, decode_access_token, digest_token
from tests.conftest import ANALYST, VIEWER, auth_headers, login, register


def test_password_policy_rejects_weak_candidates() -> None:
    for candidate in ("short", "alllowercaseletters", "PASSWORD1234"):
        with pytest.raises(PasswordPolicyError):
            validate_password(candidate)
    validate_password("Str0ng-Passphrase!42")


def test_password_may_not_embed_the_account_name() -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password("Jsmith-Str0ng!42", email="jsmith@example.com")


def test_hashes_are_salted_and_verify_correctly() -> None:
    first = hash_password("Str0ng-Passphrase!42")
    second = hash_password("Str0ng-Passphrase!42")
    assert first != second  # per-hash salt
    assert first.startswith("$argon2id$")
    assert verify_password("Str0ng-Passphrase!42", first)
    assert not verify_password("wrong", first)


def test_verify_rejects_a_corrupt_hash_without_raising() -> None:
    assert not verify_password("anything", "not-a-hash")


def test_access_token_roundtrip_and_kind_enforcement() -> None:
    token, ttl = create_access_token(subject="user-1", role="admin", session="sess-1")
    claims = decode_access_token(token)
    assert (claims.subject, claims.role, claims.session) == ("user-1", "admin", "sess-1")
    assert ttl > 0
    with pytest.raises(TokenError):
        decode_access_token(token + "tampered")


def test_token_signed_with_another_key_is_rejected() -> None:
    import jwt

    forged = jwt.encode({"sub": "x", "kind": "access"}, "attacker-key", algorithm="HS256")
    with pytest.raises(TokenError):
        decode_access_token(forged)


def test_alg_none_token_is_rejected() -> None:
    import jwt

    forged = jwt.encode({"sub": "x", "role": "admin", "kind": "access"}, key="", algorithm="none")
    with pytest.raises(TokenError):
        decode_access_token(forged)


def test_refresh_digest_is_not_the_token() -> None:
    token = "opaque-refresh-token-value"
    assert digest_token(token) != token
    assert len(digest_token(token)) == 64


def test_memory_rate_limiter_blocks_then_recovers() -> None:
    limiter = MemoryRateLimiter()
    assert limiter.check("k", limit=2, window=60).allowed
    assert limiter.check("k", limit=2, window=60).allowed
    blocked = limiter.check("k", limit=2, window=60)
    assert not blocked.allowed and blocked.retry_after > 0
    limiter.reset("k")
    assert limiter.check("k", limit=2, window=60).allowed


async def test_first_account_becomes_admin_and_next_is_viewer(client: AsyncClient) -> None:
    first = await register(client, ANALYST)
    assert first["role"] == "admin"
    second = await register(client, VIEWER)
    assert second["role"] == "viewer"


async def test_registration_rejects_a_weak_password(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={"email": "weak@edid.io", "password": "password1234"}
    )
    assert response.status_code == 422


async def test_duplicate_registration_is_not_confirmed_to_the_caller(client: AsyncClient) -> None:
    await register(client, ANALYST)
    response = await client.post("/api/v1/auth/register", json=ANALYST)
    assert response.status_code == 409
    assert "exists" not in response.json()["detail"].lower()


async def test_login_failure_message_does_not_distinguish_the_cause(client: AsyncClient) -> None:
    await register(client, ANALYST)
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@edid.io", "password": "Str0ng-Passphrase!42"}
    )
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": ANALYST["email"], "password": "Wr0ng-Passphrase!42"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_account_locks_after_repeated_failures(client: AsyncClient) -> None:
    await register(client, ANALYST)
    for _ in range(5):
        await client.post(
            "/api/v1/auth/login",
            json={"email": ANALYST["email"], "password": "Wr0ng-Passphrase!42"},
        )
    response = await client.post("/api/v1/auth/login", json=ANALYST)
    assert response.status_code == 423


async def test_protected_route_requires_a_token(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nonsense"})
    ).status_code == 401
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": "Basic abc"})
    ).status_code == 401


async def test_refresh_rotates_the_token(client: AsyncClient) -> None:
    await register(client, ANALYST)
    pair = await login(client, ANALYST)
    rotated = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != pair["refresh_token"]


async def test_replaying_a_rotated_refresh_token_kills_the_family(client: AsyncClient) -> None:
    await register(client, ANALYST)
    pair = await login(client, ANALYST)
    rotated = (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    ).json()

    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert replay.status_code == 401
    assert "reuse" in replay.json()["detail"].lower()

    # The descendant token issued before the replay is now dead too.
    aftermath = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
    )
    assert aftermath.status_code == 401


async def test_logout_revokes_and_stays_silent_about_validity(client: AsyncClient) -> None:
    await register(client, ANALYST)
    pair = await login(client, ANALYST)
    assert (
        await client.post("/api/v1/auth/logout", json={"refresh_token": pair["refresh_token"]})
    ).status_code == 204
    assert (
        await client.post(
            "/api/v1/auth/logout", json={"refresh_token": "never-existed-token-value"}
        )
    ).status_code == 204
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    ).status_code == 401


async def test_revoke_all_ends_every_session(client: AsyncClient) -> None:
    await register(client, ANALYST)
    first = await login(client, ANALYST)
    second = await login(client, ANALYST)
    assert (
        await client.post("/api/v1/auth/sessions/revoke-all", headers=auth_headers(first))
    ).status_code == 204
    for pair in (first, second):
        assert (
            await client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        ).status_code == 401


async def test_viewer_cannot_create_a_decision(client: AsyncClient, viewer_headers) -> None:
    response = await client.post(
        "/api/v1/decisions", json={"title": "Viewer attempt"}, headers=viewer_headers
    )
    assert response.status_code == 403


async def test_viewer_cannot_reach_the_admin_surface(client: AsyncClient, viewer_headers) -> None:
    assert (await client.get("/api/v1/admin/users", headers=viewer_headers)).status_code == 403
    assert (await client.get("/api/v1/admin/audit", headers=viewer_headers)).status_code == 403


async def test_role_change_invalidates_outstanding_tokens(
    client: AsyncClient, admin_headers, viewer_headers
) -> None:
    users = (await client.get("/api/v1/admin/users", headers=admin_headers)).json()
    viewer = next(user for user in users if user["email"] == VIEWER["email"])
    promoted = await client.patch(
        f"/api/v1/admin/users/{viewer['id']}/role", json={"role": "analyst"}, headers=admin_headers
    )
    assert promoted.status_code == 200
    # The viewer's old access token carries a stale role claim.
    stale = await client.get("/api/v1/auth/me", headers=viewer_headers)
    assert stale.status_code == 401


async def test_admin_cannot_demote_or_deactivate_itself(client: AsyncClient, admin_headers) -> None:
    me = (await client.get("/api/v1/auth/me", headers=admin_headers)).json()
    demote = await client.patch(
        f"/api/v1/admin/users/{me['id']}/role", json={"role": "viewer"}, headers=admin_headers
    )
    assert demote.status_code == 409
    deactivate = await client.post(
        f"/api/v1/admin/users/{me['id']}/deactivate", headers=admin_headers
    )
    assert deactivate.status_code == 409


async def test_deactivated_user_loses_access(
    client: AsyncClient, admin_headers, viewer_headers
) -> None:
    users = (await client.get("/api/v1/admin/users", headers=admin_headers)).json()
    viewer = next(user for user in users if user["email"] == VIEWER["email"])
    assert (
        await client.post(f"/api/v1/admin/users/{viewer['id']}/deactivate", headers=admin_headers)
    ).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=viewer_headers)).status_code == 401


async def test_security_headers_are_present_on_every_response(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Request-ID"]


async def test_request_id_is_echoed_when_supplied(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"


async def test_oversized_body_is_refused_before_parsing(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        content=b"x" * 4096,
        headers={"Content-Type": "application/json", "Content-Length": "20000000"},
    )
    assert response.status_code == 413


async def test_unknown_fields_are_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "x@edid.io", "password": "Str0ng-Passphrase!42", "role": "admin"},
    )
    assert response.status_code == 422


async def test_audit_trail_records_logins_without_secrets(
    client: AsyncClient, admin_headers
) -> None:
    entries = (await client.get("/api/v1/admin/audit", headers=admin_headers)).json()
    actions = {entry["action"] for entry in entries}
    assert {"user.register", "auth.login"} <= actions
    serialised = str(entries)
    assert ANALYST["password"] not in serialised


async def test_rate_limiter_returns_429_with_retry_after(client: AsyncClient, monkeypatch) -> None:
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "auth_rate_limit_requests", 3)
    statuses = [
        (
            await client.post(
                "/api/v1/auth/login",
                json={"email": "nobody@edid.io", "password": "Str0ng-Passphrase!42"},
            )
        ).status_code
        for _ in range(6)
    ]
    assert 429 in statuses
