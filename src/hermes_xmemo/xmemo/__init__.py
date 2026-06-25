"""XMemo memory provider plugin for Hermes Agent.

Provides user-owned cloud memory via XMemo's REST API: orchestrated recall,
semantic search, durable fact storage, working state, reminders, and session
snapshots.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .client import XMemoClient
from .config import load_config, save_config

logger = logging.getLogger(__name__)

# Circuit breaker: pause API calls after consecutive failures.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECONDS = 120

# Max time prefetch() may wait for an in-flight background recall. Keep this
# short because prefetch() runs on the API-call critical path.
_PREFETCH_JOIN_TIMEOUT_SECONDS = 0.25


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "xmemo_search",
    "description": (
        "Search all visible user-owned XMemo memories by natural-language query, "
        "including memories written by other agents connected to the same XMemo account. "
        "Returns relevant facts ranked by semantic similarity. "
        "Results may include agent_boundary/provenance metadata such as self or other_agent. "
        "Use this when the user asks about saved information or when prior "
        "context could change the answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 5, max 20).",
            },
            "memory_type": {
                "type": "string",
                "description": "Optional memory type filter (e.g. semantic, episodic, working).",
            },
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "xmemo_remember",
    "description": (
        "Save a durable fact to XMemo. Use for explicit preferences, decisions, "
        "conventions, architecture notes, action items, or bug-fix context that "
        "should survive across sessions. Skip transient chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact to remember. One clear concept per call.",
            },
            "path": {
                "type": "string",
                "description": "Logical path or category, e.g. 'notes/decisions' or 'hermes/preferences'.",
            },
            "memory_type": {
                "type": "string",
                "description": "Memory type: semantic, episodic, procedural, working, identity (default semantic).",
            },
            "importance": {
                "type": "number",
                "description": "Importance from 0.0 to 1.0 (default 0.7).",
            },
        },
        "required": ["content", "path"],
    },
}

UPDATE_STATE_SCHEMA = {
    "name": "xmemo_update_state",
    "description": (
        "Save the current working state to XMemo with TTL. Use for active task, "
        "next action, or blocker during long-running work so future sessions can resume."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "current_task": {
                "type": "string",
                "description": "Short description of the active task.",
            },
            "next_action": {
                "type": "string",
                "description": "The very next step the agent should take.",
            },
            "blocked_reason": {
                "type": "string",
                "description": "Why work is blocked, if applicable.",
            },
            "ttl_seconds": {
                "type": "integer",
                "description": "Time-to-live in seconds (default 86400 = 1 day).",
            },
        },
        "required": [],
    },
}

RECALL_CONTEXT_SCHEMA = {
    "name": "xmemo_recall_context",
    "description": (
        "Build a bounded, ranked context pack from all visible user-owned XMemo memories, "
        "including memories written by other agents connected to the same XMemo account. "
        "Use when you need a focused memory summary rather than raw search results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to recall context for.",
            },
            "max_items": {
                "type": "integer",
                "description": "Max memory items to include (default 5, max 20).",
            },
            "memory_type": {
                "type": "string",
                "description": "Optional memory type filter (semantic, episodic, working, identity, procedural).",
            },
        },
        "required": ["query"],
    },
}

RECORD_EVENT_SCHEMA = {
    "name": "xmemo_record_event",
    "description": (
        "Record a significant session event, milestone, decision, or handoff note "
        "to the XMemo timeline. Use for durable audit-style notes, not transient chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The event note to save.",
            },
            "event_type": {
                "type": "string",
                "description": "Event type: event, milestone, decision, handoff (default event).",
            },
        },
        "required": ["content"],
    },
}

CREATE_REMINDER_SCHEMA = {
    "name": "xmemo_create_reminder",
    "description": (
        "Create a TODO or action item in XMemo to revisit later. "
        "Use when the user asks you to follow up, save a task, or remind them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "What to remember to do.",
            },
            "due_at": {
                "type": "string",
                "description": "Optional due time as ISO 8601 string.",
            },
        },
        "required": ["content"],
    },
}

LIST_REMINDERS_SCHEMA = {
    "name": "xmemo_list_reminders",
    "description": (
        "List XMemo TODO/action items. Use when the user asks what tasks, follow-ups, "
        "or reminders are pending or done."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "item_status": {
                "type": "string",
                "description": "Filter by status: open or completed (default open).",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 20).",
            },
        },
        "required": [],
    },
}

COMPLETE_REMINDER_SCHEMA = {
    "name": "xmemo_complete_reminder",
    "description": (
        "Mark a XMemo TODO/action item as completed. "
        "Use when the user says a saved task is done, resolved, or no longer needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todo_id": {
                "type": "string",
                "description": "The exact TODO item ID from xmemo_list_reminders.",
            },
            "note": {
                "type": "string",
                "description": "Optional completion note.",
            },
        },
        "required": ["todo_id"],
    },
}

MARK_USED_SCHEMA = {
    "name": "xmemo_mark_used",
    "description": (
        "Tell XMemo that a recalled memory influenced the current answer. "
        "Call this after using a specific memory returned by xmemo_search or "
        "xmemo_recall_context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "Exact memory ID returned by xmemo_search.",
            },
            "context": {
                "type": "string",
                "description": "Optional short note on how the memory was used.",
            },
        },
        "required": ["memory_id"],
    },
}

FORGET_SCHEMA = {
    "name": "xmemo_forget",
    "description": (
        "Delete a memory from XMemo. Use only when the user explicitly asks to "
        "forget or remove a specific saved fact. Requires an exact memory ID."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "Exact memory ID from xmemo_search.",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason for deletion.",
            },
        },
        "required": ["memory_id"],
    },
}

# Schemas exposed by default. Workflow/destructive tools are opt-in via config.
_CORE_TOOL_SCHEMAS = [
    RECALL_CONTEXT_SCHEMA,
    SEARCH_SCHEMA,
    REMEMBER_SCHEMA,
    UPDATE_STATE_SCHEMA,
]

_WORKFLOW_TOOL_SCHEMAS = [
    RECORD_EVENT_SCHEMA,
    CREATE_REMINDER_SCHEMA,
    LIST_REMINDERS_SCHEMA,
    COMPLETE_REMINDER_SCHEMA,
]

_FEEDBACK_TOOL_SCHEMAS = [
    MARK_USED_SCHEMA,
]

_DESTRUCTIVE_TOOL_SCHEMAS = [
    FORGET_SCHEMA,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_trivial_prompt(text: str) -> bool:
    """Skip recall for acknowledgements, slash commands, and empty input."""
    if not text or not text.strip():
        return True
    cleaned = text.strip().lower()
    if cleaned.startswith("/"):
        return True
    return bool(
        re.match(
            r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
            r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)$",
            cleaned,
            re.IGNORECASE,
        )
    )


def _format_search_results(results: List[Dict[str, Any]]) -> str:
    """Format search results into a concise text block."""
    if not results:
        return ""
    lines = []
    for i, item in enumerate(results, 1):
        content = item.get("content", "")
        if not content:
            continue
        memory_type = item.get("memory_type", "semantic")
        path = item.get("path", "")
        score = item.get("similarity") or item.get("score")
        header = f"{i}. [{memory_type}]"
        boundary = _agent_boundary_label(item)
        if boundary:
            header += f" [{boundary}]"
        if path:
            header += f" {path}"
        if score is not None:
            header += f" (sim {score:.3f})"
        lines.append(header)
        lines.append(f"   {content.strip()}")
    return "\n".join(lines)


def _agent_boundary_label(item: Dict[str, Any]) -> str:
    """Return a compact provenance label from XMemo agent-boundary metadata."""
    boundary = item.get("agent_boundary")
    if isinstance(boundary, str):
        return boundary
    if isinstance(boundary, dict):
        for key in ("boundary", "agent_boundary", "relationship", "ownership"):
            value = boundary.get(key)
            if value:
                return str(value)
    ownership = item.get("ownership")
    if ownership:
        return str(ownership)
    return ""


def _format_recall_context(context: Dict[str, Any]) -> str:
    """Extract context_text from a recall_context response."""
    if not context:
        return ""
    text = context.get("context_text", "")
    if text and text.strip():
        return text.strip()
    items = context.get("items", [])
    if not items:
        return ""
    lines = []
    for i, item in enumerate(items, 1):
        content = item.get("content", "")
        if not content:
            continue
        boundary = _agent_boundary_label(item)
        prefix = f"{i}."
        if boundary:
            prefix += f" [{boundary}]"
        lines.append(f"{prefix} {content.strip()}")
    return "\n".join(lines)


def _session_key(session_id: str) -> str:
    """Normalize session id for cache keys."""
    return session_id or "__default__"


def _as_bool(value: Any) -> bool:
    """Parse bool-like values from JSON/config strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _is_high_signal_turn(user_content: str, assistant_content: str) -> bool:
    """Detect turns that likely contain durable facts without LLM extraction."""
    text = f"{user_content} {assistant_content}".lower()
    high_signal_phrases = [
        "remember",
        "save this",
        "write this down",
        "keep in mind",
        "going forward",
        "from now on",
        "we decided",
        "decision:",
        "architecture decision",
        "root cause",
        "fix was",
        "lesson learned",
        "runbook",
        "handoff",
        "blocked by",
        "blocker:",
    ]
    return any(phrase in text for phrase in high_signal_phrases)


