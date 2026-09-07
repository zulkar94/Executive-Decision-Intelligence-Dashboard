"""Argon2id password hashing with rehash-on-login and a dummy-verify path."""

from __future__ import annotations

import contextlib
import re

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# OWASP Password Storage Cheat Sheet (Argon2id): m=19 MiB, t=2, p=1 minimum.
_hasher = PasswordHasher(
    time_cost=3, memory_cost=65_536, parallelism=2, hash_len=32, salt_len=16, type=Type.ID
)

# Precomputed hash used to equalise timing when an account does not exist.
_DUMMY_HASH = _hasher.hash("timing-equalisation-placeholder")

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128  # bounded: Argon2 cost is attacker-controllable otherwise

_CLASSES = (
    re.compile(r"[a-z]"),
    re.compile(r"[A-Z]"),
    re.compile(r"\d"),
    re.compile(r"[^\w\s]"),
)


class PasswordPolicyError(ValueError):
    """Raised when a candidate password fails policy."""


def validate_password(password: str, *, email: str | None = None) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters"
        )
    if sum(1 for pattern in _CLASSES if pattern.search(password)) < 3:
        raise PasswordPolicyError(
            "Password must mix at least three of: lowercase, uppercase, digit, symbol"
        )
    if email:
        local = email.split("@", 1)[0].lower()
        if local and local in password.lower():
            raise PasswordPolicyError("Password must not contain the account name")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def verify_dummy() -> None:
    """Burn comparable CPU for unknown accounts so timing does not leak existence."""
    # The mismatch is the point: the CPU cost is what equalises the response time.
    with contextlib.suppress(Exception):
        _hasher.verify(_DUMMY_HASH, "wrong-password")


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True
