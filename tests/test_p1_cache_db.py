"""Unit tests for XMemoLocalCache database operations.

Run with: python -m pytest tests/test_p1_cache_db.py
"""

from __future__ import annotations

import os
import sys

# Ensure src/ is on the path so we can import hermes_xmemo without pip install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

import json
import tempfile
import time
import unittest
from pathlib import Path

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
from hermes_xmemo.xmemo.cache import XMemoLocalCache


class TestXMemoLocalCache(unittest.TestCase):

    def setUp(self):
        # Create a temporary file for the database
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.temp_db.close()
        self.db_path = Path(self.temp_db.name)
        self.cache = XMemoLocalCache(self.db_path)

    def tearDown(self):
        # Remove the temporary database and any WAL files
        try:
            if self.db_path.exists():
                self.db_path.unlink()
            # Also clean up WAL/shm files if created
            wal_file = self.db_path.with_name(self.db_path.name + "-wal")
            shm_file = self.db_path.with_name(self.db_path.name + "-shm")
            if wal_file.exists():
                wal_file.unlink()
            if shm_file.exists():
                shm_file.unlink()
        except Exception:
            pass

    def test_db_initialization_and_wal(self):
        """Verify tables are created and WAL mode is enabled."""
        self.assertTrue(self.db_path.exists())
        
        # Test WAL mode
        with self.cache._get_conn() as conn:
            cursor = conn.execute("PRAGMA journal_mode;")
            mode = cursor.fetchone()[0]
            self.assertEqual(mode.lower(), "wal")

    def test_recall_cache_write_and_read(self):
        """Test put and get on recall_cache including fresh vs stale TTL."""
        operation = "search"
        query = "test query"
        params = {"limit": 5, "scope": "default"}
        response = {"results": [{"content": "hello"}], "ok": True}

        # Cache it
        self.cache.put_cached_recall(
            operation, query, params, response, fresh_ttl=1.0, max_stale_ttl=5.0
        )

        # Retrieve immediately (should be fresh)
        res = self.cache.get_cached_recall(operation, query, params)
        self.assertIsNotNone(res)
        self.assertTrue(res["ok"])
        self.assertTrue(res["_cache_metadata"]["is_fresh"])
        self.assertEqual(res["_cache_metadata"]["max_stale_until"] - res["_cache_metadata"]["created_at"], 5.0)

        # Sleep past fresh_ttl but within max_stale_ttl
        time.sleep(1.1)
        res_stale = self.cache.get_cached_recall(operation, query, params)
        self.assertIsNotNone(res_stale)
        self.assertFalse(res_stale["_cache_metadata"]["is_fresh"])

        # Sleep past max_stale_ttl (should be a cache miss)
        time.sleep(4.0)
        res_expired = self.cache.get_cached_recall(operation, query, params)
        self.assertIsNone(res_expired)

    def test_write_outbox_lifecycle(self):
        """Test enqueuing, locking, and updating outbox writes."""
        payload = {"content": "durable fact", "path": "notes.md"}
        idempotency_key = "idemp-key-123"

        # Enqueue
        record_id = self.cache.enqueue_write(
            operation="remember",
            endpoint="/v1/remember",
            method="POST",
            payload=payload,
            idempotency_key=idempotency_key,
            status="pending"
        )
        self.assertIsNotNone(record_id)

        # List pending (should include our record)
        pending = self.cache.list_pending_writes()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], record_id)
        self.assertEqual(pending[0]["idempotency_key"], idempotency_key)
        self.assertEqual(json.loads(pending[0]["payload_json"]), payload)

        # Lock for processing
        locked = self.cache.lock_write_for_processing(record_id)
        self.assertTrue(locked)

        # Try locking again (should fail because status is no longer pending)
        locked_again = self.cache.lock_write_for_processing(record_id)
        self.assertFalse(locked_again)

        # Verify it's no longer in pending writes list
        self.assertEqual(len(self.cache.list_pending_writes()), 0)

        # Mark sent
        self.cache.mark_write_sent(record_id)
        
        # Check stats
        stats = self.cache.get_stats()
        self.assertEqual(stats["sent_writes"], 1)
        self.assertEqual(stats["pending_writes"], 0)

    def test_write_outbox_failure_and_backoff(self):
        """Test outbox failures, backoff, and eventual dead-lettering."""
        payload = {"event": "timeline"}
        idempotency_key = "idemp-key-456"

        record_id = self.cache.enqueue_write(
            operation="record_event",
            endpoint="/v1/timeline/events",
            method="POST",
            payload=payload,
            idempotency_key=idempotency_key,
            status="pending"
        )

        # Simulate a transient failure on replay (retry 1)
        self.cache.mark_write_failed(record_id, error="timeout error", is_transient=True, max_retries=3)
        
        # Should be pending but backoff set, so list_pending_writes should be empty (since next_retry_at is in future)
        pending = self.cache.list_pending_writes()
        self.assertEqual(len(pending), 0)

        # Test non-transient failure (should dead-letter immediately)
        record_id_2 = self.cache.enqueue_write(
            operation="remember",
            endpoint="/v1/remember",
            method="POST",
            payload=payload,
            idempotency_key="idemp-key-789",
            status="pending"
        )
        self.cache.mark_write_failed(record_id_2, error="400 Bad Request", is_transient=False, max_retries=3)
        
        stats = self.cache.get_stats()
        self.assertEqual(stats["failed_writes"], 1)  # record_id_2 dead-lettered

        # Check record_id (transient) reaches max retries
        # First make it eligible again by forcing it to lock and fail two more times
        self.cache.lock_write_for_processing(record_id)
        self.cache.mark_write_failed(record_id, error="timeout error", is_transient=True, max_retries=3) # retry 2
        
        self.cache.lock_write_for_processing(record_id)
        self.cache.mark_write_failed(record_id, error="timeout error", is_transient=True, max_retries=3) # retry 3 (exhausted)
        
        stats_final = self.cache.get_stats()
        self.assertEqual(stats_final["failed_writes"], 2)  # both failed

    def test_stale_lock_recovery(self):
        """Test that locked records exceeding lock timeout are recovered."""
        payload = {"x": 1}
        
        # 1. Enqueue and lock a pending record (auto_replay=1)
        record_id = self.cache.enqueue_write(
            operation="remember",
            endpoint="/v1/remember",
            method="POST",
            payload=payload,
            idempotency_key="idemp-1",
            status="pending",
            auto_replay=1
        )
        self.cache.lock_write_for_processing(record_id)

        # 2. Enqueue and lock a held record (auto_replay=0)
        record_id_2 = self.cache.enqueue_write(
            operation="record_event",
            endpoint="/v1/record_event",
            method="POST",
            payload=payload,
            idempotency_key="idemp-2",
            status="pending", # must be pending to lock it
            auto_replay=0
        )
        self.cache.lock_write_for_processing(record_id_2)

        # Mock locked_at to be stale (6 minutes ago)
        stale_time = time.time() - 360
        with self.cache._get_conn() as conn:
            conn.execute(
                "UPDATE write_outbox SET locked_at = ? WHERE id IN (?, ?)",
                (stale_time, record_id, record_id_2)
            )

        # Run recovery
        recovered = self.cache.recover_stale_locks(timeout_seconds=300)
        self.assertEqual(recovered, 2)

        # Verify status: record_id is back to pending, record_id_2 is back to held
        pending = self.cache.list_pending_writes()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], record_id)

        stats = self.cache.get_stats()
        self.assertEqual(stats["held_writes"], 1)

    def test_clear_and_pruning(self):
        """Test clear_cache, clear_outbox, and record pruning."""
        # Add a cache entry and outbox entries
        self.cache.put_cached_recall("search", "q", {}, {"ok": True})
        rec_sent = self.cache.enqueue_write("remember", "/v1/remember", "POST", {}, "k1", "sent")
        rec_failed = self.cache.enqueue_write("remember", "/v1/remember", "POST", {}, "k2", "failed")

        # Verify stats
        stats = self.cache.get_stats()
        self.assertEqual(stats["recall_entries"], 1)
        self.assertEqual(stats["sent_writes"], 1)
        self.assertEqual(stats["failed_writes"], 1)

        # Pruning: set updated_at to 25 hours ago for sent and 8 days ago for failed
        long_ago_sent = time.time() - (25 * 3600)
        long_ago_failed = time.time() - (8 * 24 * 3600)
        with self.cache._get_conn() as conn:
            conn.execute("UPDATE write_outbox SET updated_at = ? WHERE id = ?", (long_ago_sent, rec_sent))
            conn.execute("UPDATE write_outbox SET updated_at = ? WHERE id = ?", (long_ago_failed, rec_failed))

        self.cache.prune_old_records()
        stats_after_prune = self.cache.get_stats()
        self.assertEqual(stats_after_prune["sent_writes"], 0)
        self.assertEqual(stats_after_prune["failed_writes"], 0)

        # Verify clear_cache
        self.cache.clear_cache()
        self.assertEqual(self.cache.get_stats()["recall_entries"], 0)


if __name__ == "__main__":
    unittest.main()
