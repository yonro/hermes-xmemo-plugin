"""Comprehensive P1 integration tests for XMemo local cache and outbox.

Run with: python -m pytest tests/test_p1_cache.py -v
"""

from __future__ import annotations

import os
import sys

# Ensure src/ is on the path so we can import hermes_xmemo without pip install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

import datetime
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal stubs so we can import the plugin without Hermes installed
# ---------------------------------------------------------------------------

# Stub agent.memory_provider
mp_mod = type(sys)("agent.memory_provider")

class _MemoryProviderBase:
    """Minimal stub for the Hermes MemoryProvider ABC."""
    pass

mp_mod.MemoryProvider = _MemoryProviderBase  # type: ignore[attr-defined]
sys.modules.setdefault("agent", type(sys)("agent"))
sys.modules.setdefault("agent.memory_provider", mp_mod)

# Stub tools.registry
tr_mod = type(sys)("tools.registry")
tr_mod.tool_error = lambda msg: json.dumps({"error": msg})  # type: ignore[attr-defined]
sys.modules.setdefault("tools", type(sys)("tools"))
sys.modules.setdefault("tools.registry", tr_mod)

# Stub hermes_constants
hc_mod = type(sys)("hermes_constants")
hc_mod.get_hermes_home = lambda: Path("/fake/hermes/home")
sys.modules.setdefault("hermes_constants", hc_mod)

# Now safe to import
import httpx

from hermes_xmemo.xmemo.client import XMemoClient, XMemoClientError
from hermes_xmemo.xmemo.cache import XMemoLocalCache
from hermes_xmemo.xmemo import XMemoMemoryProvider


# ---------------------------------------------------------------------------
# Helper Transport
# ---------------------------------------------------------------------------

class _MockTransport(httpx.BaseTransport):
    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self._calls: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._calls.append(request)
        if not self._responses:
            raise RuntimeError("MockTransport exhausted")
        resp = self._responses.pop(0)
        resp.request = request
        return resp

    @property
    def call_count(self) -> int:
        return len(self._calls)


