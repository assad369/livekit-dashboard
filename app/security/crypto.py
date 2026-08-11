"""Symmetric encryption for secrets stored in the database.

Project API secrets are credentials for a live media server; storing them in
plaintext would turn read access to the database into full control of every
LiveKit deployment the dashboard knows about.

Key material comes from ``APP_ENCRYPTION_KEY`` (a urlsafe-base64 32-byte
Fernet key). If unset, a key is derived from ``APP_SECRET_KEY`` so the app
still runs — but that couples session signing to data-at-rest encryption, so
it warns loudly and is refused outright while APP_SECRET_KEY is still the
shipped development default.

BACK THIS KEY UP. Losing it makes every stored project secret unrecoverable.
Rotate by moving the old key to ``APP_ENCRYPTION_KEY_OLD``; MultiFernet then
decrypts with either key while encrypting only with the new one.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)


DEV_SECRET_KEY = "dev-secret-key-change-in-production"
_HKDF_INFO = b"livekit-dashboard/secret-encryption/v1"

_warned = False


class EncryptionNotConfigured(RuntimeError):
    """Raised when secrets cannot be encrypted safely."""


def has_explicit_key() -> bool:
    return bool(os.environ.get("APP_ENCRYPTION_KEY", "").strip())


def _derive_key_from_app_secret() -> bytes:
    """Derive a Fernet key from APP_SECRET_KEY via HKDF-SHA256."""
    app_secret = os.environ.get("APP_SECRET_KEY", DEV_SECRET_KEY)
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(app_secret.encode())
    return base64.urlsafe_b64encode(raw)


def is_configured() -> bool:
    """True when encryption can be performed with a non-default key."""
    if has_explicit_key():
        return True
    return os.environ.get("APP_SECRET_KEY", DEV_SECRET_KEY) != DEV_SECRET_KEY


def configuration_error() -> Optional[str]:
    """Human-readable reason encryption is unsafe, or None when it is fine."""
    if is_configured():
        return None
    return (
        "Refusing to encrypt secrets with the default development key. "
        "Set APP_ENCRYPTION_KEY (openssl rand -base64 32) or APP_SECRET_KEY "
        "to a unique value, and back it up — losing it makes stored project "
        "secrets unrecoverable."
    )


def _fernet() -> MultiFernet:
    global _warned

    error = configuration_error()
    if error:
        raise EncryptionNotConfigured(error)

    if has_explicit_key():
        primary = os.environ["APP_ENCRYPTION_KEY"].strip().encode()
    else:
        if not _warned:
            logger.warning(
                "APP_ENCRYPTION_KEY is not set — deriving the secret-encryption key "
                "from APP_SECRET_KEY. Changing APP_SECRET_KEY will make stored "
                "project secrets undecryptable. Set APP_ENCRYPTION_KEY explicitly."
            )
            _warned = True
        primary = _derive_key_from_app_secret()

    keys = [Fernet(primary)]

    old = os.environ.get("APP_ENCRYPTION_KEY_OLD", "").strip()
    if old:
        # Decrypt-only fallback during rotation; MultiFernet always encrypts
        # with the first key.
        keys.append(Fernet(old.encode()))

    return MultiFernet(keys)


def encrypt(plaintext: str) -> str:
    """Encrypt *plaintext*, returning a urlsafe token."""
    if plaintext is None:
        raise ValueError("cannot encrypt None")
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by encrypt(). Raises InvalidToken if tampered."""
    return _fernet().decrypt(token.encode()).decode()


def try_decrypt(token: str) -> Optional[str]:
    """Decrypt, returning None instead of raising. For non-fatal call sites."""
    try:
        return decrypt(token)
    except (InvalidToken, EncryptionNotConfigured, ValueError, TypeError) as exc:
        logger.error("failed to decrypt a stored secret: %s", exc)
        return None


def generate_key() -> str:
    """Generate a fresh key, for `make gen-key` and setup docs."""
    return Fernet.generate_key().decode()
