"""Synchronous REST client for XMemo.

Deliberately lightweight: uses ``httpx.Client`` directly instead of the async
``memory_manager.client.RemoteMemoryManager`` so Hermes does not inherit the
full Memory OS server dependency tree.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


def _drop_none(values: Dict[str, Any]) -> Dict[str, Any]:
    """Remove unset optional parameters before sending them over HTTP."""
    return {key: value for key, value in values.items() if value is not None}


class XMemoClientError(Exception):
    """Raised when an XMemo API call fails."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Any = None,
        is_transient: Optional[bool] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body

        if is_transient is not None:
            self.is_transient = is_transient
        else:
            # Infer transience from status_code
            if status_code >= 500:
                self.is_transient = True
            elif 400 <= status_code < 500:
                self.is_transient = False
            else:
                self.is_transient = False


class XMemoClient:
    """Synchronous XMemo REST client."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        agent_id: str = "hermes",
        agent_instance_id: str = "",
        timeout: float = 5.0,
        transport: Optional[Any] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.agent_id = agent_id
        self.agent_instance_id = agent_instance_id
        self.timeout = timeout

        self.headers: Dict[str, str] = {"X-API-Key": api_key}
        if agent_id:
            self.headers["X-Memory-OS-Agent-ID"] = agent_id
        if agent_instance_id:
            self.headers["X-Memory-OS-Agent-Instance-ID"] = agent_instance_id

        client_kwargs: Dict[str, Any] = {
            "base_url": self.base_url,
            "headers": self.headers,
            "timeout": httpx.Timeout(timeout),
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)

    # Transient error types that justify a retry.
    _TRANSIENT_EXCEPTIONS = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
    )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        max_attempts: int = 3,
        initial_delay: float = 0.5,
        backoff: float = 2.0,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        """Make a synchronous request with exponential-backoff retry.

        Retries on transient errors only (connection failures, timeouts,
        protocol errors, and HTTP 5xx).  Client errors (4xx) are raised
        immediately without retry.
        """
        import time as _time  # stdlib; avoids module-level import cycle

        # Only retry read operations to avoid non-idempotent write duplication.
        is_read = (method == "GET") or (method == "POST" and path == "/v1/recall/context")
        if not is_read:
            max_attempts = 1

        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None
        delay = initial_delay

        # Prepare request body (non-mutating copy)
        body = None
        if json_body is not None or idempotency_key is not None:
            body = dict(json_body) if isinstance(json_body, dict) else {}
            if idempotency_key:
                body["idempotency_key"] = idempotency_key

        # Prepare request headers
        req_headers = {}
        if idempotency_key:
            req_headers["Idempotency-Key"] = idempotency_key
            req_headers["X-Idempotency-Key"] = idempotency_key

        for attempt in range(1, max_attempts + 1):
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=body,
                    headers=req_headers if req_headers else None,
                )
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return {}
                return response.json()

            except httpx.HTTPStatusError as exc:
                resp_body = None
                try:
                    resp_body = exc.response.json()
                except Exception:
                    resp_body = exc.response.text
                status = exc.response.status_code

                # 5xx → transient, eligible for retry
                if status >= 500 and attempt < max_attempts:
                    logger.debug(
                        "XMemo API 5xx (attempt %d/%d): %s %s -> %s, retrying in %.1fs",
                        attempt, max_attempts, method, path, status, delay,
                    )
                    last_exc = XMemoClientError(
                        f"XMemo API error {status}: {resp_body}",
                        status_code=status,
                        response_body=resp_body,
                        is_transient=True,
                    )
                    _time.sleep(delay)
                    delay *= backoff
                    continue

                # 4xx or final 5xx attempt → raise immediately
                logger.debug("XMemo API error: %s %s -> %s: %s", method, path, status, resp_body)
                raise XMemoClientError(
                    f"XMemo API error {status}: {resp_body}",
                    status_code=status,
                    response_body=resp_body,
                    is_transient=(status >= 500),
                ) from exc

            except self._TRANSIENT_EXCEPTIONS as exc:
                last_exc = XMemoClientError(f"XMemo request failed: {exc}", is_transient=True)
                if attempt < max_attempts:
                    logger.debug(
                        "XMemo transient error (attempt %d/%d): %s %s -> %s, retrying in %.1fs",
                        attempt, max_attempts, method, path, exc, delay,
                    )
                    _time.sleep(delay)
                    delay *= backoff
                    continue
                logger.debug(
                    "XMemo request failed after %d attempts: %s %s -> %s",
                    max_attempts, method, path, exc,
                )
                raise last_exc from exc

            except Exception as exc:
                # Non-transient (e.g. JSON decode, programming error) → fail fast
                logger.debug("XMemo request failed: %s %s -> %s", method, path, exc)
                raise XMemoClientError(f"XMemo request failed: {exc}", is_transient=False) from exc

        # Should not reach here, but satisfy the type checker.
        assert last_exc is not None  # noqa: S101
        raise last_exc


    def health(self) -> Dict[str, Any]:
        """Check service health."""
        return self._request("GET", "/health")

    def recall_context(
        self,
        query: str,
        *,
        bucket: str = "%",
        scope: Optional[str] = None,
        max_items: int = 5,
        max_tokens: int = 900,
        memory_type: str = "auto",
        prefer_working: bool = True,
    ) -> Dict[str, Any]:
        """Build a bounded context pack from XMemo memories."""
        return self._request(
            "POST",
            "/v1/recall/context",
            json_body=_drop_none({
                "query": query,
                "bucket": bucket,
                "scope": scope,
                "max_items": max_items,
                "max_tokens": max_tokens,
                "memory_type": memory_type,
                "prefer_working": prefer_working,
            }),
        )

    def search(
        self,
        query: str,
        *,
        bucket: str = "%",
        scope: Optional[str] = None,
        memory_type: str = "%",
        limit: int = 5,
        explain: bool = False,
        prefer_working: bool = False,
    ) -> List[Dict[str, Any]]:
        """Semantic search over XMemo memories."""
        result = self._request(
            "GET",
            "/v1/memories/search",
            params=_drop_none({
                "query": query,
                "bucket": bucket,
                "scope": scope,
                "memory_type": memory_type,
                "limit": limit,
                "explain": explain,
                "prefer_working": prefer_working,
            }),
        )
        if isinstance(result, dict):
            return result.get("results", []) or []
        if isinstance(result, list):
            return result
        return []

    def remember(
        self,
        content: str,
        path: str,
        *,
        bucket: str = "work",
        scope: str = "hermes/default",
        memory_type: str = "semantic",
        importance: Optional[float] = None,
        confidence: Optional[float] = None,
        dedupe: bool = True,
        semantic_key: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save a durable fact to XMemo."""
        payload: Dict[str, Any] = {
            "content": content,
            "path": path,
            "bucket": bucket,
            "scope": scope,
            "memory_type": memory_type,
            "dedupe": dedupe,
        }
        if importance is not None:
            payload["importance"] = importance
        if confidence is not None:
            payload["confidence"] = confidence
        if semantic_key:
            payload["semantic_key"] = semantic_key
        return self._request(
            "POST", "/v1/remember", json_body=payload, idempotency_key=idempotency_key
        )

    def update_state(
        self,
        *,
        state_key: str = "active_task",
        content: str = "",
        current_task: str = "",
        next_action: str = "",
        blocked_reason: str = "",
        bucket: str = "work",
        scope: str = "hermes/default",
        ttl_seconds: int = 86400,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist active working state with TTL."""
        payload: Dict[str, Any] = {
            "state_key": state_key,
            "bucket": bucket,
            "scope": scope,
            "ttl_seconds": ttl_seconds,
        }
        if content:
            payload["content"] = content
        if current_task:
            payload["current_task"] = current_task
        if next_action:
            payload["next_action"] = next_action
        if blocked_reason:
            payload["blocked_reason"] = blocked_reason
        return self._request(
            "POST", "/v1/update_state", json_body=payload, idempotency_key=idempotency_key
        )

    def record_event(
        self,
        content: str,
        *,
        event_type: str = "event",
        bucket: str = "work",
        scope: str = "hermes/default",
        session_id: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a timeline event."""
        payload: Dict[str, Any] = {
            "content": content,
            "event_type": event_type,
            "bucket": bucket,
            "scope": scope,
        }
        if session_id:
            payload["session_id"] = session_id
        return self._request(
            "POST", "/v1/timeline/events", json_body=payload, idempotency_key=idempotency_key
        )

    def create_restart_snapshot(
        self,
        *,
        session_id: str = "",
        bucket: str = "work",
        scope: str = "hermes/default",
        state_key: str = "active_task",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Capture a restart snapshot before handoff or shutdown."""
        payload: Dict[str, Any] = {
            "bucket": bucket,
            "scope": scope,
            "state_key": state_key,
        }
        if session_id:
            payload["session_id"] = session_id
        return self._request(
            "POST", "/v1/restart/snapshot", json_body=payload, idempotency_key=idempotency_key
        )

    def create_reminder(
        self,
        content: str,
        *,
        bucket: str = "work",
        scope: str = "hermes/default",
        due_at: str = "",
        session_id: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a TODO/action item to revisit later."""
        payload: Dict[str, Any] = {
            "content": content,
            "bucket": bucket,
            "scope": scope,
        }
        if due_at:
            payload["due_at"] = due_at
        if session_id:
            payload["session_id"] = session_id
        return self._request(
            "POST", "/v1/reminders", json_body=payload, idempotency_key=idempotency_key
        )

    def list_reminders(
        self,
        *,
        bucket: str = "%",
        scope: Optional[str] = None,
        item_status: str = "open",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """List open or completed TODO items."""
        result = self._request(
            "GET",
            "/v1/reminders",
            params=_drop_none({
                "bucket": bucket,
                "scope": scope,
                "item_status": item_status,
                "limit": limit,
            }),
        )
        if isinstance(result, dict):
            return result.get("items", []) or result.get("reminders", []) or []
        if isinstance(result, list):
            return result
        return []

    def complete_reminder(
        self,
        todo_id: str,
        *,
        bucket: str = "%",
        scope: Optional[str] = None,
        note: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mark a TODO item as completed."""
        payload: Dict[str, Any] = _drop_none({"bucket": bucket, "scope": scope})
        if note:
            payload["note"] = note
        return self._request(
            "POST",
            f"/v1/reminders/{todo_id}/complete",
            json_body=payload,
            idempotency_key=idempotency_key,
        )

    def mark_used(
        self,
        memory_id: str,
        *,
        context: str = "",
        action: str = "used",
        usage_tracking_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record that a recalled memory was used in the answer.

        Payload matches Memory OS MemoryUsageRequest: only usage_tracking_id,
        action, context, and metadata are accepted (extra="forbid").
        """
        payload: Dict[str, Any] = {"action": action}
        if context:
            payload["context"] = context
        if usage_tracking_id:
            payload["usage_tracking_id"] = usage_tracking_id
        if metadata:
            payload["metadata"] = metadata
        return self._request(
            "POST",
            f"/v1/memories/{memory_id}/usage",
            json_body=payload,
            idempotency_key=idempotency_key,
        )

    def forget(
        self,
        memory_id: str,
        *,
        bucket: str = "%",
        scope: Optional[str] = None,
        reason: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Delete a memory by exact id."""
        payload: Dict[str, Any] = _drop_none({"bucket": bucket, "scope": scope})
        if reason:
            payload["reason"] = reason
        return self._request(
            "POST",
            f"/v1/memories/{memory_id}/forget",
            json_body=payload,
            idempotency_key=idempotency_key,
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        try:
            self._client.close()
        except Exception as exc:
            logger.debug("XMemo client close failed: %s", exc)
