"""Real SQLite/HTTP regressions for account changes in a shared profile."""
import json
import sqlite3

import httpx
import pytest

from test_proactive_tools import XMemoMemoryProvider
from hermes_xmemo.xmemo.cache import identity_fingerprint
from hermes_xmemo.xmemo.cache import XMemoLocalCache
from hermes_xmemo.xmemo.client import XMemoClient
from hermes_xmemo.xmemo.cache import with_cache_identity


def make_provider(db, *, key="account-A-secret", agent="agent-A", url="https://fixture.invalid", offline=False, status=200):
    provider = XMemoMemoryProvider()
    provider._config = {"api_key": key, "agent_id": agent, "base_url": url,
                        "read_bucket": "work", "read_scope": "shared"}
    provider._local_cache = XMemoLocalCache(db)
    calls = []

    def handle(request):
        calls.append(request)
        if offline:
            raise httpx.ConnectError("controlled offline", request=request)
        if status >= 400:
            return httpx.Response(status, json={"error": "denied"})
        item = {"id": "fixture", "content": "account-A-only", "score": 0.9}
        return httpx.Response(status, json={"results": [item]} if request.method == "GET"
                              else {"context_text": "account-A-only", "items": [item]})

    client = XMemoClient(url, key, agent_id=agent, transport=httpx.MockTransport(handle))
    provider._client = client
    return provider, client, calls


def invoke(provider, client, operation, **args):
    if operation == "prefetch":
        provider.queue_prefetch("same query", session_id="test")
        for thread in provider._prefetch_threads.values():
            thread.join(5)
            assert not thread.is_alive()
        return provider.prefetch("same query", session_id="test")
    return getattr(provider, "_handle_" + operation)(client, {"query": "same query", **args})


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("operation", ["search", "recall_context", "prefetch"])
@pytest.mark.parametrize("identity", [{"key": "account-B-secret"}, {"agent": "agent-B"}, {"url": "https://other.invalid"}])
def test_identity_change_never_reads_other_account(tmp_path, monkeypatch, stale, operation, identity):
    monkeypatch.setattr("time.sleep", lambda _: None)
    db = tmp_path / "cache.db"
    a, ca, _ = make_provider(db)
    assert "account-A-only" in invoke(a, ca, operation)
    if stale:
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE recall_cache SET fresh_until=0")
    b, cb, calls = make_provider(db, offline=True, **identity)
    result = invoke(b, cb, operation)
    assert "account-A-only" not in result
    assert calls  # A different identity cannot skip authorization via a fresh hit.
    ca.close()
    cb.close()


def test_same_identity_reuses_search_without_secret_on_disk(tmp_path):
    db = tmp_path / "cache.db"
    a, ca, calls = make_provider(db)
    first = json.loads(invoke(a, ca, "search"))
    second = json.loads(invoke(a, ca, "search"))
    assert first["results"] == second["results"]
    assert second["source"] == "fresh_cache" and len(calls) == 1
    # A new provider/cache instance retains identity-bound entries.
    b, cb, bcalls = make_provider(db)
    assert json.loads(invoke(b, cb, "search"))["source"] == "fresh_cache"
    assert not bcalls
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT params_json, hit_count FROM recall_cache").fetchone()
        assert row[1] == 2 and "account-A-secret" not in row[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for path in tmp_path.iterdir():
        assert b"account-A-secret" not in path.read_bytes()
    ca.close(); cb.close()


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("operation", ["search", "recall_context", "prefetch"])
def test_auth_failure_never_falls_back(tmp_path, operation, status):
    db = tmp_path / "cache.db"
    a, ca, _ = make_provider(db)
    invoke(a, ca, operation)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE recall_cache SET fresh_until=0")
    b, cb, calls = make_provider(db, status=status)
    assert "account-A-only" not in invoke(b, cb, operation)
    assert len(calls) == 1
    ca.close(); cb.close()


@pytest.mark.parametrize("field,value", [
    ("since", "2026-01-01T00:00:00Z"), ("until", "2026-02-01T00:00:00Z"),
    ("owner_timezone", "UTC"), ("reference_time", "2026-03-01T00:00:00Z"),
    ("temporal_axis_mode", "updated_at"), ("query_mode", "topic"), ("time_order", "asc"),
])
def test_same_identity_temporal_keys_do_not_cross(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr("time.sleep", lambda _: None)
    db = tmp_path / "cache.db"
    a, ca, _ = make_provider(db)
    invoke(a, ca, "search")
    b, cb, calls = make_provider(db, offline=True)
    assert "account-A-only" not in invoke(b, cb, "search", **{field: value})
    assert calls
    ca.close(); cb.close()


def test_legacy_cleanup_is_read_cache_only(tmp_path):
    db = tmp_path / "cache.db"
    cache = XMemoLocalCache(db)
    legacy = {"bucket": "work"}
    bound = with_cache_identity(legacy, "synthetic-key", "agent")
    cache.put_cached_recall("search", "legacy", legacy, {"results": []})
    cache.put_cached_recall("search", "legacy-valid-json", legacy, {"results": []})
    cache.put_cached_recall("search", "valid", bound, {"results": []})
    cache.put_cached_recall("search", "expired", bound, {"results": []}, max_stale_ttl=-1)
    # New keys cannot reach legacy rows even before cleanup.
    assert cache.get_cached_recall("search", "legacy", bound) is None
    cache.enqueue_write("remember", "/fixture", "POST", {"content": "queued"}, "fixture-idempotency", identity=identity_fingerprint("synthetic-key", "agent"))
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM write_outbox").fetchall()
        conn.execute("UPDATE recall_cache SET params_json='not json' WHERE query='legacy'")
    upgraded = XMemoLocalCache(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT query FROM recall_cache").fetchall() == [("valid",)]
        assert conn.execute("SELECT * FROM write_outbox").fetchall() == before
    assert upgraded.get_cached_recall("search", "valid", bound) is not None


def test_identity_framing_and_anonymous_are_distinct():
    assert with_cache_identity({}, "a\0b", "c") != with_cache_identity({}, "a", "b\0c")
    assert with_cache_identity({}, "", "agent") != with_cache_identity({}, "key", "agent")
    assert with_cache_identity({}, "", "agent") == with_cache_identity({}, "", "agent")
