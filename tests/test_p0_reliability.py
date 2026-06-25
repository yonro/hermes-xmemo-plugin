"""P0 verification tests for retry, status tracking, and system prompt injection.

Run with: python tests/test_p0_reliability.py
"""
from __future__ import annotations

import os
import sys

# Ensure src/ is on the path so we can import hermes_xmemo without pip install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

import json
import sys
import time
import unittest
from typing import Any, Dict, List, Optional
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
from pathlib import Path
hc_mod = type(sys)("hermes_constants")
hc_mod.get_hermes_home = lambda: Path("/fake/hermes/home")
sys.modules.setdefault("hermes_constants", hc_mod)

# Now safe to import
import httpx

from hermes_xmemo.xmemo.client import XMemoClient, XMemoClientError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockTransport(httpx.BaseTransport):
    """Transport that returns pre-configured responses in order."""

    def __init__(self, responses: List[httpx.Response]):
        self._responses = list(responses)
        self._call_count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._call_count >= len(self._responses):
            raise RuntimeError("MockTransport exhausted")
        resp = self._responses[self._call_count]
        resp.request = request  # httpx requires this
        self._call_count += 1
        return resp

    @property
    def call_count(self) -> int:
        return self._call_count


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

class TestRetry5xx(unittest.TestCase):
    """5xx errors should be retried up to max_attempts."""

    def test_5xx_retried_then_succeeds(self):
        transport = _MockTransport([
            _make_response(502, "Bad Gateway"),
            _make_response(200, {"ok": True}),
        ])
        client = XMemoClient(
            base_url="http://localhost",
            api_key="test",
            transport=transport,
        )
        result = client._request("GET", "/health", initial_delay=0.01)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(transport.call_count, 2)

    def test_5xx_exhausts_retries(self):
        transport = _MockTransport([
            _make_response(500, "Internal Server Error"),
            _make_response(500, "Internal Server Error"),
            _make_response(500, "Internal Server Error"),
        ])
        client = XMemoClient(
            base_url="http://localhost",
            api_key="test",
            transport=transport,
        )
        with self.assertRaises(XMemoClientError) as ctx:
            client._request("GET", "/health", initial_delay=0.01)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(transport.call_count, 3)


class TestNoRetry4xx(unittest.TestCase):
    """4xx errors should NOT be retried."""

    def test_401_not_retried(self):
        transport = _MockTransport([
            _make_response(401, {"error": "unauthorized"}),
        ])
        client = XMemoClient(
            base_url="http://localhost",
            api_key="bad",
            transport=transport,
        )
        with self.assertRaises(XMemoClientError) as ctx:
            client._request("GET", "/health", initial_delay=0.01)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(transport.call_count, 1)

    def test_404_not_retried(self):
        transport = _MockTransport([
            _make_response(404, "Not Found"),
        ])
        client = XMemoClient(
            base_url="http://localhost",
            api_key="test",
            transport=transport,
        )
        with self.assertRaises(XMemoClientError) as ctx:
            client._request("GET", "/v1/missing", initial_delay=0.01)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(transport.call_count, 1)

    def test_422_not_retried(self):
        transport = _MockTransport([
            _make_response(422, {"detail": "validation error"}),
        ])
        client = XMemoClient(
            base_url="http://localhost",
            api_key="test",
            transport=transport,
        )
        with self.assertRaises(XMemoClientError) as ctx:
            client._request("POST", "/v1/remember", initial_delay=0.01)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(transport.call_count, 1)


