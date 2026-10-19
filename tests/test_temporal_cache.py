"""Real SQLite cache isolation and baseline SHA-256 compatibility."""
import importlib.util
from pathlib import Path

import pytest


def load(name):
    path = Path(__file__).resolve().parents[1] / "src/hermes_xmemo/xmemo" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_cache_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Cache = load("cache").XMemoLocalCache
identity = load("cache").with_cache_identity
build = load("temporal").build_cache_params
COMMON = {"bucket": "%", "scope": None, "memory_type": "auto", "base_url": "https://xmemo.dev"}
LEGACY = {
    "search": {**COMMON, "limit": 10},
    "recall_context": {**COMMON, "max_items": 5, "max_tokens": 900, "prefer_working": True},
}
# Computed with git show HEAD:src/hermes_xmemo/xmemo/cache.py at 30fe1bc.
GOLDEN = {
    "search": "5b563e52d7aefa4d3d281a40451d588c117e39d1e1e8eb65797c051a143f336f",
    "recall_context": "99445081beec7f5e10a0c75a23a7b59cff238272f5689c1a4cef0a1dee873ec1",
}
IDENTITY_GOLDEN = {
    "search": "eb9348fb83f505d361c0ccea0292e5aeadeeb4c480895b9509f9a4c8dc32063d",
    "recall_context": "f798049b90ce7f1d9a54669699c03b6f277212c64b97e0f63e29f847ce7101f4",
}
BOUNDS = {"since": "2026-10-01T00:00:00+09:00", "until": "2026-10-02T00:00:00+09:00"}


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "cache.sqlite")


@pytest.mark.parametrize("operation", LEGACY)
def test_legacy_golden(cache, operation):
    base = LEGACY[operation]
    params = build(base, since=None, until=None)
    assert params == base
    assert params is not base
    assert cache._hash_signature(operation, "test", params) == GOLDEN[operation]
    assert cache._hash_signature(operation, "test", identity(params, "test", "hermes")) == IDENTITY_GOLDEN[operation]


@pytest.mark.parametrize("operation", LEGACY)
@pytest.mark.parametrize("field,values", [
    ("since", ["2026-10-01T01:00:00+09:00", "2026-10-01T02:00:00+09:00"]),
    ("until", ["2026-10-02T01:00:00+09:00", "2026-10-02T02:00:00+09:00"]),
    ("time_order", ["asc", "desc"]),
    ("owner_timezone", ["Asia/Tokyo", "UTC"]),
    ("reference_time", ["2026-10-03T00:00:00Z", "2026-10-04T00:00:00Z"]),
    ("temporal_axis_mode", ["created_at", "updated_at", "split_filter_created_order_updated"]),
    ("query_mode", ["topic", "activity_log"]),
])
def test_each_field_isolates(cache, operation, field, values):
    base = LEGACY[operation]
    original = dict(base)
    keys = {cache._hash_signature(operation, "test", build(base, **BOUNDS))}
    for value in values:
        filters = {**BOUNDS, field: value}
        params = build(base, **filters)
        key = cache._hash_signature(operation, "test", params)
        assert key not in keys
        keys.add(key)
        assert key == cache._hash_signature(operation, "test", build(base, **dict(reversed(list(filters.items())))))
    assert base == original


def test_hypothetical_isolates_recall_only(cache):
    base = LEGACY["recall_context"]
    keys = {cache._hash_signature("recall_context", "test", build(base, allow_hypothetical=True, hypothetical_answer=v))
            for v in [None, "first", "second"]}
    assert len(keys) == 3
    with pytest.raises(ValueError, match="recall_context"):
        build(LEGACY["search"], hypothetical_answer="no")


@pytest.mark.parametrize("operation", LEGACY)
@pytest.mark.parametrize("write_temporal", [False, True])
def test_sqlite_bidirectional_isolation(cache, operation, write_temporal):
    legacy = build(LEGACY[operation])
    temporal = build(LEGACY[operation], **BOUNDS)
    source, other = (temporal, legacy) if write_temporal else (legacy, temporal)
    cache.put_cached_recall(operation, "test", source, {"result": "own"})
    assert cache.get_cached_recall(operation, "test", source)["result"] == "own"
    assert cache.get_cached_recall(operation, "test", other) is None
    other_operation = "search" if operation == "recall_context" else "recall_context"
    assert cache.get_cached_recall(other_operation, "test", source) is None


def test_strip_without_utc_conversion():
    params = build({}, since=" 2026-10-01T00:00:00+09:00 ")
    assert params == {"since": "2026-10-01T00:00:00+09:00"}
    with pytest.raises(ValueError):
        build({}, query_mode="activity_log")