def _make_response(status: int, body: dict | str = "") -> httpx.Response:
    if isinstance(body, dict):
        content = json.dumps(body).encode()
    elif body:
        content = body.encode()
    else:
        content = b""
    return httpx.Response(status_code=status, content=content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestErrorClassification(unittest.TestCase):
    """Verify XMemoClientError.is_transient rules."""

    def test_transient_failures(self):
        # 5xx errors are transient
        err_503 = XMemoClientError("Service Unavailable", status_code=503)
        self.assertTrue(err_503.is_transient)

        # Connection / Timeout exceptions raised by client are transient
        err_conn = XMemoClientError("connection timed out", is_transient=True)
        self.assertTrue(err_conn.is_transient)

    def test_non_transient_failures(self):
        # 4xx errors are NOT transient
        err_400 = XMemoClientError("Bad Request", status_code=400)
        self.assertFalse(err_400.is_transient)

        err_422 = XMemoClientError("Validation Error", status_code=422)
        self.assertFalse(err_422.is_transient)

        # Other programming or parse errors are NOT transient
        err_prog = XMemoClientError("JSON parse error", is_transient=False)
        self.assertFalse(err_prog.is_transient)


class TestIdempotencyHeaders(unittest.TestCase):
    """Verify Idempotency-Key headers and body copying."""

    def test_headers_and_body_sent_correctly(self):
        transport = _MockTransport([_make_response(200, {"ok": True})])
        client = XMemoClient(
            base_url="http://localhost",
            api_key="test",
            transport=transport,
        )
        
        orig_body = {"content": "durable fact", "path": "notes.md"}
        client.remember(
            content="durable fact",
            path="notes.md",
            idempotency_key="unique-idemp-key-1",
        )

        self.assertEqual(transport.call_count, 1)
        req = transport._calls[0]
        
        # Verify headers (both standard and alternative compatibility)
        self.assertEqual(req.headers.get("Idempotency-Key"), "unique-idemp-key-1")
        self.assertEqual(req.headers.get("X-Idempotency-Key"), "unique-idemp-key-1")

        # Verify body contains idempotency_key
        body_sent = json.loads(req.content.decode())
        self.assertEqual(body_sent["idempotency_key"], "unique-idemp-key-1")

        # Verify original body was NOT mutated (non-destructive copying)
        self.assertNotIn("idempotency_key", orig_body)


class TestP1Integration(unittest.TestCase):

    def setUp(self):
        # Create temp DB
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.temp_db.close()
        self.db_path = Path(self.temp_db.name)
        
        self.provider = XMemoMemoryProvider()
        self.provider._config = {
            "api_key": "test-key",
            "scope": "hermes/default",
            "bucket": "work",
            "enable_non_idempotent_replay": False,  # Conservative default
        }
        self.provider._local_cache = XMemoLocalCache(self.db_path)
        self.provider._status = "online"

        # Patch _trigger_outbox_sync to prevent async background threads in tests
        self.sync_patcher = patch.object(self.provider, "_trigger_outbox_sync")
        self.mock_trigger_sync = self.sync_patcher.start()

    def tearDown(self):
        self.sync_patcher.stop()
        # Clean up database
        try:
            if self.db_path.exists():
                self.db_path.unlink()
            wal_file = self.db_path.with_name(self.db_path.name + "-wal")
            shm_file = self.db_path.with_name(self.db_path.name + "-shm")
            if wal_file.exists():
                wal_file.unlink()
            if shm_file.exists():
                shm_file.unlink()
        except Exception:
            pass

    @patch("hermes_xmemo.xmemo.client.XMemoClient.search")
    def test_read_fallback_fresh_vs_stale(self, mock_search):
        # Seed cache
        query = "find hermes info"
        params = {
            "bucket": "%",
            "scope": None,
            "memory_type": "%",
            "limit": 5,
            "base_url": "https://xmemo.dev",
        }
        response_data = [{"content": "hermes cache content"}]
        
        # 1. Success write to cache
        mock_search.return_value = response_data
        client = self.provider._get_client()
        res_online_str = self.provider._handle_search(client, {"query": query, "limit": 5})
        self.assertIn("hermes cache content", res_online_str)
        
        # Verify record exists in cache
        cached = self.provider._local_cache.get_cached_recall("search", query, params)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["results"], response_data)

        # 2. Transient fail -> Fallback to cache
        mock_search.side_effect = XMemoClientError("503 Service Unavailable", status_code=503)
        res_fallback_str = self.provider._handle_search(client, {"query": query, "limit": 5})
        res_fallback = json.loads(res_fallback_str)
        
        self.assertEqual(res_fallback["status"], "degraded")
        self.assertEqual(res_fallback["source"], "cache")
        self.assertTrue(res_fallback["stale"])
        self.assertEqual(res_fallback["results"], response_data)
        self.assertIn("XMemo is temporarily offline", res_fallback["note"])

        # 3. Max stale timeout exceeded (> 24h)
        # Mock max_stale_until to be in the past (e.g. 25 hours ago)
        with self.provider._local_cache._get_conn() as conn:
            conn.execute("UPDATE recall_cache SET max_stale_until = ?", (time.time() - 3600,))
        
        res_expired_str = self.provider._handle_search(client, {"query": query, "limit": 5})
        res_expired = json.loads(res_expired_str)
        
        self.assertEqual(res_expired["status"], "degraded")
        self.assertIn("no cached copy is available", res_expired["error"])
        self.assertNotIn("results", res_expired)

    @patch("hermes_xmemo.xmemo.client.XMemoClient.remember")
    @patch("hermes_xmemo.xmemo.client.XMemoClient.record_event")
    def test_write_outbox_replay_policies(self, mock_record_event, mock_remember):
        """Verify remember enters pending while record_event enters held by default."""
        client = self.provider._get_client()
        
        # Mock transient failures
        mock_remember.side_effect = XMemoClientError("500 Server Error", status_code=500)
        mock_record_event.side_effect = XMemoClientError("500 Server Error", status_code=500)

        # 1. remember (idempotent write) -> should enter as pending
        res_rem_str = self.provider._handle_remember(client, {"content": "memory", "path": "file.md"})
        res_rem = json.loads(res_rem_str)
        self.assertEqual(res_rem["status"], "queued")
        self.assertEqual(res_rem["outbox_status"], "pending")

        # 2. record_event (non-idempotent write) -> should enter as held under conservative default
        res_ev_str = self.provider._handle_record_event(client, {"content": "timeline event"})
        res_ev = json.loads(res_ev_str)
        self.assertEqual(res_ev["status"], "queued")
        self.assertEqual(res_ev["outbox_status"], "held")

        # Check outbox counts in database
        stats = self.provider._local_cache.get_stats()
        self.assertEqual(stats["pending_writes"], 1)
        self.assertEqual(stats["held_writes"], 1)

    @patch("hermes_xmemo.xmemo.client.XMemoClient.remember")
    def test_success_response_keeps_legacy_memory_id(self, mock_remember):
        client = self.provider._get_client()
        mock_remember.return_value = {"id": "mem-123"}

        result = json.loads(self.provider._handle_remember(
            client,
            {"content": "memory", "path": "file.md"}
        ))

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["id"], "mem-123")
        self.assertEqual(result["memory_id"], "mem-123")

    @patch("hermes_xmemo.xmemo.client.XMemoClient.update_state")
    def test_success_response_keeps_legacy_state_id(self, mock_update_state):
        client = self.provider._get_client()
        mock_update_state.return_value = {"id": "state-456"}

        result = json.loads(self.provider._handle_update_state(
            client,
            {"current_task": "test", "next_action": "next"}
        ))

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["id"], "state-456")
        self.assertEqual(result["state_id"], "state-456")

    @patch("hermes_xmemo.xmemo.client.XMemoClient._request")
    def test_outbox_replay_cycle_and_dead_lettering(self, mock_request):
        """Test background sync draining, retries backoff, and dead-lettering."""
        # Seed 1 pending write
        payload = {"content": "test payload"}
        idempotency_key = "idemp-test-1"
        self.provider._local_cache.enqueue_write(
            operation="remember",
            endpoint="/v1/remember",
            method="POST",
            payload=payload,
            idempotency_key=idempotency_key,
            status="pending"
        )

        # 1. Transient failure on replay (retry 1)
        mock_request.side_effect = XMemoClientError("503 Server Error", status_code=503)
        self.provider._sync_outbox()
        
        # Check database: record still pending, retry_count incremented to 1, next_retry_at set in future
        with self.provider._local_cache._get_conn() as conn:
            row = conn.execute("SELECT status, retry_count, next_retry_at FROM write_outbox").fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["retry_count"], 1)
            self.assertGreater(row["next_retry_at"], time.time())

        # 2. Non-transient failure on replay (like 400 Bad Request) -> immediately dead-letter (failed)
        # Seed another pending write
        record_id_2 = self.provider._local_cache.enqueue_write(
            operation="remember",
            endpoint="/v1/remember",
            method="POST",
            payload=payload,
            idempotency_key="idemp-test-2",
            status="pending"
        )
        mock_request.side_effect = XMemoClientError("400 Bad Request", status_code=400)
        self.provider._sync_outbox()
        
        # Verify it has transitioned to failed immediately
        with self.provider._local_cache._get_conn() as conn:
            row = conn.execute("SELECT status, retry_count FROM write_outbox WHERE id = ?", (record_id_2,)).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["retry_count"], 1)

    @patch("hermes_xmemo.xmemo.client.XMemoClient.health")
    def test_stale_lock_recovery_on_initialize(self, mock_health):
        """Verify that stale processing locks are recovered during provider initialization."""
        mock_health.side_effect = Exception("conn error") # skip health success
        
        # Seed a locked record (processing) with locked_at in the past (6 mins ago)
        record_id = self.provider._local_cache.enqueue_write(
            operation="remember",
            endpoint="/v1/remember",
            method="POST",
            payload={},
            idempotency_key="idemp-lock-1",
            status="pending"
        )
        self.provider._local_cache.lock_write_for_processing(record_id)
        
        with self.provider._local_cache._get_conn() as conn:
            conn.execute("UPDATE write_outbox SET locked_at = ?", (time.time() - 360,))

        # Trigger initialize
        self.provider.initialize("test-session")
        
        # Verify status is back to pending
        stats = self.provider._local_cache.get_stats()
        self.assertEqual(stats["pending_writes"], 1)

    def test_clear_outbox_warning(self):
        """Verify clear_cache and clear_outbox isolate correctly."""
        self.provider._local_cache.put_cached_recall("search", "q", {}, {"ok": True})
        self.provider._local_cache.enqueue_write("remember", "/v1/remember", "POST", {}, "k1")
        
        # Clear cache only
        self.provider._local_cache.clear_cache()
        stats = self.provider._local_cache.get_stats()
        self.assertEqual(stats["recall_entries"], 0)
        self.assertEqual(stats["pending_writes"], 1) # outbox remains
        
        # Clear outbox
        self.provider._local_cache.clear_outbox()
        stats_final = self.provider._local_cache.get_stats()
        self.assertEqual(stats_final["pending_writes"], 0)


if __name__ == "__main__":
    unittest.main()
