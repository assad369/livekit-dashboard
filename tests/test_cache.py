"""Tests for the agent-dispatch TTL cache and its keying.

The cache used to be keyed by LiveKit URL alone. Under multi-project that is
a cross-project data leak: two projects pointed at the same server with
different API keys would read each other's dispatch results.
"""

import time

import pytest

from app.services import cache
from app.services.livekit import LiveKitClient


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def test_same_url_different_credentials_get_distinct_keys():
    a = LiveKitClient("wss://lk.example.com", "APIaaa", "secret-a", project_id="p1")
    b = LiveKitClient("wss://lk.example.com", "APIbbb", "secret-b", project_id="p2")

    assert a.url == b.url
    assert a.cache_key != b.cache_key


def test_cached_dispatches_do_not_leak_between_projects():
    a = LiveKitClient("wss://lk.example.com", "APIaaa", "secret-a", project_id="p1")
    b = LiveKitClient("wss://lk.example.com", "APIbbb", "secret-b", project_id="p2")

    cache.set(a.cache_key, ["dispatch-for-a"], 0.1)

    assert cache.is_fresh(a.cache_key) is True
    assert cache.is_fresh(b.cache_key) is False
    assert cache.get(b.cache_key)["data"] == []


def test_changing_credentials_produces_a_fresh_key():
    """Editing a project's API key must not serve results cached under the old one."""
    before = LiveKitClient("wss://lk.example.com", "APIold", "s", project_id="p1")
    after = LiveKitClient("wss://lk.example.com", "APInew", "s", project_id="p1")

    cache.set(before.cache_key, ["stale"], 0.1)
    assert cache.is_fresh(after.cache_key) is False


def test_same_project_and_credentials_share_a_key():
    a = LiveKitClient("wss://lk.example.com", "APIaaa", "secret-a", project_id="p1")
    b = LiveKitClient("wss://lk.example.com", "APIaaa", "secret-a", project_id="p1")

    assert a.cache_key == b.cache_key


def test_cache_key_does_not_contain_the_secret():
    c = LiveKitClient("wss://lk.example.com", "APIaaa", "super-secret", project_id="p1")
    assert "super-secret" not in c.cache_key


def test_entry_expires_after_ttl(monkeypatch):
    c = LiveKitClient("wss://lk.example.com", "APIaaa", "s", project_id="p1")
    cache.set(c.cache_key, ["x"], 0.1)
    assert cache.is_fresh(c.cache_key) is True

    later = time.monotonic() + cache.TTL + 1
    monkeypatch.setattr(cache.time, "monotonic", lambda: later)
    assert cache.is_fresh(c.cache_key) is False


def test_invalidate_forces_a_refetch():
    c = LiveKitClient("wss://lk.example.com", "APIaaa", "s", project_id="p1")
    cache.set(c.cache_key, ["x"], 0.1)
    cache.invalidate(c.cache_key)
    assert cache.is_fresh(c.cache_key) is False


def test_client_falls_back_to_environment_credentials():
    """A bare LiveKitClient() must still work for the single-project deployment."""
    import os

    c = LiveKitClient()
    assert c.url == os.environ["LIVEKIT_URL"]
    assert c.key == os.environ["LIVEKIT_API_KEY"]
    assert c.secret == os.environ["LIVEKIT_API_SECRET"]
    assert c.project_id == "env"
