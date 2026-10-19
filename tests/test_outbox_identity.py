"""Account-bound durable writes using real SQLite and HTTP request construction."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from test_cache_identity import make_provider
from hermes_xmemo.xmemo.cache import XMemoLocalCache, identity_fingerprint


def queue(provider, client, operation):
    provider._trigger_outbox_sync = lambda: None
    args = ({"content": "synthetic-A-only", "path": "synthetic/a"}
            if operation == "remember" else {"current_task": "synthetic-A-only"})
    result = json.loads(getattr(provider, "_handle_" + operation)(client, args))
    assert result["status"] == "queued"
    return result["idempotency_key"]


def rows(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM write_outbox")]


@pytest.mark.parametrize("operation", ["remember", "update_state"])
@pytest.mark.parametrize("identity", [{"key": "synthetic-B"}, {"agent": "agent-B"}])
def test_other_identity_cannot_replay(tmp_path, operation, identity):
    db = tmp_path / "cache.db"
    a, ca, _ = make_provider(db, offline=True)
    key = queue(a, ca, operation)
    before = rows(db)
    b, cb, calls = make_provider(db, **identity)
    b._sync_outbox()
    assert not calls
    assert rows(db) == before
    # Original identity can return and drain its own queue exactly once.
    restored, cr, own_calls = make_provider(db)
    restored._sync_outbox()
    restored._sync_outbox()
    assert len(own_calls) == 1
    request = own_calls[0]
    assert request.headers["Idempotency-Key"] == key
    assert json.loads(request.content)["idempotency_key"] == key
    assert rows(db)[0]["status"] == "sent"
    for client in (ca, cb, cr):
        client.close()


def test_same_identity_concurrent_sync_sends_once(tmp_path):
    db = tmp_path / "cache.db"
    a, ca, _ = make_provider(db, offline=True)
    queue(a, ca, "remember")
    b, cb, bcalls = make_provider(db)
    c, cc, ccalls = make_provider(db)
    # Both workers select the same pending row before either takes its lock.
    barrier = Barrier(2)
    for provider in (b, c):
        original = provider._local_cache.list_pending_writes
        def select(*args, _original=original, **kwargs):
            records = _original(*args, **kwargs)
            barrier.wait(timeout=5)
            return records
        provider._local_cache.list_pending_writes = select
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(p._sync_outbox) for p in (b, c)]
        for future in futures:
            future.result(timeout=10)
    assert len(bcalls) + len(ccalls) == 1
    assert rows(db)[0]["status"] == "sent"
    for client in (ca, cb, cc):
        client.close()


@pytest.mark.parametrize("status", ["pending", "processing", "held", "failed", "sent"])
def test_unowned_legacy_rows_are_preserved_and_never_replayed(tmp_path, status):
    db = tmp_path / "cache.db"
    a, ca, _ = make_provider(db, offline=True)
    queue(a, ca, "remember")
    # Recreate the pre-identity schema, including actual queued payload bytes.
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE write_outbox DROP COLUMN identity")
        conn.execute("UPDATE write_outbox SET status=?, locked_at=1, updated_at=1", (status,))
    before = rows(db)[0]
    if status in ("sent", "failed", "held"):
        # Terminal states retain their original retention policy; existing held
        # rows remain held without being relabelled as needing legacy recovery.
        for _ in range(2):
            upgraded = XMemoLocalCache(db)
            assert {key: rows(db)[0][key] for key in before} == before
            assert rows(db)[0]["identity"] is None
        upgraded.prune_old_records()
        assert len(rows(db)) == (1 if status == "held" else 0)
        ca.close()
        return
    for _ in range(2):
        upgraded = XMemoLocalCache(db)
        upgraded.recover_stale_locks()
        upgraded.prune_old_records()
        row = rows(db)[0]
        assert row["status"] == "held" and row["auto_replay"] == 0
        assert row["identity"] is None and row["locked_at"] is None
        assert row["last_error"] == "legacy write without owner identity; not replayed automatically"
        for field in ("id", "payload_json", "idempotency_key", "created_at"):
            assert row[field] == before[field]
        assert upgraded.get_stats()["held_writes"] == 1
    b, cb, calls = make_provider(db, key="synthetic-B")
    b._sync_outbox()
    assert not calls
    a._sync_outbox()
    assert rows(db)[0]["status"] == "held"
    ca.close(); cb.close()


def test_identity_on_disk_contains_no_raw_credential(tmp_path):
    db = tmp_path / "cache.db"
    a, ca, _ = make_provider(db, offline=True)
    queue(a, ca, "remember")
    row = rows(db)[0]
    assert row["identity"] == identity_fingerprint(ca.api_key, ca.agent_id)
    assert ca.api_key not in json.dumps(row) and ca.agent_id not in json.dumps(row)
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for file in tmp_path.iterdir():
        assert ca.api_key.encode() not in file.read_bytes()
    ca.close()
