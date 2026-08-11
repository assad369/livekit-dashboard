"""Tests for admin account storage and authentication."""

import os

import pytest

from app.security.passwords import hash_password, needs_rehash, verify_password
from app.services import users as user_service
from tests.fake_mongo import FakeDatabase


@pytest.fixture
def db():
    return FakeDatabase()


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def test_hash_is_not_the_plaintext():
    hashed = hash_password("hunter2")
    assert "hunter2" not in hashed
    assert hashed.startswith("$argon2")


def test_verify_round_trip():
    hashed = hash_password("hunter2")
    assert verify_password(hashed, "hunter2") is True
    assert verify_password(hashed, "hunter3") is False


def test_same_password_hashes_differently():
    """Per-hash salt: identical passwords must not produce identical hashes."""
    assert hash_password("same") != hash_password("same")


def test_verify_is_total():
    """Malformed input returns False rather than raising into a request handler."""
    assert verify_password("", "x") is False
    assert verify_password("not-a-hash", "x") is False
    assert verify_password(hash_password("x"), "") is False


def test_empty_password_is_refused():
    with pytest.raises(ValueError):
        hash_password("")


def test_needs_rehash_on_garbage():
    assert needs_rehash("not-a-hash") is True
    assert needs_rehash(hash_password("x")) is False


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

async def test_bootstrap_creates_the_admin_from_the_environment(db):
    created = await user_service.bootstrap_admin(db)

    assert created is not None
    assert created.username == os.environ["ADMIN_USERNAME"]

    doc = await db["users"].find_one({"username": os.environ["ADMIN_USERNAME"]})
    assert doc["password_hash"].startswith("$argon2")


async def test_bootstrap_never_stores_the_plaintext_password(db):
    await user_service.bootstrap_admin(db)
    doc = await db["users"].find_one({})
    assert os.environ["ADMIN_PASSWORD"] not in str(doc)


async def test_bootstrap_is_idempotent(db):
    await user_service.bootstrap_admin(db)
    assert await user_service.bootstrap_admin(db) is None
    assert await db["users"].count_documents({}) == 1


async def test_bootstrap_does_not_reset_a_changed_password(db):
    """Re-running bootstrap must not undo a password change made in the app."""
    await user_service.bootstrap_admin(db)
    await user_service.change_password(db, "admin", "a-new-password")

    await user_service.bootstrap_admin(db)

    user = await user_service.authenticate(db, os.environ["ADMIN_USERNAME"], "a-new-password")
    assert user is not None
    assert await user_service.authenticate(
        db, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"]
    ) is None


async def test_bootstrap_is_a_noop_without_a_database():
    assert await user_service.bootstrap_admin(None) is None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

async def test_authenticate_against_the_database(db):
    await user_service.bootstrap_admin(db)

    user = await user_service.authenticate(
        db, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"]
    )
    assert user is not None
    assert user.source == "mongo"


async def test_authenticate_rejects_a_bad_password(db):
    await user_service.bootstrap_admin(db)
    assert await user_service.authenticate(db, os.environ["ADMIN_USERNAME"], "nope") is None


async def test_authenticate_rejects_an_unknown_user(db):
    await user_service.bootstrap_admin(db)
    assert await user_service.authenticate(db, "someone-else", "whatever") is None


async def test_authenticate_rejects_a_disabled_account(db):
    await user_service.bootstrap_admin(db)
    await db["users"].update_one({"_id": "admin"}, {"$set": {"disabled": True}})

    assert await user_service.authenticate(
        db, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"]
    ) is None


async def test_authenticate_records_last_login(db):
    await user_service.bootstrap_admin(db)
    await user_service.authenticate(
        db, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"]
    )
    doc = await db["users"].find_one({"_id": "admin"})
    assert doc["last_login"] is not None


async def test_env_fallback_when_no_database():
    """A Mongo outage must not lock the operator out of the dashboard."""
    user = await user_service.authenticate(
        None, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"]
    )
    assert user is not None
    assert user.source == "env"
    assert user.id == user_service.ENV_USER_ID


async def test_env_fallback_when_the_collection_is_empty(db):
    """A fresh deployment must not be locked out before bootstrap runs."""
    user = await user_service.authenticate(
        db, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"]
    )
    assert user is not None
    assert user.source == "env"


async def test_no_env_fallback_once_an_account_exists(db):
    """After bootstrap the database is authoritative — env creds alone must fail."""
    await user_service.bootstrap_admin(db)
    await user_service.change_password(db, "admin", "changed-in-app")

    assert await user_service.authenticate(
        db, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"]
    ) is None


async def test_authenticate_rejects_blanks(db):
    assert await user_service.authenticate(db, "", "") is None


# ---------------------------------------------------------------------------
# Lookups and password change
# ---------------------------------------------------------------------------

async def test_get_user(db):
    await user_service.bootstrap_admin(db)
    assert (await user_service.get_user(db, "admin")).username == os.environ["ADMIN_USERNAME"]
    assert await user_service.get_user(db, "missing") is None


async def test_get_user_resolves_the_env_account_without_a_database():
    user = await user_service.get_user(None, user_service.ENV_USER_ID)
    assert user is not None
    assert user.source == "env"


async def test_change_password(db):
    await user_service.bootstrap_admin(db)
    assert await user_service.change_password(db, "admin", "brand-new") is True
    assert await user_service.authenticate(
        db, os.environ["ADMIN_USERNAME"], "brand-new"
    ) is not None


async def test_change_password_without_storage_reports_failure():
    assert await user_service.change_password(None, "admin", "x") is False


async def test_change_password_refuses_empty(db):
    await user_service.bootstrap_admin(db)
    with pytest.raises(ValueError):
        await user_service.change_password(db, "admin", "")


def test_default_password_is_flagged(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "changeme")
    assert user_service.using_default_password() is True
    monkeypatch.setenv("ADMIN_PASSWORD", "something-else")
    assert user_service.using_default_password() is False
