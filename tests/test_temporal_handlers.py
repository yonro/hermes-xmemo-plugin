"""Tool boundary checks using the existing Hermes host stubs."""
import json
from unittest.mock import MagicMock

import pytest

from test_proactive_tools import XMemoMemoryProvider
from hermes_xmemo.xmemo import SEARCH_SCHEMA, RECALL_CONTEXT_SCHEMA
from hermes_xmemo.xmemo.client import XMemoClientError


FILTERS = {
    "since": "2026-10-01T00:00:00+09:00", "until": "2026-10-02T00:00:00+09:00",
    "time_order": "desc", "owner_timezone": "Asia/Tokyo",
    "reference_time": "2026-10-03T00:00:00Z", "temporal_axis_mode": "updated_at",
    "query_mode": "activity_log",
}


@pytest.fixture
def boundary():
    provider = XMemoMemoryProvider()
    provider._config = {"api_key": "test", "base_url": "https://xmemo.dev", "enable_tools": True}
    provider._local_cache = MagicMock()
    provider._local_cache.get_cached_recall.return_value = None
    client = MagicMock(api_key="test", agent_id="hermes", base_url="https://xmemo.dev")
    client.search.return_value = [{"id": "one", "content": "example"}]
    client.recall_context.return_value = {"context": "example", "items": []}
    return provider, client


@pytest.mark.parametrize("method", ["search", "recall_context"])
@pytest.mark.parametrize("invalid", [
    {"since": "naive"}, {"until": "2026-02-30T00:00:00Z"},
    {"reference_time": "2026-10-01T24:00:00Z"}, {"time_order": False},
    {"owner_timezone": " "}, {"temporal_axis_mode": "bad"}, {"query_mode": "bad"},
    {"query_mode": "activity_log"},
    {"query_mode": "activity_log", "since": FILTERS["since"]},
    {"query_mode": "activity_log", "until": FILTERS["until"]},
])
def test_invalid_has_no_side_effects(boundary, method, invalid):
    provider, client = boundary
    provider._is_breaker_open = MagicMock(return_value=True)
    provider._record_failure = MagicMock()
    provider._record_success = MagicMock()
    before = (provider._consecutive_failures, provider._breaker_open_until, provider._status)
    result = json.loads(getattr(provider, "_handle_" + method)(client, {"query": "test", **invalid}))
    assert "error" in result
    assert client.mock_calls == []
    assert provider._local_cache.mock_calls == []
    provider._is_breaker_open.assert_not_called()
    provider._record_failure.assert_not_called()
    provider._record_success.assert_not_called()
    assert before == (provider._consecutive_failures, provider._breaker_open_until, provider._status)


@pytest.mark.parametrize("method", ["search", "recall_context"])
@pytest.mark.parametrize("temporal", [False, True])
def test_exact_client_and_cache_parameters(boundary, method, temporal):
    provider, client = boundary
    filters = dict(FILTERS) if temporal else {}
    if method == "recall_context" and temporal:
        filters["hypothetical_answer"] = "possible answer"
    result = json.loads(getattr(provider, "_handle_" + method)(client, {"query": "test", **filters}))
    assert "error" not in result
    expected = {"bucket": provider._read_bucket(), "scope": provider._read_scope(), **filters}
    if method == "search":
        expected.update(memory_type="%", limit=5)
    else:
        expected.update(memory_type="auto", max_items=5, max_tokens=900, prefer_working=True)
    getattr(client, method).assert_called_once_with(query="test", **expected)
    args = provider._local_cache.put_cached_recall.call_args.args
    assert args[:2] == (method, "test")
    assert args[2] == {**expected, "base_url": "https://xmemo.dev",
                       "cache_identity_v1": "94de802a324e63414b56577248c8d63dc9d1f293fed3350e335686b91cc0ee2b"}


@pytest.mark.parametrize("method", ["search", "recall_context"])
@pytest.mark.parametrize("status,transient", [(422, False), (503, True)])
def test_failure_fallback_uses_temporal_key(boundary, method, status, transient):
    provider, client = boundary
    getattr(client, method).side_effect = XMemoClientError("test failure", status_code=status)
    cached = {"results": [{"id": "cached", "content": "cached"}], "context": "cached",
              "_cache_metadata": {"is_fresh": False, "created_at": 1}}
    provider._local_cache.get_cached_recall.return_value = cached
    result = json.loads(getattr(provider, "_handle_" + method)(client, {"query": "test", **FILTERS}))
    expected_reads = (1 if method == "search" else 0) + int(transient)
    assert provider._local_cache.get_cached_recall.call_count == expected_reads
    provider._local_cache.put_cached_recall.assert_not_called()
    if transient:
        assert result["source"] == "cache"
        assert result["stale"] is True
    else:
        assert "error" in result and "source" not in result
    for call in provider._local_cache.get_cached_recall.call_args_list:
        assert call.args[:2] == (method, "test")
        for field, value in FILTERS.items():
            assert call.args[2][field] == value


def test_search_does_not_read_hypothetical(boundary):
    provider, client = boundary
    class Args(dict):
        def get(self, key, *default):
            assert key != "hypothetical_answer"
            return super().get(key, *default)
        def __getitem__(self, key):
            assert key != "hypothetical_answer"
            return super().__getitem__(key)
    provider._handle_search(client, Args(query="test", hypothetical_answer="ignored"))
    assert "hypothetical_answer" not in client.search.call_args.kwargs


def test_recall_rejects_empty_hypothetical(boundary):
    provider, client = boundary
    result = json.loads(provider._handle_recall_context(client, {"query": "test", "hypothetical_answer": " "}))
    assert "error" in result
    assert client.mock_calls == []
    assert provider._local_cache.mock_calls == []


def test_schemas_and_public_shape(boundary):
    provider, _ = boundary
    for schema in (SEARCH_SCHEMA, RECALL_CONTEXT_SCHEMA):
        params = json.loads(json.dumps(schema))["parameters"]
        assert params["type"] == "object"
        assert params["required"] == ["query"]
        assert set(FILTERS) <= params["properties"].keys()
        for name in FILTERS:
            assert params["properties"][name]["type"] == "string"
            assert params["properties"][name]["description"]
        assert params["properties"]["query_mode"]["enum"] == ["topic", "activity_log"]
        assert params["properties"]["time_order"]["enum"] == ["asc", "desc"]
        assert params["properties"]["temporal_axis_mode"]["enum"] == [
            "created_at", "updated_at", "split_filter_created_order_updated",
        ]
    assert "hypothetical_answer" not in SEARCH_SCHEMA["parameters"]["properties"]
    assert "hypothetical_answer" in RECALL_CONTEXT_SCHEMA["parameters"]["properties"]
    schemas = provider.get_tool_schemas()
    assert SEARCH_SCHEMA in schemas and RECALL_CONTEXT_SCHEMA in schemas
    assert all({"name", "description", "parameters"} <= s.keys() for s in schemas)
    json.dumps(schemas)
