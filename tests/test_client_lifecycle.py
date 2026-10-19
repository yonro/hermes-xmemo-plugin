"""Public initialization keeps request, cache and queued-write identities aligned."""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from test_proactive_tools import XMemoMemoryProvider
from hermes_xmemo.xmemo.client import XMemoClient
from hermes_xmemo.xmemo.cache import identity_fingerprint
import hermes_xmemo.xmemo as plugin
import hermes_xmemo.xmemo.config as config
import hermes_constants


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("XMEMO_", "MEMORY_OS_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("XMEMO_KEY", "synthetic-A")
    monkeypatch.setenv("XMEMO_AGENT_ID", "agent-A")
    monkeypatch.setenv("XMEMO_URL", "https://fixture.invalid")
    monkeypatch.setenv("XMEMO_AGENT_INSTANCE_ID", "instance-A")
    monkeypatch.setenv("XMEMO_CONFIG_HOME", str(tmp_path / "cli"))
    monkeypatch.setattr(config, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    calls, clients = [], []
    mode = {"offline": False, "entered": None, "release": None}

    def transport(request):
        calls.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if mode["entered"]:
            mode["entered"].set()
            assert mode["release"].wait(5)
        if mode["offline"]:
            raise httpx.ConnectError("synthetic offline", request=request)
        marker = "data-from-" + request.headers["X-API-Key"]
        return httpx.Response(200, json={"results": [{"id": "synthetic", "content": marker}],
                                        "context_text": marker, "items": []})

    def factory(**kwargs):
        client = XMemoClient(**kwargs, transport=httpx.MockTransport(transport))
        clients.append(client)
        return client

    monkeypatch.setattr(plugin, "XMemoClient", factory)
    provider = XMemoMemoryProvider()
    provider._trigger_outbox_sync = lambda: None
    yield provider, calls, clients, mode, tmp_path
    for client in clients:
        client.close()


@pytest.mark.parametrize("operation", ["search", "recall_context", "prefetch", "remember"])
def test_reinitialize_uses_new_identity(lifecycle, monkeypatch, operation):
    p, calls, clients, mode, home = lifecycle
    p.initialize("session-A")
    old = p._get_client()
    monkeypatch.setenv("XMEMO_KEY", "synthetic-B")
    monkeypatch.setenv("XMEMO_AGENT_ID", "agent-B")
    p.initialize("session-B")
    calls.clear()
    if operation == "remember":
        mode["offline"] = True
        result = json.loads(p._handle_remember(p._get_client(), {"content": "synthetic", "path": "synthetic"}))
        assert result["status"] == "queued"
        with sqlite3.connect(home / "xmemo_cache.db") as conn:
            assert conn.execute("SELECT identity FROM write_outbox").fetchone()[0] == identity_fingerprint("synthetic-B", "agent-B")
    elif operation == "prefetch":
        p.queue_prefetch("synthetic uncached query", session_id="session-B")
        for thread in p._prefetch_threads.values():
            thread.join(5)
            assert not thread.is_alive()
    else:
        getattr(p, "_handle_" + operation)(p._get_client(), {"query": "synthetic uncached query"})
    assert calls and all(r.headers["X-API-Key"] == "synthetic-B" and
                         r.headers["X-Memory-OS-Agent-ID"] == "agent-B" for r in calls)
    if operation != "remember":
        with sqlite3.connect(home / "xmemo_cache.db") as conn:
            params, response = conn.execute("SELECT params_json,response_json FROM recall_cache").fetchone()
        assert json.loads(params)["cache_identity_v1"] == identity_fingerprint("synthetic-B", "agent-B")
        assert "data-from-synthetic-B" in response and "data-from-synthetic-A" not in response
    assert old._client.is_closed


@pytest.mark.parametrize("env,value", [("XMEMO_KEY", "synthetic-B"), ("XMEMO_AGENT_ID", "agent-B"),
    ("XMEMO_AGENT_INSTANCE_ID", "instance-B"), ("XMEMO_URL", "https://other.invalid"),
    ("XMEMO_TIMEOUT_SECONDS", "9")])
def test_client_settings_changes_refresh(lifecycle, monkeypatch, env, value):
    p, _, clients, _, _ = lifecycle
    p.initialize("A"); old = p._get_client()
    monkeypatch.setenv(env, value)
    p.initialize("B")
    assert p._get_client() is not old and old._client.is_closed
    assert len(clients) == 2


def test_unchanged_settings_reuse_client_and_shutdown_recreates(lifecycle):
    p, _, clients, _, _ = lifecycle
    p.initialize("A"); old = p._get_client()
    p.initialize("B")
    assert p._get_client() is old and len(clients) == 1
    p.shutdown(); p.initialize("C")
    assert old._client.is_closed and p._get_client() is not old
    assert len(clients) == 2


def test_concurrent_access_and_initialize_finish_with_new_client(lifecycle, monkeypatch):
    p, _, _, _, _ = lifecycle
    p.initialize("A")
    monkeypatch.setenv("XMEMO_KEY", "synthetic-B")
    with ThreadPoolExecutor(8) as pool:
        futures = [pool.submit(p._get_client) for _ in range(24)]
        futures.append(pool.submit(p.initialize, "B"))
        for future in futures:
            future.result(timeout=5)
    assert p._get_client().api_key == "synthetic-B"


def test_inflight_old_response_keeps_actual_request_identity(lifecycle, monkeypatch):
    p, _, _, mode, home = lifecycle
    p.initialize("A"); old = p._get_client()
    entered, release = Event(), Event()
    mode.update(entered=entered, release=release)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(p._handle_search, old, {"query": "old inflight query"})
        assert entered.wait(5)
        monkeypatch.setenv("XMEMO_KEY", "synthetic-B")
        p.initialize("B")
        release.set()
        pending.result(timeout=5)
    with sqlite3.connect(home / "xmemo_cache.db") as conn:
        row = conn.execute("SELECT params_json,response_json FROM recall_cache").fetchone()
    # A response already in flight may complete, but never under B's identity.
    assert json.loads(row[0])["cache_identity_v1"] == identity_fingerprint("synthetic-A", "agent-A")
    assert "data-from-synthetic-A" in row[1]


def test_retained_old_request_cannot_read_new_identity_cache(lifecycle, monkeypatch):
    p, _, _, _, _ = lifecycle
    p.initialize("A"); old = p._get_client()
    monkeypatch.setenv("XMEMO_KEY", "synthetic-B")
    p.initialize("B")
    p._handle_search(p._get_client(), {"query": "same query"})
    # A caller that captured A before initialize must not fresh-hit B's cache.
    result = p._handle_search(old, {"query": "same query"})
    assert "data-from-synthetic-B" not in result


def test_equivalent_endpoint_preserves_existing_cache_key(lifecycle, monkeypatch):
    p, _, _, _, home = lifecycle
    monkeypatch.setenv("XMEMO_URL", "https://fixture.invalid/")
    p.initialize("A")
    p._handle_search(p._get_client(), {"query": "synthetic query"})
    with sqlite3.connect(home / "xmemo_cache.db") as conn:
        params = json.loads(conn.execute("SELECT params_json FROM recall_cache").fetchone()[0])
    assert params["base_url"] == "https://fixture.invalid/"
