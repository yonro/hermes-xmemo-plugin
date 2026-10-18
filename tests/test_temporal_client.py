"""Wire-level retrieval checks without a Hermes host or external service."""
import importlib.util
import json
from pathlib import Path
import sys
import types

import httpx
import pytest


# Load the real client in an isolated package to avoid importing the host plugin.
package = types.ModuleType("_temporal_wire_tests")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "src/hermes_xmemo/xmemo")]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    package.__name__ + ".client", Path(package.__path__[0]) / "client.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

FIELDS = {
    "since": "2026-10-01T00:00:00+09:00",
    "until": "2026-10-02T00:00:00+09:00",
    "time_order": "asc", "owner_timezone": "Asia/Tokyo",
    "reference_time": "2026-10-03T00:00:00Z",
    "temporal_axis_mode": "split_filter_created_order_updated",
    "query_mode": "activity_log",
}


@pytest.fixture
def wire():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [{"id": "sample"}]})
    client = module.XMemoClient("https://example.invalid", "test-key",
                                transport=httpx.MockTransport(handle))
    yield client, requests
    client.close()


@pytest.mark.parametrize("method", ["search", "recall_context"])
def test_all_fields_exact_wire(wire, method):
    client, requests = wire
    fields = dict(FIELDS)
    if method == "recall_context":
        fields["hypothetical_answer"] = "possible answer"
    getattr(client, method)("test", **fields)
    assert len(requests) == 1
    request = requests[0]
    if method == "search":
        assert request.method == "GET"
        assert request.url.path == "/v1/memories/search"
        assert dict(request.url.params) == {
            "query": "test", "bucket": "%", "memory_type": "%", "limit": "10",
            "explain": "false", "prefer_working": "false", **fields,
        }
    else:
        assert request.method == "POST"
        assert request.url.path == "/v1/recall/context"
        assert json.loads(request.content) == {
            "query": "test", "bucket": "%", "max_items": 5, "max_tokens": 900,
            "memory_type": "auto", "prefer_working": True, **fields,
        }


@pytest.mark.parametrize("method", ["search", "recall_context"])
@pytest.mark.parametrize("invalid", [
    {"since": "2026-10-01"}, {"until": "2026-02-30T00:00:00Z"},
    {"reference_time": "2026-10-01T24:00:00Z"}, {"time_order": "bad"},
    {"owner_timezone": " "}, {"temporal_axis_mode": "bad"}, {"query_mode": "bad"},
    {"query_mode": "activity_log"},
    {"query_mode": "activity_log", "since": FIELDS["since"]},
    {"query_mode": "activity_log", "until": FIELDS["until"]},
    {"since": FIELDS["since"], "until": "2026-09-30T15:00:00Z"},
])
def test_invalid_is_zero_requests(wire, method, invalid):
    client, requests = wire
    with pytest.raises(ValueError):
        getattr(client, method)("test", **invalid)
    assert requests == []


def test_search_hypothetical_not_supported(wire):
    client, requests = wire
    with pytest.raises(TypeError):
        client.search("test", hypothetical_answer="no")
    with pytest.raises(ValueError):
        client.recall_context("test", hypothetical_answer=" ")
    assert requests == []


@pytest.mark.parametrize("path,suffix", [(None, b""), ("%", b""), ("work/*", b"&path=work%2F%2A")])
def test_legacy_search_bytes(wire, path, suffix):
    client, requests = wire
    client.search("hello world", bucket="work", scope="test", memory_type="semantic",
                  limit=7, explain=True, prefer_working=True, path=path)
    assert requests[0].url.query == (
        b"query=hello+world&bucket=work&scope=test&memory_type=semantic&limit=7"
        b"&explain=true&prefer_working=true" + suffix
    )
    assert requests[0].content == b""


def test_legacy_recall_bytes(wire):
    client, requests = wire
    client.recall_context("hello", bucket="work", scope="test", max_items=3,
                          max_tokens=100, memory_type="semantic", prefer_working=False)
    assert requests[0].content == (
        b'{"query":"hello","bucket":"work","scope":"test","max_items":3,'
        b'"max_tokens":100,"memory_type":"semantic","prefer_working":false}'
    )


def test_list_memories_legacy(wire):
    client, requests = wire
    assert client.list_memories(path=" work ", limit=2) == [{"id": "sample"}]
    assert requests[0].url.query == (
        b"query=%2A&bucket=%25&memory_type=%25&limit=2&explain=false"
        b"&prefer_working=false&path=work%25"
    )


@pytest.mark.parametrize("method", ["search", "recall_context"])
def test_server_422_not_retried(method):
    requests = []
    def reject(request):
        requests.append(request)
        return httpx.Response(422, json={"detail": "invalid timezone"})
    client = module.XMemoClient("https://example.invalid", "test-key",
                                transport=httpx.MockTransport(reject))
    try:
        with pytest.raises(module.XMemoClientError) as error:
            getattr(client, method)("test", owner_timezone="server-checks-this")
        assert error.value.status_code == 422
        assert error.value.is_transient is False
        assert len(requests) == 1
    finally:
        client.close()
