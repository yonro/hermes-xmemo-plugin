"""Controlled-clock regressions: traffic must not postpone breaker recovery."""
import json
import pytest
from test_cache_identity import make_provider, invoke
from hermes_xmemo.xmemo import _BREAKER_THRESHOLD, _BREAKER_COOLDOWN_SECONDS


@pytest.mark.parametrize("operation", ["search", "recall_context", "prefetch", "write"])
def test_rejections_preserve_deadline_then_recover(tmp_path, monkeypatch, caplog, operation):
    clock = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda _: None)
    p, c, calls = make_provider(tmp_path / "cache.db")
    p._trigger_outbox_sync = lambda: None
    for _ in range(_BREAKER_THRESHOLD):
        p._record_failure("initial backend failure")
    deadline = p._breaker_open_until
    caplog.clear()

    def request():
        if operation == "write":
            return p._run_write_with_outbox(c, "remember", "/fixture", "POST", {},
                lambda key: c._request("POST", "/fixture", json_body={}, idempotency_key=key))
        return invoke(p, c, operation)

    clock[0] += 60
    request()
    assert p._breaker_open_until == deadline
    assert p._consecutive_failures == _BREAKER_THRESHOLD
    assert not calls
    assert "tripped" not in caplog.text
    clock[0] = deadline
    request()
    assert len(calls) == 1
    assert p._consecutive_failures == 0 and p._status == "online"
    assert not p._is_breaker_open()
    c.close()


@pytest.mark.parametrize("operation", ["search", "recall_context", "prefetch"])
def test_failed_probe_reopens_immediately(tmp_path, monkeypatch, operation):
    clock = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda _: None)
    p, c, calls = make_provider(tmp_path / "cache.db", offline=True)
    for _ in range(_BREAKER_THRESHOLD):p._record_failure("initial")
    clock[0] = p._breaker_open_until
    invoke(p, c, operation)
    assert len(calls) == 3  # One logical probe with existing bounded HTTP retries.
    assert p._is_breaker_open()
    assert p._breaker_open_until == clock[0] + _BREAKER_COOLDOWN_SECONDS
    c.close()


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("operation", ["search", "recall_context", "prefetch"])
def test_open_breaker_preserves_fallback(tmp_path, monkeypatch, cached, operation):
    monkeypatch.setattr("time.monotonic", lambda: 1000.0)
    p, c, calls = make_provider(tmp_path / "cache.db")
    if cached:
        invoke(p, c, operation)
        with p._local_cache._get_conn() as conn:conn.execute("UPDATE recall_cache SET fresh_until=0")
    for _ in range(_BREAKER_THRESHOLD):p._record_failure("initial")
    before = len(calls)
    deadline = p._breaker_open_until
    result = invoke(p, c, operation)
    assert len(calls) == before
    assert p._breaker_open_until == deadline
    assert ("account-A-only" in result) == cached
    if operation != "prefetch":
        parsed = json.loads(result)
        if cached:assert parsed["stale"] is True and parsed["source"] == "cache"
        else:assert "no cached copy" in parsed["error"] and parsed["note"]
    elif cached:assert "Degraded:" in result
    c.close()
