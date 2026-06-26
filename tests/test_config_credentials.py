"""Credential resolution tests for Hermes/XMemo compatibility."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

hc_mod = type(sys)("hermes_constants")
hc_mod.get_hermes_home = lambda: Path("/fake/hermes/home")
sys.modules.setdefault("hermes_constants", hc_mod)

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

from hermes_xmemo.xmemo.config import load_config


class TestCredentialResolution(unittest.TestCase):
    def _env(self, **extra: str) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("XMEMO_")
            and key not in {"MEMORY_OS_API_KEY", "MEMORY_OS_MCP_TOKEN", "MEMORY_OS_CONFIG_HOME"}
        }
        env.update(extra)
        return env

    def test_xmemo_key_env_takes_priority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "credentials.json"
            credential_path.write_text(
                json.dumps({"token": "shared-token-should-not-win"}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                self._env(XMEMO_CONFIG_HOME=temp_dir, XMEMO_KEY="env-token-wins"),
                clear=True,
            ):
                cfg = load_config()

        self.assertEqual(cfg["api_key"], "env-token-wins")
        self.assertEqual(cfg["credential_source"], "env:XMEMO_KEY")

    def test_legacy_memory_os_api_key_still_works(self):
        with patch.dict(
            os.environ,
            self._env(MEMORY_OS_API_KEY="legacy-api-key"),
            clear=True,
        ):
            cfg = load_config()

        self.assertEqual(cfg["api_key"], "legacy-api-key")
        self.assertEqual(cfg["credential_source"], "env:MEMORY_OS_API_KEY")

    def test_shared_xmemo_client_credential_is_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "credentials.json"
            credential_path.write_text(
                json.dumps({
                    "version": 1,
                    "storage": "user-scoped-credential-file",
                    "token": "shared-login-token",
                }),
                encoding="utf-8",
            )
            with patch.dict(os.environ, self._env(XMEMO_CONFIG_HOME=temp_dir), clear=True):
                cfg = load_config()

        self.assertEqual(cfg["api_key"], "shared-login-token")
        self.assertEqual(cfg["credential_source"], "shared-credential")


if __name__ == "__main__":
    unittest.main(verbosity=2)
