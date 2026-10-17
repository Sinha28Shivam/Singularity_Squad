"""PBKDF2-HMAC-SHA256 password hashing with a random per-user salt."""
from __future__ import annotations

import hashlib
import hmac
import secrets

ITERATIONS = 200_000


def hash_password(password: str) -> tuple[str, str]:
    """Return (hash_hex, salt_hex)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return digest.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except (TypeError, ValueError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return hmac.compare_digest(digest.hex(), hash_hex or "")
