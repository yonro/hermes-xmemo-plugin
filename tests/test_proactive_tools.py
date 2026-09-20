"""Tests for proactive XMemo tools (xmemo_search, xmemo_get, xmemo_list) and registration."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src/ is on the path so we can import hermes_xmemo without pip install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

# Minimal stubs so we can import the plugin without Hermes installed
mp_mod = type(sys)("agent.memory_provider")


class _MemoryProviderBase:
    pass


mp_mod.MemoryProvider = _MemoryProviderBase  # type: ignore[attr-defined]
sys.modules.setdefault("agent", type(sys)("agent"))
sys.modules.setdefault("agent.memory_provider", mp_mod)

tr_mod = type(sys)("tools.registry")
tr_mod.tool_error = lambda msg: json.dumps({"error": msg})  # type: ignore[attr-defined]
sys.modules.setdefault("tools", type(sys)("tools"))
sys.modules.setdefault("tools.registry", tr_mod)

hc_mod = type(sys)("hermes_constants")
hc_mod.get_hermes_home = lambda: Path("/fake/hermes/home")
sys.modules.setdefault("hermes_constants", hc_mod)

from hermes_xmemo.xmemo import (  # noqa: E402
    GET_SCHEMA,
    LIST_SCHEMA,
    SEARCH_SCHEMA,
    XMemoMemoryProvider,
    register,
)


class MockPluginContext:
    def __init__(self):
        self.registered_provider = None
        self.registered_tools = {}

    def register_memory_provider(self, provider):
        self.registered_provider = provider

    def register_tool(self, name, toolset="xmemo", schema=None, handler=None, **kwargs):
        if handler is None and isinstance(toolset, dict):
            schema, handler = toolset, schema
            toolset = "xmemo"
        self.registered_tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
        }


class TestProactiveTools(unittest.TestCase):
    def setUp(self):
        self.provider = XMemoMemoryProvider()
        self.provider._config = {
            "api_key": "test-key",
            "base_url": "https://xmemo.dev",
            "enable_tools": True,
        }

    def test_schemas_included_in_core(self):
        schemas = self.provider.get_tool_schemas()
        names = [s["name"] for s in schemas]
        self.assertIn("xmemo_search", names)
        self.assertIn("xmemo_get", names)
        self.assertIn("xmemo_list", names)

    def test_get_schema_structure(self):
        self.assertEqual(GET_SCHEMA["name"], "xmemo_get")
        self.assertIn("memory_id", GET_SCHEMA["parameters"]["properties"])
        self.assertEqual(GET_SCHEMA["parameters"]["required"], ["memory_id"])

    def test_list_schema_structure(self):
        self.assertEqual(LIST_SCHEMA["name"], "xmemo_list")
        self.assertIn("path", LIST_SCHEMA["parameters"]["properties"])
        self.assertIn("limit", LIST_SCHEMA["parameters"]["properties"])

    def test_register_context_registers_tools_and_provider(self):
        ctx = MockPluginContext()
        with patch("hermes_xmemo.xmemo.load_config", return_value={"api_key": "test", "enable_tools": True}):
            register(ctx)

        self.assertIsNotNone(ctx.registered_provider)
        self.assertIn("xmemo_search", ctx.registered_tools)
        self.assertIn("xmemo_get", ctx.registered_tools)
        self.assertIn("xmemo_list", ctx.registered_tools)

    def test_register_context_skips_tools_when_disabled(self):
        ctx = MockPluginContext()
        with patch("hermes_xmemo.xmemo.load_config", return_value={"api_key": "test", "enable_tools": False}):
            register(ctx)

        self.assertIsNotNone(ctx.registered_provider)
        self.assertEqual(len(ctx.registered_tools), 0)

    @patch("hermes_xmemo.xmemo.XMemoMemoryProvider._get_client")
    def test_handle_tool_call_get_success(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_memory.return_value = {
            "id": "mem-123",
            "content": "Full untruncated content here",
            "path": "test/path",
            "created_at": "2026-09-20T00:00:00Z",
            "metadata": {"source": "user"},
            "memory_type": "semantic",
        }
        mock_get_client.return_value = mock_client

        raw_res = self.provider.handle_tool_call("xmemo_get", {"memory_id": "mem-123"})
        res = json.loads(raw_res)
        self.assertEqual(res["id"], "mem-123")
        self.assertEqual(res["content"], "Full untruncated content here")
        mock_client.get_memory.assert_called_once_with("mem-123")

    @patch("hermes_xmemo.xmemo.XMemoMemoryProvider._get_client")
    def test_handle_tool_call_get_missing_id(self, mock_get_client):
        raw_res = self.provider.handle_tool_call("xmemo_get", {})
        res = json.loads(raw_res)
        self.assertIn("error", res)
        self.assertIn("memory_id", res["error"])

    @patch("hermes_xmemo.xmemo.XMemoMemoryProvider._get_client")
    def test_handle_tool_call_list_success(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.list_memories.return_value = [
            {
                "id": "mem-1",
                "content": "First item content",
                "path": "projects/alpha",
                "created_at": "2026-09-20T00:00:00Z",
                "score": 0.95,
            }
        ]
        mock_get_client.return_value = mock_client

        raw_res = self.provider.handle_tool_call("xmemo_list", {"path": "projects/alpha", "limit": 10})
        res = json.loads(raw_res)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["items"][0]["id"], "mem-1")
        self.assertEqual(res["items"][0]["preview"], "First item content")
        mock_client.list_memories.assert_called_once()

    @patch("hermes_xmemo.xmemo.XMemoMemoryProvider._get_client")
    def test_registered_handlers_callable(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_memory.return_value = {"id": "m1", "content": "text"}
        mock_get_client.return_value = mock_client

        ctx = MockPluginContext()
        with patch("hermes_xmemo.xmemo.load_config", return_value={"api_key": "test", "enable_tools": True}):
            register(ctx)

        # Hermes invokes handler with (args) or kwargs
        get_handler = ctx.registered_tools["xmemo_get"]["handler"]
        result_json = get_handler({"memory_id": "m1"})
        res = json.loads(result_json)
        self.assertEqual(res["id"], "m1")


if __name__ == "__main__":
    unittest.main()
