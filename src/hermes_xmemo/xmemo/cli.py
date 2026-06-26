"""Setup wizard for the XMemo memory provider.

Invoked by ``hermes memory setup xmemo`` via the provider's ``post_setup()``
hook. Collects credentials and preferences, then writes:
  - config.yaml  → memory.provider = xmemo
  - .env         → XMEMO_KEY
  - xmemo.json   → non-secret provider settings
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict

from hermes_cli.secret_prompt import masked_secret_prompt


def _prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    """Prompt for a value with optional default and secret masking."""
    suffix = f" [{default}]" if default else ""
    if secret:
        val = masked_secret_prompt(f"  {label}{suffix}: ")
    else:
        sys.stdout.write(f"  {label}{suffix}: ")
        sys.stdout.flush()
        val = sys.stdin.readline().strip()
    return val or (default or "")


def _curses_select(title: str, choices: list[str], default: int = 0) -> int:
    """Interactive single-select for choice fields."""
    try:
        from hermes_cli.curses_ui import curses_radiolist
        return curses_radiolist(title, choices, selected=default, cancel_returns=default)
    except Exception:
        # Fallback: print numbered list and read stdin
        print(f"\n  {title}")
        for i, choice in enumerate(choices):
            marker = ">" if i == default else " "
            print(f"    {marker} {i + 1}. {choice}")
        sys.stdout.write("  Select (number): ")
        sys.stdout.flush()
        try:
            idx = int(sys.stdin.readline().strip()) - 1
            if 0 <= idx < len(choices):
                return idx
        except ValueError:
            pass
        return default


def _write_env_vars(env_path: Path, env_writes: dict) -> None:
    """Append or update env vars in .env file."""
    env_path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        key_match = line.split("=", 1)[0].strip() if "=" in line else ""
        if key_match in env_writes:
            new_lines.append(f"{key_match}={env_writes[key_match]}")
            updated_keys.add(key_match)
        else:
            new_lines.append(line)

    for key, val in env_writes.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _run_schema_setup(
    provider: Any,
    hermes_home: str,
    config: Dict[str, Any],
) -> None:
    """Walk the provider's config schema and persist answers."""
    from hermes_constants import get_hermes_home
    from hermes_cli.config import load_config, save_config as save_global_config

    schema = provider.get_config_schema() if hasattr(provider, "get_config_schema") else []

    provider_name = provider.name if hasattr(provider, "name") else "xmemo"
    if not isinstance(config.get("memory"), dict):
        config["memory"] = {}

    provider_config = config["memory"].get(provider_name, {})
    if not isinstance(provider_config, dict):
        provider_config = {}

    env_path = get_hermes_home() / ".env"
    env_writes: Dict[str, str] = {}

    if schema:
        print(f"\n  Configuring XMemo:\n")
        for field in schema:
            key = field["key"]
            desc = field.get("description", key)
            default = field.get("default")
            is_secret = field.get("secret", False)
            choices = field.get("choices")
            env_var = field.get("env_var")
            url = field.get("url")

            when = field.get("when")
            if when and isinstance(when, dict):
                if not all(provider_config.get(k) == v for k, v in when.items()):
                    continue

            if choices and not is_secret:
                current = provider_config.get(key, default)
                current_idx = 0
                if current and current in choices:
                    current_idx = choices.index(current)
                sel = _curses_select(f"  {desc}", choices, default=current_idx)
                provider_config[key] = choices[sel]
            elif is_secret:
                existing = os.environ.get(env_var, "") if env_var else ""
                existing_source = f"env:{env_var}" if existing and env_var else ""
                if not existing:
                    try:
                        from .config import _resolve_api_key

                        existing, existing_source = _resolve_api_key()
                    except Exception:
                        existing, existing_source = "", ""
                if existing:
                    masked = f"...{existing[-4:]}" if len(existing) > 4 else "set"
                    source_label = (
                        "shared XMemo login"
                        if existing_source == "shared-credential"
                        else existing_source or "configured"
                    )
                    val = _prompt(
                        f"{desc} (current: {masked} from {source_label}, blank to keep)",
                        secret=True,
                    )
                else:
                    if url:
                        print(f"  Get your XMemo token at {url}")
                    val = _prompt(desc, secret=True)
                if val and env_var:
                    env_writes[env_var] = val
            else:
                current = provider_config.get(key)
                effective_default = current or default
                val = _prompt(
                    desc,
                    default=str(effective_default) if effective_default else None,
                )
                if val:
                    provider_config[key] = val
                    if env_var and env_var not in env_writes:
                        env_writes[env_var] = val

    config["memory"]["provider"] = provider_name

    # Merge current provider_config into the global memory.<provider> block so
    # the non-secret values are also visible in config.yaml (in addition to the
    # native xmemo.json written by save_config).
    existing_memory = config.setdefault("memory", {})
    existing_provider_cfg = existing_memory.get(provider_name, {})
    if isinstance(existing_provider_cfg, dict):
        existing_provider_cfg.update(provider_config)
        existing_memory[provider_name] = existing_provider_cfg

    save_global_config(config)

    if hasattr(provider, "save_config"):
        try:
            provider.save_config(provider_config, hermes_home)
        except Exception as exc:
            print(f"  Failed to write XMemo config: {exc}")

    if env_writes:
        _write_env_vars(env_path, env_writes)

    print(f"\n  Memory provider: {provider_name}")
    print(f"  Activation saved to config.yaml")
    print(f"  Provider config saved to {hermes_home}/xmemo.json")
    if env_writes:
        print(f"  API key saved to {hermes_home}/.env")
    print(f"\n  Start a new session to activate.\n")


def cmd_setup(provider: Any | None = None, hermes_home: str = "", config: Dict[str, Any] | None = None) -> None:
    """Entry point for the XMemo setup wizard.

    Called with a provider instance when invoked from ``post_setup()``.
    """
    from hermes_constants import get_hermes_home

    if provider is None:
        from . import XMemoMemoryProvider
        provider = XMemoMemoryProvider()

    if not hermes_home:
        hermes_home = str(get_hermes_home())

    if config is None:
        from hermes_cli.config import load_config
        config = load_config()

    _run_schema_setup(provider, hermes_home, config)
