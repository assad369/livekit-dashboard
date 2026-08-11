"""Tests for secret-at-rest encryption."""

import pytest
from cryptography.fernet import InvalidToken

from app.security import crypto


@pytest.fixture
def real_key(monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", crypto.generate_key())
    monkeypatch.delenv("APP_ENCRYPTION_KEY_OLD", raising=False)
    yield


def test_round_trip(real_key):
    assert crypto.decrypt(crypto.encrypt("APIsecret123")) == "APIsecret123"


def test_ciphertext_does_not_contain_the_plaintext(real_key):
    token = crypto.encrypt("super-secret-value")
    assert "super-secret-value" not in token


def test_each_encryption_is_distinct(real_key):
    """Fernet includes a random IV, so identical inputs must not match."""
    assert crypto.encrypt("same") != crypto.encrypt("same")


def test_tampered_token_is_rejected(real_key):
    token = crypto.encrypt("value")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(InvalidToken):
        crypto.decrypt(tampered)


def test_token_from_another_key_is_rejected(monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", crypto.generate_key())
    token = crypto.encrypt("value")

    monkeypatch.setenv("APP_ENCRYPTION_KEY", crypto.generate_key())
    with pytest.raises(InvalidToken):
        crypto.decrypt(token)


def test_key_rotation_decrypts_old_tokens(monkeypatch):
    """Old data stays readable while new data is written with the new key."""
    old_key = crypto.generate_key()
    monkeypatch.setenv("APP_ENCRYPTION_KEY", old_key)
    old_token = crypto.encrypt("legacy-secret")

    new_key = crypto.generate_key()
    monkeypatch.setenv("APP_ENCRYPTION_KEY", new_key)
    monkeypatch.setenv("APP_ENCRYPTION_KEY_OLD", old_key)

    assert crypto.decrypt(old_token) == "legacy-secret"

    # New writes use the new key only — readable after the old key is retired.
    new_token = crypto.encrypt("fresh-secret")
    monkeypatch.delenv("APP_ENCRYPTION_KEY_OLD")
    assert crypto.decrypt(new_token) == "fresh-secret"


def test_refuses_the_default_development_secret(monkeypatch):
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("APP_SECRET_KEY", crypto.DEV_SECRET_KEY)

    assert crypto.is_configured() is False
    assert "default development key" in crypto.configuration_error()
    with pytest.raises(crypto.EncryptionNotConfigured):
        crypto.encrypt("value")


def test_derives_a_key_from_app_secret_when_no_explicit_key(monkeypatch):
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("APP_ENCRYPTION_KEY_OLD", raising=False)
    monkeypatch.setenv("APP_SECRET_KEY", "a-real-unique-production-secret")

    assert crypto.is_configured() is True
    assert crypto.configuration_error() is None
    assert crypto.decrypt(crypto.encrypt("derived")) == "derived"


def test_derived_key_changes_with_app_secret(monkeypatch):
    """Documents the coupling: rotating APP_SECRET_KEY orphans stored secrets."""
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("APP_ENCRYPTION_KEY_OLD", raising=False)
    monkeypatch.setenv("APP_SECRET_KEY", "secret-one")
    token = crypto.encrypt("value")

    monkeypatch.setenv("APP_SECRET_KEY", "secret-two")
    with pytest.raises(InvalidToken):
        crypto.decrypt(token)


def test_try_decrypt_returns_none_instead_of_raising(real_key):
    assert crypto.try_decrypt("not-a-token") is None
    assert crypto.try_decrypt(crypto.encrypt("ok")) == "ok"


def test_generate_key_produces_usable_keys(monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", crypto.generate_key())
    assert crypto.decrypt(crypto.encrypt("x")) == "x"
