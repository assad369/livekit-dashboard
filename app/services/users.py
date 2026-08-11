"""Admin account storage and authentication.

Single-admin by design: there is one account, bootstrapped from
ADMIN_USERNAME/ADMIN_PASSWORD on first run and thereafter stored in MongoDB
with an argon2id hash.

The environment fallback is deliberate and load-bearing. When Mongo is
unconfigured or down, authenticating against the env vars is what keeps the
dashboard reachable — locking the operator out of the tool they would use to
diagnose the outage is a worse failure than the weaker credential check.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.security.passwords import hash_password, needs_rehash, verify_password

logger = logging.getLogger(__name__)

COLLECTION = "users"
ENV_USER_ID = "env"


@dataclass(frozen=True)
class User:
    id: str
    username: str
    source: str  # "mongo" | "env"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def env_username() -> str:
    return os.environ.get("ADMIN_USERNAME", "admin")


def env_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "changeme")


def using_default_password() -> bool:
    """True when the shipped default password is still in effect."""
    return env_password() == "changeme"


async def bootstrap_admin(db) -> Optional[User]:
    """Create the admin account from the environment if none exists.

    Idempotent: does nothing once any user exists, so changing ADMIN_PASSWORD
    later does not silently reset a password the operator changed in the app.
    """
    if db is None:
        return None

    existing = await db[COLLECTION].count_documents({}, limit=1)
    if existing:
        return None

    username = env_username()
    password = env_password()
    if not username or not password:
        logger.warning("cannot bootstrap admin: ADMIN_USERNAME/ADMIN_PASSWORD not set")
        return None

    if using_default_password():
        logger.warning(
            "Bootstrapping the admin account with the default password 'changeme'. "
            "Change it immediately."
        )

    doc = {
        "_id": "admin",
        "username": username,
        "password_hash": hash_password(password),
        "created_at": _now(),
        "last_login": None,
        "disabled": False,
        "source": "bootstrap",
    }
    await db[COLLECTION].insert_one(doc)
    logger.info("bootstrapped admin account %r", username)
    return User(id="admin", username=username, source="mongo")


# A precomputed hash to verify against when there is no account to check.
# argon2 is deliberately slow, so returning early on an unknown username would
# answer in ~1 ms versus ~50 ms for a real one — a timing oracle that defeats
# the generic "invalid username or password" message.
_DUMMY_HASH = hash_password("not-a-real-password")


def _burn_hash_time() -> None:
    verify_password(_DUMMY_HASH, "wrong")


async def _authenticate_env(username: str, password: str) -> Optional[User]:
    """Constant-time comparison against the environment credentials."""
    expected_user = env_username()
    expected_pass = env_password()
    user_ok = secrets.compare_digest(username.encode(), expected_user.encode())
    pass_ok = secrets.compare_digest(password.encode(), expected_pass.encode())
    if user_ok and pass_ok:
        return User(id=ENV_USER_ID, username=expected_user, source="env")
    return None


async def authenticate(db, username: str, password: str) -> Optional[User]:
    """Verify credentials, preferring the database over the environment."""
    if not username or not password:
        return None

    if db is None:
        logger.warning("authenticating against environment credentials (no database)")
        return await _authenticate_env(username, password)

    doc = await db[COLLECTION].find_one({"username": username})
    if doc is None:
        # No stored account yet (e.g. bootstrap has not run). Fall back so a
        # fresh deployment is not locked out of its own dashboard.
        count = await db[COLLECTION].count_documents({}, limit=1)
        if count == 0:
            return await _authenticate_env(username, password)
        _burn_hash_time()
        return None

    if doc.get("disabled"):
        _burn_hash_time()
        return None

    if not verify_password(doc.get("password_hash", ""), password):
        return None

    updates: dict = {"last_login": _now()}
    if needs_rehash(doc["password_hash"]):
        updates["password_hash"] = hash_password(password)
    await db[COLLECTION].update_one({"_id": doc["_id"]}, {"$set": updates})

    return User(id=str(doc["_id"]), username=doc["username"], source="mongo")


async def get_user(db, user_id: str) -> Optional[User]:
    if user_id == ENV_USER_ID:
        return User(id=ENV_USER_ID, username=env_username(), source="env")
    if db is None:
        return None
    doc = await db[COLLECTION].find_one({"_id": user_id})
    if doc is None or doc.get("disabled"):
        return None
    return User(id=str(doc["_id"]), username=doc["username"], source="mongo")


async def change_password(db, user_id: str, new_password: str) -> bool:
    """Set a new password. Returns False when there is nowhere to store it."""
    if db is None or user_id == ENV_USER_ID:
        return False
    if not new_password:
        raise ValueError("password must not be empty")

    result = await db[COLLECTION].update_one(
        {"_id": user_id},
        {"$set": {"password_hash": hash_password(new_password), "updated_at": _now()}},
    )
    return result.matched_count == 1
