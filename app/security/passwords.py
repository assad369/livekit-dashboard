"""Password hashing (argon2id).

argon2id is the current password-hashing recommendation and is memory-hard,
which is the property that matters against GPU cracking. The previous scheme
compared plaintext against an environment variable, so any process able to
read the environment — or any log that captured it — had the password.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# argon2-cffi's defaults track the RFC 9106 low-memory profile; they are a
# reasonable balance for a dashboard that hashes once per login.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an argon2id hash (which embeds its own salt and parameters)."""
    if not password:
        raise ValueError("password must not be empty")
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Constant-time verify. Returns False rather than raising on any failure."""
    if not stored_hash or not password:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than we now use."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return True
