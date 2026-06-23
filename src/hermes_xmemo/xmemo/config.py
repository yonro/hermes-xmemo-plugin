"""XMemo provider configuration loader.

Reads settings from (highest to lowest priority):
  1. Environment variables: XMEMO_KEY, MEMORY_OS_API_KEY
  2. $HERMES_HOME/xmemo.json for non-secret values only

Secrets (api_key) are NEVER read from xmemo.json. If a stale api_key is found
in the file it is ignored and will be removed on the next save_config().
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://xmemo.dev"
DEFAULT_BUCKET = "work"
DEFAULT_SCOPE = "hermes/default"
DEFAULT_READ_BUCKET = "%"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_PREFETCH_MAX_ITEMS = 5
DEFAULT_PREFETCH_MAX_TOKENS = 900


def _config_path() -> Path:
    return get_hermes_home() / "xmemo.json"


def _default_agent_instance_id() -> str:
    """Generate a stable, opaque, non-reversible install identifier."""
    return uuid.uuid4().hex


def _load_file_cfg() -> Dict[str, Any]:
    """Read non-secret config from xmemo.json."""
    cfg_path = _config_path()
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("Failed to read %s: %s", cfg_path, exc)
        return {}

    # Defensive: if someone previously wrote a secret into this file, drop it
    # in memory and schedule cleanup on next save.
    if "api_key" in data:
        logger.warning(
            "xmemo.json contained an api_key; it has been ignored. "
            "Use the XMEMO_KEY environment variable or run 'hermes memory setup xmemo'."
        )
        data = {k: v for k, v in data.items() if k != "api_key"}
    return data


def load_config(*, create_instance: bool = False) -> Dict[str, Any]:
    """Load XMemo provider configuration.

    Args:
        create_instance: If True, generate and persist a missing
            ``agent_instance_id``. Callers that only *read* config (e.g.
            ``is_available()``) should pass False to avoid side effects.
    """
    file_cfg = _load_file_cfg()

    api_key = os.environ.get("XMEMO_KEY") or os.environ.get("MEMORY_OS_API_KEY", "")

    base_url = (
        os.environ.get("XMEMO_URL")
        or os.environ.get("MEMORY_OS_URL")
        or file_cfg.get("base_url", "")
        or DEFAULT_BASE_URL
    )

    agent_id = os.environ.get("XMEMO_AGENT_ID") or file_cfg.get("agent_id", "hermes")

    agent_instance_id = (
        os.environ.get("XMEMO_AGENT_INSTANCE_ID") or file_cfg.get("agent_instance_id", "")
    )

    bucket = os.environ.get("XMEMO_BUCKET") or file_cfg.get("bucket", DEFAULT_BUCKET)
    scope = os.environ.get("XMEMO_SCOPE") or file_cfg.get("scope", DEFAULT_SCOPE)
    read_bucket = (
        os.environ.get("XMEMO_READ_BUCKET")
        or file_cfg.get("read_bucket")
        or DEFAULT_READ_BUCKET
    )
    read_scope = os.environ.get("XMEMO_READ_SCOPE")
    if read_scope is None:
        read_scope = file_cfg.get("read_scope")

    timeout = file_cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if "XMEMO_TIMEOUT_SECONDS" in os.environ:
        try:
            timeout = float(os.environ["XMEMO_TIMEOUT_SECONDS"])
        except ValueError:
            pass

    prefetch_max_items = file_cfg.get("prefetch_max_items", DEFAULT_PREFETCH_MAX_ITEMS)
    if "XMEMO_PREFETCH_MAX_ITEMS" in os.environ:
        try:
            prefetch_max_items = int(os.environ["XMEMO_PREFETCH_MAX_ITEMS"])
        except ValueError:
            pass

    prefetch_max_tokens = file_cfg.get("prefetch_max_tokens", DEFAULT_PREFETCH_MAX_TOKENS)
    if "XMEMO_PREFETCH_MAX_TOKENS" in os.environ:
        try:
            prefetch_max_tokens = int(os.environ["XMEMO_PREFETCH_MAX_TOKENS"])
        except ValueError:
            pass

    enable_workflow_tools = file_cfg.get("enable_workflow_tools", False)
    enable_destructive_tools = file_cfg.get("enable_destructive_tools", False)
    capture_timeline = file_cfg.get("capture_timeline", False)

    config = {
        "api_key": api_key,
        "base_url": base_url,
        "agent_id": agent_id,
        "agent_instance_id": agent_instance_id,
        "bucket": bucket,
        "scope": scope,
        "read_bucket": read_bucket,
        "read_scope": read_scope,
        "timeout_seconds": timeout,
        "prefetch_max_items": prefetch_max_items,
        "prefetch_max_tokens": prefetch_max_tokens,
        "enable_workflow_tools": enable_workflow_tools,
        "enable_destructive_tools": enable_destructive_tools,
        "capture_timeline": capture_timeline,
    }

    if create_instance and not config["agent_instance_id"]:
        config["agent_instance_id"] = _default_agent_instance_id()
        try:
            save_config(config)
        except Exception as exc:
            logger.debug("Failed to persist generated agent_instance_id: %s", exc)

    return config


def save_config(values: Dict[str, Any], hermes_home: Optional[str] = None) -> None:
    """Persist non-secret XMemo config to $HERMES_HOME/xmemo.json."""
    if hermes_home:
        cfg_path = Path(hermes_home) / "xmemo.json"
    else:
        cfg_path = _config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    existing: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass

    # Never write secrets to disk; also remove any stale secret that may exist.
    safe_values = {k: v for k, v in values.items() if k != "api_key"}
    existing = {k: v for k, v in existing.items() if k != "api_key"}
    existing.update(safe_values)

    cfg_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
