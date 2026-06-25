"""SQLite-based local cache and outbox manager for the XMemo Hermes plugin.

Provides thread-safe read caching and outbox write queuing with status locking,
exponential backoff, dead-lettering, WAL mode, and stale lock recovery.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class XMemoLocalCache:
    """Manages the local SQLite database for recall caching and write queuing."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            try:
                from hermes_constants import get_hermes_home
                db_path = get_hermes_home() / "xmemo_cache.db"
            except Exception:
                # Fallback for testing or standalone execution
                db_path = Path("xmemo_cache.db")
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Obtain a new connection, enabling WAL mode and setting a busy timeout."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception as exc:
            logger.debug("Failed to enable WAL mode: %s", exc)
        return conn

    def _init_db(self) -> None:
        """Initialize SQLite tables and indexes."""
        if self.db_path.parent:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_conn() as conn:
            # Table for caching GET queries and recall context requests
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recall_cache (
                  id TEXT PRIMARY KEY,
                  operation TEXT NOT NULL,
                  query TEXT NOT NULL,
                  params_json TEXT NOT NULL,
                  response_json TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  fresh_until REAL NOT NULL,
                  max_stale_until REAL NOT NULL,
                  hit_count INTEGER NOT NULL DEFAULT 0
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_recall_cache_lookup
                ON recall_cache(operation, id, max_stale_until);
            """)

            # Table for queuing failed writes
            conn.execute("""
                CREATE TABLE IF NOT EXISTS write_outbox (
                  id TEXT PRIMARY KEY,
                  operation TEXT NOT NULL,
                  endpoint TEXT NOT NULL,
                  method TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  retry_count INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT,
                  locked_at REAL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  next_retry_at REAL,
                  auto_replay INTEGER NOT NULL DEFAULT 1
                );
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_write_outbox_idempotency
                ON write_outbox(idempotency_key);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_write_outbox_pending
                ON write_outbox(status, next_retry_at, created_at);
            """)

            # Check if auto_replay column exists in write_outbox, if not, migrate it
            try:
                cursor = conn.execute("PRAGMA table_info(write_outbox);")
                columns = [row["name"] for row in cursor.fetchall()]
                if columns and "auto_replay" not in columns:
                    conn.execute("ALTER TABLE write_outbox ADD COLUMN auto_replay INTEGER NOT NULL DEFAULT 1;")
            except Exception as exc:
                logger.debug("Failed to migrate auto_replay column: %s", exc)

    def _hash_signature(self, operation: str, query: str, params: Dict[str, Any]) -> str:
        """Generate a stable SHA256 signature of the request parameters."""
        sorted_params = json.dumps(params, sort_keys=True)
        sig = f"{operation}:{query}:{sorted_params}"
        return hashlib.sha256(sig.encode("utf-8")).hexdigest()

    def recover_stale_locks(self, timeout_seconds: float = 300.0) -> int:
        """Release processing records stuck for too long (e.g. after a crash)."""
        stale_time = time.time() - timeout_seconds
        recovered_count = 0
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT id, auto_replay FROM write_outbox WHERE status = 'processing' AND locked_at < ?",
                (stale_time,)
            )
            rows = cursor.fetchall()
            if not rows:
                return 0
            for row in rows:
                record_id = row["id"]
                auto_replay = row["auto_replay"]
                target_status = "pending" if auto_replay == 1 else "held"
                conn.execute(
                    "UPDATE write_outbox SET status = ?, locked_at = NULL, updated_at = ? WHERE id = ?",
                    (target_status, time.time(), record_id)
                )
                recovered_count += 1
        return recovered_count

    # -------------------------------------------------------------------------
    # Read Cache API (recall_cache)
    # -------------------------------------------------------------------------

    def get_cached_recall(
        self, operation: str, query: str, params: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a cached query response if it is within max_stale_until."""
        cache_id = self._hash_signature(operation, query, params)
        now = time.time()
        with self._get_conn() as conn:
            cursor = conn.execute(
                """SELECT response_json, created_at, fresh_until, max_stale_until 
                   FROM recall_cache 
                   WHERE operation = ? AND id = ? AND max_stale_until >= ?""",
                (operation, cache_id, now)
            )
            row = cursor.fetchone()
            if row:
                conn.execute(
                    "UPDATE recall_cache SET hit_count = hit_count + 1 WHERE id = ?",
                    (cache_id,)
                )
                try:
                    res = json.loads(row["response_json"])
                    is_fresh = now <= row["fresh_until"]
                    res["_cache_metadata"] = {
                        "created_at": row["created_at"],
                        "fresh_until": row["fresh_until"],
                        "max_stale_until": row["max_stale_until"],
                        "is_fresh": is_fresh,
                    }
                    return res
                except Exception as exc:
                    logger.error("Failed to parse cached response JSON: %s", exc)
        return None

    def put_cached_recall(
        self,
        operation: str,
        query: str,
        params: Dict[str, Any],
        response: Dict[str, Any],
        fresh_ttl: float = 300.0,
        max_stale_ttl: float = 86400.0,
    ) -> None:
        """Cache a successful query response."""
        cache_id = self._hash_signature(operation, query, params)
        now = time.time()
        fresh_until = now + fresh_ttl
        max_stale_until = now + max_stale_ttl
        params_json = json.dumps(params, sort_keys=True)
        # Avoid caching the metadata itself if it's already in the dictionary
        clean_resp = {k: v for k, v in response.items() if k != "_cache_metadata"}
        response_json = json.dumps(clean_resp)

        with self._get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO recall_cache 
                   (id, operation, query, params_json, response_json, created_at, fresh_until, max_stale_until, hit_count) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT hit_count FROM recall_cache WHERE id = ?), 0))""",
                (cache_id, operation, query, params_json, response_json, now, fresh_until, max_stale_until, cache_id)
            )

    # -------------------------------------------------------------------------
    # Write Outbox API (write_outbox)
    # -------------------------------------------------------------------------

    def enqueue_write(
        self,
        operation: str,
        endpoint: str,
        method: str,
        payload: Dict[str, Any],
        idempotency_key: str,
        status: str = "pending",
        auto_replay: int = 1,
    ) -> str:
        """Enqueue a write request in the outbox database."""
        import uuid
        record_id = str(uuid.uuid4())
        now = time.time()
        payload_json = json.dumps(payload)
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO write_outbox 
                   (id, operation, endpoint, method, payload_json, idempotency_key, status, retry_count, created_at, updated_at, auto_replay) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (record_id, operation, endpoint, method, payload_json, idempotency_key, status, now, now, auto_replay)
            )
        return record_id

    def list_pending_writes(self) -> List[Dict[str, Any]]:
        """List pending writes that are eligible for a retry attempt."""
        now = time.time()
        with self._get_conn() as conn:
            cursor = conn.execute(
                """SELECT id, operation, endpoint, method, payload_json, idempotency_key, retry_count 
                   FROM write_outbox 
                   WHERE status = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= ?) 
                   ORDER BY created_at ASC""",
                (now,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def lock_write_for_processing(self, record_id: str) -> bool:
        """Transition a record's status to 'processing' to prevent concurrent replays."""
        now = time.time()
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE write_outbox SET status = 'processing', locked_at = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                (now, now, record_id)
            )
            return cursor.rowcount > 0

    def mark_write_sent(self, record_id: str) -> None:
        """Mark a record as successfully synchronized."""
        now = time.time()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE write_outbox SET status = 'sent', locked_at = NULL, updated_at = ? WHERE id = ?",
                (now, record_id)
            )

    def mark_write_failed(
        self, record_id: str, error: str, is_transient: bool, max_retries: int = 5
    ) -> None:
        """Increment retry count or dead-letter a failed outbox record."""
        now = time.time()
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT retry_count FROM write_outbox WHERE id = ?", (record_id,)
            )
            row = cursor.fetchone()
            if not row:
                return

            retry_count = row["retry_count"] + 1
            if not is_transient or retry_count >= max_retries:
                # Dead-letter (status = 'failed')
                conn.execute(
                    """UPDATE write_outbox 
                       SET status = 'failed', retry_count = ?, last_error = ?, locked_at = NULL, updated_at = ? 
                       WHERE id = ?""",
                    (retry_count, error, now, record_id)
                )
            else:
                # Calculate backoff: (2^retry_count) * 10 seconds, capped at 1 hour
                backoff_delay = min((2 ** retry_count) * 10, 3600)
                next_retry_at = now + backoff_delay
                conn.execute(
                    """UPDATE write_outbox 
                       SET status = 'pending', retry_count = ?, last_error = ?, locked_at = NULL, next_retry_at = ?, updated_at = ? 
                       WHERE id = ?""",
                    (retry_count, error, next_retry_at, now, record_id)
                )

    # -------------------------------------------------------------------------
    # Pruning & Diagnostics
    # -------------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Safely clear all query cache entries."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM recall_cache;")

    def clear_outbox(self) -> None:
        """Clear all outbox records (WARNING: unsynced writes will be lost)."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM write_outbox;")

    def prune_old_records(self) -> None:
        """Prune sent outbox records (> 24h) and cap failed records (delete > 7 days or > 100 records)."""
        now = time.time()
        one_day_ago = now - 86400
        seven_days_ago = now - (7 * 86400)
        with self._get_conn() as conn:
            # Prune successful syncs older than 24h
            conn.execute("DELETE FROM write_outbox WHERE status = 'sent' AND updated_at < ?", (one_day_ago,))
            # Prune failed syncs older than 7 days
            conn.execute("DELETE FROM write_outbox WHERE status = 'failed' AND updated_at < ?", (seven_days_ago,))
            # Cap dead-letter queue at 100 entries
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM write_outbox WHERE status = 'failed'")
            count = cursor.fetchone()["cnt"]
            if count > 100:
                excess = count - 100
                conn.execute(
                    """DELETE FROM write_outbox 
                       WHERE id IN (
                         SELECT id FROM write_outbox 
                         WHERE status = 'failed' 
                         ORDER BY updated_at ASC 
                         LIMIT ?
                       )""",
                    (excess,)
                )

    def get_stats(self) -> Dict[str, Any]:
        """Collect database statistics for diagnostics."""
        stats = {
            "recall_entries": 0,
            "pending_writes": 0,
            "held_writes": 0,
            "failed_writes": 0,
            "sent_writes": 0,
        }
        try:
            with self._get_conn() as conn:
                cursor = conn.execute("SELECT COUNT(*) as cnt FROM recall_cache")
                stats["recall_entries"] = cursor.fetchone()["cnt"]

                cursor = conn.execute(
                    "SELECT status, COUNT(*) as cnt FROM write_outbox GROUP BY status"
                )
                for row in cursor.fetchall():
                    status = row["status"]
                    cnt = row["cnt"]
                    if status == "pending":
                        stats["pending_writes"] = cnt
                    elif status == "held":
                        stats["held_writes"] = cnt
                    elif status == "failed":
                        stats["failed_writes"] = cnt
                    elif status == "sent":
                        stats["sent_writes"] = cnt
        except Exception as exc:
            logger.error("Failed to collect database stats: %s", exc)
        return stats