class TestProviderStatus(unittest.TestCase):
    """Provider status tracking and system prompt injection."""

    def _make_provider(self):
        from hermes_xmemo.xmemo import XMemoMemoryProvider
        p = XMemoMemoryProvider()
        p._config = {"api_key": "test-key", "scope": "hermes/default"}
        return p

    def test_initial_status_is_unknown(self):
        p = self._make_provider()
        self.assertEqual(p._status, "unknown")

    def test_record_success_sets_online(self):
        p = self._make_provider()
        p._record_success()
        self.assertEqual(p._status, "online")
        self.assertGreater(p._last_success_at, 0)

    def test_record_failure_sets_degraded(self):
        p = self._make_provider()
        p._record_failure("timeout")
        self.assertEqual(p._status, "degraded")
        self.assertEqual(p._last_error, "timeout")

    def test_breaker_trips_to_offline(self):
        p = self._make_provider()
        for i in range(5):  # _BREAKER_THRESHOLD = 5
            p._record_failure(f"error {i}")
        self.assertEqual(p._status, "offline")

    def test_system_prompt_online(self):
        p = self._make_provider()
        p._record_success()
        prompt = p.system_prompt_block()
        self.assertIn("XMemo status: online", prompt)
        self.assertIn("scope: hermes/default", prompt)

    def test_system_prompt_degraded(self):
        p = self._make_provider()
        p._record_failure("network timeout")
        prompt = p.system_prompt_block()
        self.assertIn("XMemo status: degraded", prompt)
        self.assertIn("Do not assume the user has no saved memories", prompt)

    def test_system_prompt_offline(self):
        p = self._make_provider()
        for i in range(5):
            p._record_failure(f"error {i}")
        prompt = p.system_prompt_block()
        self.assertIn("XMemo status: offline", prompt)
        self.assertIn("Do not overwrite or forget user memory", prompt)

    def test_system_prompt_unknown_shows_online(self):
        """Before any API call, status is 'unknown' -> show as online."""
        p = self._make_provider()
        prompt = p.system_prompt_block()
        self.assertIn("XMemo status: online", prompt)

    def test_handle_tool_call_breaker_open(self):
        p = self._make_provider()
        # Trip the breaker
        for i in range(5):
            p._record_failure(f"error {i}")
        result = json.loads(p.handle_tool_call("xmemo_search", {"query": "test"}))
        self.assertEqual(result["status"], "offline")
        self.assertIn("note", result)

    @patch("hermes_xmemo.xmemo.client.XMemoClient.health")
    def test_initialize_health_check_failure_sets_degraded(self, mock_health):
        mock_health.side_effect = Exception("connection refused")
        p = self._make_provider()
        p.initialize("test-session")
        self.assertEqual(p._status, "degraded")
        self.assertEqual(p._last_error, "connection refused")


class TestWriteNoRetry(unittest.TestCase):
    """Write operations (non-GET, non-recall-context) should NOT be retried."""

    def test_remember_500_not_retried(self):
        transport = _MockTransport([
            _make_response(500, "Internal Server Error"),
            _make_response(200, {"ok": True}),  # should not be reached
        ])
        client = XMemoClient(
            base_url="http://localhost",
            api_key="test",
            transport=transport,
        )
        with self.assertRaises(XMemoClientError) as ctx:
            client.remember(content="test content", path="test.md")
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(transport.call_count, 1)

    def test_recall_context_is_read_so_retried(self):
        transport = _MockTransport([
            _make_response(503, "Service Unavailable"),
            _make_response(200, {"ok": True}),
        ])
        client = XMemoClient(
            base_url="http://localhost",
            api_key="test",
            transport=transport,
        )
        result = client._request("POST", "/v1/recall/context", initial_delay=0.01)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(transport.call_count, 2)


class TestReadWideDefaults(unittest.TestCase):
    """Ensure read-wide defaults (bucket='%', scope=None) are preserved."""

    def test_read_bucket_default(self):
        from hermes_xmemo.xmemo import XMemoMemoryProvider
        p = XMemoMemoryProvider()
        p._config = {}
        self.assertEqual(p._read_bucket(), "%")

    def test_read_scope_default(self):
        from hermes_xmemo.xmemo import XMemoMemoryProvider
        p = XMemoMemoryProvider()
        p._config = {}
        self.assertIsNone(p._read_scope())


if __name__ == "__main__":
    unittest.main(verbosity=2)