def _redact_for_log(text: str, max_len: int = 200) -> str:
    """Truncate and redact sensitive-looking content before storing/logging."""
    if not text:
        return ""
    if len(text) > max_len:
        text = text[:max_len] + "..."
    # Mask likely tokens/keys in logs (best-effort).
    return re.sub(r"\b[a-zA-Z0-9_-]{24,}\b", "[REDACTED]", text)


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class XMemoMemoryProvider(MemoryProvider):
    """XMemo cloud memory provider for Hermes Agent."""

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._client: Optional[XMemoClient] = None
        self._client_lock = threading.Lock()

        # Per-session prefetch cache
        self._prefetch_results: Dict[str, str] = {}
        self._prefetch_threads: Dict[str, threading.Thread] = {}
        self._prefetch_lock = threading.Lock()

        # Background worker references for clean shutdown
        self._snapshot_thread: Optional[threading.Thread] = None

        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

        # Provider status tracking (P0)
        self._status = "unknown"  # online | degraded | offline | unknown
        self._last_success_at = 0.0
        self._last_error = ""

        # Local database cache and outbox (P1)
        self._local_cache = None
        self._outbox_sync_lock = threading.Lock()

        # Session / runtime metadata
        self._session_id = ""
        self._turn_count = 0
        self._agent_context = "primary"
        self._auto_write_enabled = True

    @property
    def name(self) -> str:
        return "xmemo"

    def is_available(self) -> bool:
        """Check if XMemo is configured. No network calls and no file writes."""
        try:
            cfg = load_config(create_instance=False)
            return bool(cfg.get("api_key"))
        except Exception:
            return False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        # Keep the interactive Hermes setup path intentionally small:
        # a default XMemo Cloud install only needs a token. Advanced values
        # such as bucket, scope, and optional tools still have
        # defaults in config.load_config() and can be overridden with
        # environment variables or $HERMES_HOME/xmemo.json.
        return [
            {
                "key": "api_key",
                "description": "XMemo token from xmemo.dev",
                "secret": True,
                "required": True,
                "env_var": "XMEMO_KEY",
                "url": "https://xmemo.dev",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Write non-secret config to $HERMES_HOME/xmemo.json."""
        save_config(values, hermes_home=hermes_home)

    def post_setup(self, hermes_home: str, config: Dict[str, Any]) -> None:
        """Run the full XMemo setup wizard after provider selection."""
        from .cli import cmd_setup
        cmd_setup(provider=self, hermes_home=hermes_home, config=config)

    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._status = "online"
        self._last_success_at = time.monotonic()

    def _record_failure(self, error: str = "") -> None:
        self._consecutive_failures += 1
        self._last_error = error or "unknown error"
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECONDS
            self._status = "offline"
            logger.warning(
                "XMemo circuit breaker tripped after %d consecutive failures. "
                "Pausing API calls for %ds.",
                self._consecutive_failures,
                _BREAKER_COOLDOWN_SECONDS,
            )
        else:
            self._status = "degraded"

    def _get_client(self) -> XMemoClient:
        """Thread-safe client accessor with lazy initialization."""
        with self._client_lock:
            if self._client is not None:
                return self._client
            self._client = XMemoClient(
                base_url=self._config.get("base_url", "https://xmemo.dev"),
                api_key=self._config.get("api_key", ""),
                agent_id=self._config.get("agent_id", "hermes"),
                agent_instance_id=self._config.get("agent_instance_id", ""),
                timeout=float(self._config.get("timeout_seconds", 5.0)),
            )
            return self._client

    def _run_write_with_outbox(
        self,
        client: XMemoClient,
        operation: str,
        endpoint: str,
        method: str,
        payload: Dict[str, Any],
        api_call_fn: Any,
    ) -> str:
        """Helper to run a write operation, enqueuing to outbox on transient failure."""
        import uuid
        # Generate stable idempotency key at initiation
        idempotency_key = uuid.uuid4().hex

        # Check if circuit breaker is open
        if self._is_breaker_open():
            return self._enqueue_transient_failure(
                operation, endpoint, method, payload, idempotency_key, "Circuit breaker is open"
            )

        try:
            # Try online API call
            result = api_call_fn(idempotency_key)
            self._record_success()

            # Trigger background outbox sync opportunistically after success
            self._trigger_outbox_sync()

            result_data = dict(result) if isinstance(result, dict) else {}
            result_data.update(self._compat_success_fields(operation, result_data))
            return json.dumps({
                "status": "synced",
                "result": self._success_message_for_op(operation, result),
                **result_data,
            })
        except Exception as exc:
            logger.debug("XMemo online write failed: %s", exc)

            # Check if error is transient
            from .client import XMemoClientError
            is_transient = True
            if isinstance(exc, XMemoClientError) and not exc.is_transient:
                is_transient = False

            if is_transient:
                return self._enqueue_transient_failure(
                    operation, endpoint, method, payload, idempotency_key, str(exc)
                )

            # Non-transient error: fail immediately
            self._record_failure(str(exc))
            return tool_error(f"XMemo {operation} failed: {exc}")

    def _enqueue_transient_failure(
        self,
        operation: str,
        endpoint: str,
        method: str,
        payload: Dict[str, Any],
        idempotency_key: str,
        error_msg: str,
    ) -> str:
        """Enqueue a transient failure into the outbox with proper pending/held status."""
        self._record_failure(error_msg)

        if not self._local_cache:
            return tool_error(f"XMemo is offline and local outbox is unavailable: {error_msg}")

        # Determine status: pending or held
        is_idempotent = operation in ("remember", "update_state")
        enable_non_idempotent = _as_bool(self._config.get("enable_non_idempotent_replay", False))

        status = "pending"
        if not is_idempotent and not enable_non_idempotent:
            status = "held"

        auto_replay = 1 if (is_idempotent or enable_non_idempotent) else 0

        try:
            self._local_cache.enqueue_write(
                operation=operation,
                endpoint=endpoint,
                method=method,
                payload=payload,
                idempotency_key=idempotency_key,
                status=status,
                auto_replay=auto_replay,
            )
            # Trigger background outbox sync (only picks up pending writes)
            if status == "pending":
                self._trigger_outbox_sync()

            result_note = "queued locally and will be synchronized automatically when connection is restored."
            if status == "held":
                result_note = (
                    "queued locally. Automatic synchronization is suspended for this operation "
                    "to prevent duplicate writes. It can be synchronized when connection is restored."
                )

            return json.dumps({
                "status": "queued",
                "idempotency_key": idempotency_key,
                "outbox_status": status,
                "result": f"XMemo is temporarily unavailable. The write operation was {result_note}",
            })
        except Exception as db_exc:
            logger.error("Failed to enqueue write: %s", db_exc)
            return tool_error(f"XMemo is offline and failed to queue write locally: {db_exc}")

    def _success_message_for_op(self, op: str, result: Any) -> str:
        """Get user-facing success message for a write operation."""
        if op == "remember":
            return "Saved to XMemo."
        elif op == "update_state":
            return "Working state saved to XMemo."
        elif op == "record_event":
            return "Event recorded in XMemo timeline."
        elif op == "create_reminder":
            return "Reminder saved to XMemo."
        elif op == "complete_reminder":
            return "Reminder marked completed."
        elif op == "mark_used":
            return "Memory usage recorded in XMemo."
        elif op == "forget":
            return "Memory deleted from XMemo."
        elif op == "create_restart_snapshot":
            return "Restart snapshot created."
        return "Operation completed successfully."

    def _compat_success_fields(self, op: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """Add legacy aliases so older callers do not break on field renames."""
        compat: Dict[str, Any] = {}
        result_id = result.get("id")

        if op == "remember":
            if result_id is not None and "memory_id" not in result:
                compat["memory_id"] = result_id
            if "memory_id" in result and result_id is None:
                compat["id"] = result["memory_id"]
        elif op == "record_event":
            if result_id is not None and "event_id" not in result:
                compat["event_id"] = result_id
            if "event_id" in result and result_id is None:
                compat["id"] = result["event_id"]
        elif op in ("create_reminder", "complete_reminder"):
            if result_id is not None and "todo_id" not in result:
                compat["todo_id"] = result_id
            if "todo_id" in result and result_id is None:
                compat["id"] = result["todo_id"]
        elif op == "mark_used":
            if result_id is not None and "memory_id" not in result:
                compat["memory_id"] = result_id
            if "memory_id" in result and result_id is None:
                compat["id"] = result["memory_id"]
        elif op == "forget":
            if result_id is not None and "memory_id" not in result:
                compat["memory_id"] = result_id
            if "memory_id" in result and result_id is None:
                compat["id"] = result["memory_id"]
        elif op == "create_restart_snapshot":
            if result_id is not None and "snapshot_id" not in result:
                compat["snapshot_id"] = result_id
            if "snapshot_id" in result and result_id is None:
                compat["id"] = result["snapshot_id"]
        elif op == "update_state":
            if result_id is not None and "state_id" not in result:
                compat["state_id"] = result_id
            if "state_id" in result and result_id is None:
                compat["id"] = result["state_id"]

        return compat

    def _trigger_outbox_sync(self) -> None:
        """Trigger a background outbox synchronization thread."""
        if not self._local_cache or not self._config.get("api_key"):
            return
        if self._is_breaker_open():
            return
        t = threading.Thread(target=self._sync_outbox, daemon=True, name="xmemo-outbox-sync")
        t.start()

    def _sync_outbox(self) -> None:
        """Background synchronization worker that drains the outbox queue."""
        if not self._local_cache:
            return

        acquired = self._outbox_sync_lock.acquire(blocking=False)
        if not acquired:
            return

        try:
            # Prune old records first (keeps DB size minimal)
            self._local_cache.prune_old_records()

            pending_records = self._local_cache.list_pending_writes()
            if not pending_records:
                return

            logger.info("XMemo outbox sync: processing %d pending writes", len(pending_records))
            client = self._get_client()

            for record in pending_records:
                record_id = record["id"]
                if not self._local_cache.lock_write_for_processing(record_id):
                    continue

                operation = record["operation"]
                endpoint = record["endpoint"]
                method = record["method"]
                idempotency_key = record["idempotency_key"]
                payload = json.loads(record["payload_json"])

                try:
                    client._request(
                        method=method,
                        path=endpoint,
                        json_body=payload,
                        idempotency_key=idempotency_key,
                    )
                    self._local_cache.mark_write_sent(record_id)
                    self._record_success()
                    logger.info("XMemo outbox sync: successfully synchronized %s write (%s)", operation, idempotency_key)
                except Exception as exc:
                    from .client import XMemoClientError
                    is_transient = True
                    if isinstance(exc, XMemoClientError) and not exc.is_transient:
                        is_transient = False

                    self._local_cache.mark_write_failed(
                        record_id=record_id,
                        error=str(exc),
                        is_transient=is_transient,
                        max_retries=5,
                    )
                    self._record_failure(str(exc))
                    logger.warning("XMemo outbox sync: failed to synchronize %s write (%s): %s", operation, idempotency_key, exc)

                    if self._is_breaker_open():
                        logger.debug("XMemo outbox sync: circuit breaker tripped, suspending sync")
                        break
        except Exception as exc:
            logger.error("XMemo outbox sync encountered unexpected error: %s", exc)
        finally:
            self._outbox_sync_lock.release()

    def _write_bucket(self) -> str:
        """Bucket for new Hermes-authored memories."""
        return str(self._config.get("bucket", "work") or "work")

    def _write_scope(self) -> str:
        """Scope for new Hermes-authored memories."""
        return str(self._config.get("scope", "hermes/default") or "hermes/default")

    def _read_bucket(self) -> str:
        """Bucket filter for recall/search.

        Read paths default to all visible buckets so Hermes can recall memories
        written by other agents in the same XMemo account. Operators may still
        narrow this with an advanced ``read_bucket`` value in xmemo.json.
        """
        return str(self._config.get("read_bucket") or "%")

    def _read_scope(self) -> Optional[str]:
        """Scope filter for recall/search.

        ``None`` means no explicit scope filter, matching XMemo's MCP/REST
        recall defaults. Operators may set ``read_scope`` in xmemo.json to
        narrow recall for a specific deployment.
        """
        value = self._config.get("read_scope")
        if value is None or str(value).strip() == "":
            return None
        return str(value)

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize XMemo provider for a session."""
        self._config = load_config(create_instance=True)
        self._session_id = session_id or ""
        self._turn_count = 0

        self._agent_context = kwargs.get("agent_context", "primary") or "primary"
        self._auto_write_enabled = self._agent_context == "primary"

        # Scope per-profile if the active Hermes profile differs from default
        profile = kwargs.get("agent_identity") or "default"
        configured_scope = self._config.get("scope", "hermes/default")
        if configured_scope == "hermes/default" and profile != "default":
            self._config["scope"] = f"hermes/{profile}"
            try:
                save_config(self._config)
            except Exception:
                pass

        if not self._config.get("api_key"):
            logger.debug("XMemo not configured — plugin inactive")
            return

        # Initialize local database cache
        from .cache import XMemoLocalCache
        try:
            if self._local_cache is None:
                self._local_cache = XMemoLocalCache()
            # Perform stale lock recovery on startup
            recovered = self._local_cache.recover_stale_locks()
            if recovered > 0:
                logger.info("Recovered %d stale processing outbox locks", recovered)
        except Exception as exc:
            logger.error("Failed to initialize local cache database: %s", exc)

        # Optional lightweight health check; failure does not block startup.
        try:
            client = self._get_client()
            client.health()
            self._record_success()
        except Exception as exc:
            logger.debug("XMemo health check failed (non-blocking): %s", exc)
            self._record_failure(str(exc))

        # Trigger background outbox sync attempt on startup
        self._trigger_outbox_sync()

    def system_prompt_block(self) -> str:
        """Return provider instructions for the system prompt, including live status."""
        if not self._config.get("api_key"):
            return ""
        scope = self._config.get("scope", "hermes/default")

        # Status line varies by provider state.
        status = self._status
        if status == "online" or status == "unknown":
            status_line = (
                "XMemo status: online. "
                "Recall/search reads all visible user-owned XMemo memories across connected agents."
            )
        elif status == "degraded":
            status_line = (
                "XMemo status: degraded. "
                "Remote recall may be temporarily unavailable. "
                "Do not assume the user has no saved memories just because recall is empty."
            )
        else:  # offline
            status_line = (
                "XMemo status: offline. "
                "Memory service is temporarily unavailable. "
                "Do not overwrite or forget user memory based only on missing recall results."
            )

        return (
            "# XMemo Memory\n"
            f"{status_line}\n"
            f"New Hermes memories are written to scope: {scope}.\n"
            "Use agent_boundary/provenance metadata to distinguish self vs other_agent memories. "
            "Use xmemo_search to recall saved facts before answering. "
            "Use xmemo_remember to store durable facts (preferences, decisions, conventions, action items). "
            "Use xmemo_update_state to save the current task state with TTL."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return prefetched XMemo context for the upcoming turn."""
        if _is_trivial_prompt(query):
            return ""

        key = _session_key(session_id or self._session_id)
        thread = self._prefetch_threads.get(key)
        if thread and thread.is_alive():
            thread.join(timeout=_PREFETCH_JOIN_TIMEOUT_SECONDS)

        with self._prefetch_lock:
            result = self._prefetch_results.pop(key, "")

        if not result:
            return ""
        return f"## XMemo Memory\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire a background recall for the next turn."""
        if not self._config.get("api_key"):
            return
        if _is_trivial_prompt(query):
            return

        key = _session_key(session_id or self._session_id)

        # Guard against a hung prior thread for this session
        prior = self._prefetch_threads.get(key)
        if prior and prior.is_alive():
            logger.debug("XMemo prefetch skipped: prior thread still running for session %s", key)
            return

        def _run() -> None:
            max_items = int(self._config.get("prefetch_max_items", 5))
            max_tokens = int(self._config.get("prefetch_max_tokens", 900))
            params = {
                "bucket": self._read_bucket(),
                "scope": self._read_scope(),
                "memory_type": "auto",
                "max_items": max_items,
                "max_tokens": max_tokens,
                "prefer_working": True,
                "base_url": self._config.get("base_url", "https://xmemo.dev"),
            }

            try:
                if self._is_breaker_open():
                    raise Exception("Circuit breaker is open")

                client = self._get_client()
                context = client.recall_context(
                    query=query,
                    bucket=self._read_bucket(),
                    scope=self._read_scope(),
                    max_items=max_items,
                    max_tokens=max_tokens,
                    prefer_working=True,
                )
                self._record_success()

                # Write successful results to cache
                if self._local_cache:
                    try:
                        self._local_cache.put_cached_recall("recall_context", query, params, context)
                    except Exception as db_exc:
                        logger.error("Failed to cache prefetch recall_context: %s", db_exc)

                text = _format_recall_context(context)
                if text:
                    with self._prefetch_lock:
                        self._prefetch_results[key] = text
            except Exception as exc:
                self._record_failure(str(exc))
                logger.debug("XMemo prefetch online failed: %s", exc)

                # Evaluate if failure is transient/retryable
                from .client import XMemoClientError
                is_transient = True
                if isinstance(exc, XMemoClientError) and not exc.is_transient:
                    is_transient = False

                if is_transient and self._local_cache:
                    try:
                        cached = self._local_cache.get_cached_recall("recall_context", query, params)
                        if cached:
                            meta = cached.get("_cache_metadata", {})
                            import datetime
                            last_synced_at = datetime.datetime.fromtimestamp(
                                meta.get("created_at", 0), datetime.timezone.utc
                            ).isoformat()

                            text = _format_recall_context(cached)
                            if text:
                                warning_text = (
                                    f"[Degraded: returning cached XMemo memories from {last_synced_at}. "
                                    "These may be stale. Do not assume the user has no other memories.]\n"
                                    f"{text}"
                                )
                                with self._prefetch_lock:
                                    self._prefetch_results[key] = warning_text
                    except Exception as db_exc:
                        logger.debug("Failed to read prefetch from cache fallback: %s", db_exc)

        t = threading.Thread(target=_run, daemon=True, name=f"xmemo-prefetch-{key}")
        with self._prefetch_lock:
            self._prefetch_threads[key] = t
        t.start()

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        """Persist a completed turn to XMemo if it is high-signal and trigger outbox sync."""
        if not self._config.get("api_key"):
            return

        # Always trigger background outbox sync at the end of every turn
        self._trigger_outbox_sync()

        if not self._auto_write_enabled:
            return

        self._turn_count += 1

        # Automatic timeline writes are opt-in only. When disabled, do not record
        # any turn — even high-signal ones — to avoid surprising privacy behavior.
        if not _as_bool(self._config.get("capture_timeline", False)):
            return

        # When enabled, still only persist high-signal turns to avoid noise.
        if not _is_high_signal_turn(user_content, assistant_content):
            return

        # Defensive truncation to avoid storing long raw outputs or secrets.
        safe_user = _redact_for_log(user_content, max_len=240)
        summary = f"Turn {self._turn_count}: {safe_user[:120]}..."

        payload = {
            "content": summary,
            "event_type": "session_event",
            "bucket": self._write_bucket(),
            "scope": self._write_scope(),
            "session_id": session_id or self._session_id,
        }

        try:
            client = self._get_client()
            self._run_write_with_outbox(
                client=client,
                operation="record_event",
                endpoint="/v1/timeline/events",
                method="POST",
                payload=payload,
                api_call_fn=lambda key: client.record_event(
                    content=summary,
                    event_type="session_event",
                    bucket=self._write_bucket(),
                    scope=self._write_scope(),
                    session_id=session_id or self._session_id,
                    idempotency_key=key,
                )
            )
        except Exception as exc:
            logger.debug("XMemo sync_turn failed: %s", exc)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Must be callable BEFORE initialize() because MemoryManager.add_provider()
        # indexes tool names for routing immediately after loading. Read config
        # from disk if we have not been initialized yet.
        cfg = self._config if self._config else load_config(create_instance=False)
        schemas = list(_CORE_TOOL_SCHEMAS)
        if _as_bool(cfg.get("enable_workflow_tools", False)):
            schemas.extend(_WORKFLOW_TOOL_SCHEMAS)
        if _as_bool(cfg.get("enable_destructive_tools", False)):
            schemas.extend(_DESTRUCTIVE_TOOL_SCHEMAS)
        # Feedback tools remain internal by default; can be exposed via config later.
        return schemas

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs
    ) -> str:
        """Route a tool call to the correct XMemo API."""
        try:
            client = self._get_client()
        except Exception as exc:
            return tool_error(str(exc))

        if tool_name == "xmemo_search":
            return self._handle_search(client, args)
        if tool_name == "xmemo_remember":
            return self._handle_remember(client, args)
        if tool_name == "xmemo_update_state":
            return self._handle_update_state(client, args)
        if tool_name == "xmemo_recall_context":
            return self._handle_recall_context(client, args)
        if tool_name == "xmemo_record_event":
            return self._handle_record_event(client, args)
        if tool_name == "xmemo_create_reminder":
            return self._handle_create_reminder(client, args)
        if tool_name == "xmemo_list_reminders":
            return self._handle_list_reminders(client, args)
        if tool_name == "xmemo_complete_reminder":
            return self._handle_complete_reminder(client, args)
        if tool_name == "xmemo_mark_used":
            return self._handle_mark_used(client, args)
        if tool_name == "xmemo_forget":
            return self._handle_forget(client, args)

        return tool_error(f"Unknown XMemo tool: {tool_name}")

    def _handle_search(self, client: XMemoClient, args: Dict[str, Any]) -> str:
        query = args.get("query", "").strip()
        if not query:
            return tool_error("Missing required parameter: query")

        try:
            limit = min(int(args.get("limit", 5)), 20)
        except (ValueError, TypeError):
            limit = 5
        memory_type = args.get("memory_type", "%")

        # Cache lookup signature parameters
        params = {
            "bucket": self._read_bucket(),
            "scope": self._read_scope(),
            "memory_type": memory_type,
            "limit": limit,
            "base_url": self._config.get("base_url", "https://xmemo.dev"),
        }

        try:
            if self._is_breaker_open():
                raise Exception("Circuit breaker is open")

            results = client.search(
                query=query,
                bucket=self._read_bucket(),
                scope=self._read_scope(),
                memory_type=memory_type,
                limit=limit,
            )
            self._record_success()

            # Write successful results to cache
            if self._local_cache:
                try:
                    self._local_cache.put_cached_recall("search", query, params, {"results": results})
                except Exception as db_exc:
                    logger.error("Failed to cache search results: %s", db_exc)

            if not results:
                return json.dumps({"result": "No relevant XMemo memories found."})
            return json.dumps({
                "results": results,
                "formatted": _format_search_results(results),
                "count": len(results),
            })
        except Exception as exc:
            self._record_failure(str(exc))
            logger.debug("XMemo search online failed: %s", exc)

            # Evaluate if failure is transient/retryable
            from .client import XMemoClientError
            is_transient = True
            if isinstance(exc, XMemoClientError) and not exc.is_transient:
                is_transient = False

            if is_transient and self._local_cache:
                try:
                    cached = self._local_cache.get_cached_recall("search", query, params)
                    if cached and "results" in cached:
                        results = cached["results"]
                        meta = cached.get("_cache_metadata", {})
                        import datetime
                        last_synced_at = datetime.datetime.fromtimestamp(
                            meta.get("created_at", 0), datetime.timezone.utc
                        ).isoformat()

                        note = (
                            "XMemo is temporarily offline. Returning cached memories. These may be stale and "
                            "do not represent the absolute latest cloud state. Do not assume the user has no other memories."
                        )

                        if not results:
                            return json.dumps({
                                "status": "degraded",
                                "source": "cache",
                                "stale": True,
                                "last_synced_at": last_synced_at,
                                "note": note,
                                "result": "No relevant XMemo memories found in local cache.",
                            })

                        return json.dumps({
                            "status": "degraded",
                            "source": "cache",
                            "stale": True,
                            "last_synced_at": last_synced_at,
                            "note": note,
                            "results": results,
                            "formatted": _format_search_results(results),
                            "count": len(results),
                        })
                except Exception as db_exc:
                    logger.debug("Failed to read search from cache fallback: %s", db_exc)

            # Cache miss or non-transient error
            status = "offline" if self._is_breaker_open() else "degraded"
            return json.dumps({
                "status": status,
                "error": f"XMemo search failed and no cached copy is available: {exc}",
                "note": "The memory store is temporarily unreachable and there is no local cache for this query. Do not assume the user has no memories.",
            })

    def _handle_remember(self, client: XMemoClient, args: Dict[str, Any]) -> str:
        content = args.get("content", "").strip()
        path = args.get("path", "").strip()
        if not content:
            return tool_error("Missing required parameter: content")
        if not path:
            return tool_error("Missing required parameter: path")

        memory_type = args.get("memory_type", "semantic")
        importance = args.get("importance")
        if importance is not None:
            try:
                importance = float(importance)
            except (ValueError, TypeError):
                importance = None

        payload = {
            "content": content,
            "path": path,
            "bucket": self._write_bucket(),
            "scope": self._write_scope(),
            "memory_type": memory_type,
            "dedupe": True,
        }
        if importance is not None:
            payload["importance"] = importance

        return self._run_write_with_outbox(
            client=client,
            operation="remember",
            endpoint="/v1/remember",
            method="POST",
            payload=payload,
            api_call_fn=lambda key: client.remember(
                content=content,
                path=path,
                bucket=self._write_bucket(),
                scope=self._write_scope(),
                memory_type=memory_type,
                importance=importance,
                idempotency_key=key,
            )
        )

    def _handle_update_state(self, client: XMemoClient, args: Dict[str, Any]) -> str:
        current_task = args.get("current_task", "").strip()
        next_action = args.get("next_action", "").strip()
        blocked_reason = args.get("blocked_reason", "").strip()
        try:
            ttl_seconds = int(args.get("ttl_seconds", 86400))
        except (ValueError, TypeError):
            ttl_seconds = 86400

        if not any([current_task, next_action, blocked_reason]):
            return tool_error("At least one of current_task, next_action, or blocked_reason is required")

        payload = {
            "current_task": current_task,
            "next_action": next_action,
            "blocked_reason": blocked_reason,
            "bucket": self._write_bucket(),
            "scope": self._write_scope(),
            "ttl_seconds": ttl_seconds,
        }

        return self._run_write_with_outbox(
            client=client,
            operation="update_state",
            endpoint="/v1/update_state",
            method="POST",
            payload=payload,
            api_call_fn=lambda key: client.update_state(
                current_task=current_task,
                next_action=next_action,
                blocked_reason=blocked_reason,
                bucket=self._write_bucket(),
                scope=self._write_scope(),
                ttl_seconds=ttl_seconds,
                idempotency_key=key,
            )
        )

    def _handle_recall_context(self, client: XMemoClient, args: Dict[str, Any]) -> str:
        query = args.get("query", "").strip()
        if not query:
            return tool_error("Missing required parameter: query")

        try:
            max_items = min(int(args.get("max_items", 5)), 20)
        except (ValueError, TypeError):
            max_items = 5
        memory_type = args.get("memory_type", "auto")
        max_tokens = int(self._config.get("prefetch_max_tokens", 900))

        # Cache lookup signature parameters
        params = {
            "bucket": self._read_bucket(),
            "scope": self._read_scope(),
            "memory_type": memory_type,
            "max_items": max_items,
            "max_tokens": max_tokens,
            "prefer_working": True,
            "base_url": self._config.get("base_url", "https://xmemo.dev"),
        }

        try:
            if self._is_breaker_open():
                raise Exception("Circuit breaker is open")

            context = client.recall_context(
                query=query,
                bucket=self._read_bucket(),
                scope=self._read_scope(),
                max_items=max_items,
                max_tokens=max_tokens,
                memory_type=memory_type,
                prefer_working=True,
            )
            self._record_success()

            # Write successful results to cache
            if self._local_cache:
                try:
                    self._local_cache.put_cached_recall("recall_context", query, params, context)
                except Exception as db_exc:
                    logger.error("Failed to cache recall_context: %s", db_exc)

            text = _format_recall_context(context)
            if not text:
                return json.dumps({"result": "No relevant XMemo context found."})
            return json.dumps({
                "context": text,
                "items": context.get("items", []) if isinstance(context, dict) else [],
            })
        except Exception as exc:
            self._record_failure(str(exc))
            logger.debug("XMemo recall_context online failed: %s", exc)

            # Evaluate if failure is transient/retryable
            from .client import XMemoClientError
            is_transient = True
            if isinstance(exc, XMemoClientError) and not exc.is_transient:
                is_transient = False

            if is_transient and self._local_cache:
                try:
                    cached = self._local_cache.get_cached_recall("recall_context", query, params)
                    if cached:
                        meta = cached.get("_cache_metadata", {})
                        import datetime
                        last_synced_at = datetime.datetime.fromtimestamp(
                            meta.get("created_at", 0), datetime.timezone.utc
                        ).isoformat()

                        note = (
                            "XMemo is temporarily offline. Returning cached memories. These may be stale and "
                            "do not represent the absolute latest cloud state. Do not assume the user has no other memories."
                        )

                        text = _format_recall_context(cached)
                        if not text:
                            return json.dumps({
                                "status": "degraded",
                                "source": "cache",
                                "stale": True,
                                "last_synced_at": last_synced_at,
                                "note": note,
                                "result": "No relevant XMemo context found in local cache.",
                            })

                        return json.dumps({
                            "status": "degraded",
                            "source": "cache",
                            "stale": True,
                            "last_synced_at": last_synced_at,
                            "note": note,
                            "context": text,
                            "items": cached.get("items", []) if isinstance(cached, dict) else [],
                        })
                except Exception as db_exc:
                    logger.debug("Failed to read recall_context from cache fallback: %s", db_exc)

            # Cache miss or non-transient error
            status = "offline" if self._is_breaker_open() else "degraded"
            return json.dumps({
                "status": status,
                "error": f"XMemo recall_context failed and no cached copy is available: {exc}",
                "note": "The memory store is temporarily unreachable and there is no local cache for this query. Do not assume the user has no memories.",
            })

    def _handle_record_event(self, client: XMemoClient, args: Dict[str, Any]) -> str:
        content = args.get("content", "").strip()
        if not content:
            return tool_error("Missing required parameter: content")

        event_type = args.get("event_type", "event").strip() or "event"
        payload = {
            "content": content,
            "event_type": event_type,
            "bucket": self._write_bucket(),
            "scope": self._write_scope(),
            "session_id": self._session_id,
        }

        return self._run_write_with_outbox(
            client=client,
            operation="record_event",
            endpoint="/v1/timeline/events",
            method="POST",
            payload=payload,
            api_call_fn=lambda key: client.record_event(
                content=content,
                event_type=event_type,
                bucket=self._write_bucket(),
                scope=self._write_scope(),
                session_id=self._session_id,
                idempotency_key=key,
            )
        )

    def _handle_create_reminder(self, client: XMemoClient, args: Dict[str, Any]) -> str:
        content = args.get("content", "").strip()
        if not content:
            return tool_error("Missing required parameter: content")

        due_at = args.get("due_at", "").strip()
        payload = {
            "content": content,
            "due_at": due_at,
            "bucket": self._write_bucket(),
            "scope": self._write_scope(),
            "session_id": self._session_id,
        }

        return self._run_write_with_outbox(
            client=client,
            operation="create_reminder",
            endpoint="/v1/reminders",
            method="POST",
            payload=payload,
            api_call_fn=lambda key: client.create_reminder(
                content=content,
                due_at=due_at,
                bucket=self._write_bucket(),
                scope=self._write_scope(),
                session_id=self._session_id,
                idempotency_key=key,
            )
        )

    def _handle_list_reminders(self, client: XMemoClient, args: Dict[str, Any]) -> str:
        if self._is_breaker_open():
            return tool_error("XMemo API temporarily unavailable (circuit breaker open).")

        item_status = args.get("item_status", "open") or "open"
        try:
            limit = min(int(args.get("limit", 20)), 100)
        except (ValueError, TypeError):
            limit = 20

        try:
            items = client.list_reminders(
                bucket=self._read_bucket(),
                scope=self._read_scope(),
                item_status=item_status,
                limit=limit,
            )
            self._record_success()
            if not items:
                return json.dumps({"result": f"No {item_status} XMemo reminders found."})
            return json.dumps({
                "items": items,
                "count": len(items),
            })
        except Exception as exc:
            self._record_failure(str(exc))
            return tool_error(f"XMemo list_reminders failed: {exc}")

    def _handle_complete_reminder(self, client: XMemoClient, args: Dict[str, Any]) -> str:
        todo_id = args.get("todo_id", "").strip()
        if not todo_id:
            return tool_error("Missing required parameter: todo_id")

        note = args.get("note", "").strip()
        payload = {
            "bucket": self._read_bucket(),
            "scope": self._read_scope(),
            "note": note,
        }

        return self._run_write_with_outbox(
            client=client,
            operation="complete_reminder",
            endpoint=f"/v1/reminders/{todo_id}/complete",
            method="POST",
            payload=payload,
            api_call_fn=lambda key: client.complete_reminder(
                todo_id=todo_id,
                note=note,
                bucket=self._read_bucket(),
                scope=self._read_scope(),
                idempotency_key=key,
            )
        )

    def _handle_mark_used(self, client: XMemoClient, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "").strip()
        if not memory_id:
            return tool_error("Missing required parameter: memory_id")

        context = args.get("context", "").strip()
        payload = {
            "action": "used",
            "context": context,
        }

        return self._run_write_with_outbox(
            client=client,
            operation="mark_used",
            endpoint=f"/v1/memories/{memory_id}/usage",
            method="POST",
            payload=payload,
            api_call_fn=lambda key: client.mark_used(
                memory_id=memory_id,
                context=context,
                idempotency_key=key,
            )
        )

    def _handle_forget(self, client: XMemoClient, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "").strip()
        if not memory_id:
            return tool_error("Missing required parameter: memory_id")

        reason = args.get("reason", "").strip()
        payload = {
            "bucket": self._read_bucket(),
            "scope": self._read_scope(),
            "reason": reason,
        }

        return self._run_write_with_outbox(
            client=client,
            operation="forget",
            endpoint=f"/v1/memories/{memory_id}/forget",
            method="POST",
            payload=payload,
            api_call_fn=lambda key: client.forget(
                memory_id=memory_id,
                reason=reason,
                bucket=self._read_bucket(),
                scope=self._read_scope(),
                idempotency_key=key,
            )
        )

    def shutdown(self) -> None:
        """Clean shutdown: flush threads and close client."""
        for t in list(self._prefetch_threads.values()):
            if t and t.is_alive():
                t.join(timeout=1.0)
        if self._snapshot_thread and self._snapshot_thread.is_alive():
            self._snapshot_thread.join(timeout=5.0)
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception as exc:
                    logger.debug("XMemo client close failed: %s", exc)
                self._client = None

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """Update session tracking and clean stale prefetch cache."""
        old_key = _session_key(self._session_id)
        self._session_id = new_session_id or ""

        if reset or rewound:
            with self._prefetch_lock:
                self._prefetch_results.pop(old_key, None)
                old_thread = self._prefetch_threads.pop(old_key, None)
            if old_thread and old_thread.is_alive():
                old_thread.join(timeout=1.0)

        if reset:
            self._turn_count = 0

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror Hermes built-in memory writes to XMemo."""
        if not self._config.get("api_key"):
            return
        if not self._auto_write_enabled:
            return
        if action not in {"add", "replace"}:
            # Remove is not mirrored until we have stable remote id mapping.
            return
        if not content:
            return

        path = f"hermes/builtin-memory/{target}"
        payload = {
            "content": content,
            "path": path,
            "bucket": self._write_bucket(),
            "scope": self._write_scope(),
            "memory_type": "semantic",
            "dedupe": True,
        }

        try:
            client = self._get_client()
            self._run_write_with_outbox(
                client=client,
                operation="remember",
                endpoint="/v1/remember",
                method="POST",
                payload=payload,
                api_call_fn=lambda key: client.remember(
                    content=content,
                    path=path,
                    bucket=self._write_bucket(),
                    scope=self._write_scope(),
                    memory_type="semantic",
                    idempotency_key=key,
                )
            )
        except Exception as exc:
            logger.debug("XMemo on_memory_write mirror failed: %s", exc)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Capture a restart snapshot at session end and trigger final outbox sync."""
        if not self._config.get("api_key"):
            return

        # Trigger background outbox sync at session end
        self._trigger_outbox_sync()

        if not self._auto_write_enabled:
            return

        def _snapshot() -> None:
            payload = {
                "session_id": self._session_id,
                "bucket": self._write_bucket(),
                "scope": self._write_scope(),
            }
            try:
                client = self._get_client()
                self._run_write_with_outbox(
                    client=client,
                    operation="create_restart_snapshot",
                    endpoint="/v1/restart/snapshot",
                    method="POST",
                    payload=payload,
                    api_call_fn=lambda key: client.create_restart_snapshot(
                        session_id=self._session_id,
                        bucket=self._write_bucket(),
                        scope=self._write_scope(),
                        idempotency_key=key,
                    )
                )
            except Exception as exc:
                logger.debug("XMemo session-end snapshot failed: %s", exc)

        self._snapshot_thread = threading.Thread(
            target=_snapshot, daemon=True, name="xmemo-snapshot"
        )
        self._snapshot_thread.start()


def register(ctx) -> None:
    """Register XMemo as a memory provider plugin."""
    ctx.register_memory_provider(XMemoMemoryProvider())
