"""Install the XMemo plugin into the user's Hermes plugins directory."""

from __future__ import annotations

import argparse
import importlib.resources
import os
import shutil
import sys
from pathlib import Path


def get_hermes_home() -> Path:
    """Return the Hermes home directory.

    Honors ``HERMES_HOME``; falls back to ``~/.hermes``.
    """
    home = os.environ.get("HERMES_HOME", "")
    if home:
        return Path(home).expanduser().resolve()
    return Path.home() / ".hermes"


def install_plugin(hermes_home: Path | None = None, dry_run: bool = False) -> Path:
    """Copy the bundled XMemo plugin files into ``$HERMES_HOME/plugins/xmemo/``.

    Args:
        hermes_home: Override the Hermes home directory.  If ``None``, resolves
            via ``HERMES_HOME`` / ``~/.hermes``.
        dry_run: When ``True``, print what would happen without copying.

    Returns:
        The destination plugin directory.
    """
    target_home = hermes_home if hermes_home is not None else get_hermes_home()
    dest = target_home / "plugins" / "xmemo"
    src = importlib.resources.files(__package__) / "xmemo"

    if dry_run:
        print(f"Would copy {src} -> {dest}")
        return dest

    if not src.is_dir():
        raise RuntimeError(f"Plugin source directory not found in package: {src}")

    # Remove any previous install to avoid stale / nested files.
    if dest.exists():
        shutil.rmtree(dest)

    shutil.copytree(src, dest)

    if not (dest / "__init__.py").exists():
        raise RuntimeError(f"Installation appears incomplete: {dest}")

    return dest


def main(args: list[str] | None = None) -> int:
    """CLI entry point for ``hermes-xmemo install``."""
    parser = argparse.ArgumentParser(
        prog="hermes-xmemo",
        description="Install the XMemo memory provider plugin for Hermes Agent.",
    )
    parser.add_argument(
        "command",
        choices=["install"],
        help="Command to run.  Only 'install' is supported.",
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=None,
        help="Path to the Hermes home directory (default: $HERMES_HOME or ~/.hermes).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be copied without making changes.",
    )

    parsed = parser.parse_args(args)

    try:
        dest = install_plugin(hermes_home=parsed.hermes_home, dry_run=parsed.dry_run)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if parsed.dry_run:
        print("Dry run complete.")
    else:
        print(f"Installed XMemo plugin to {dest}")
        print("Run 'hermes memory setup xmemo' to configure.")
    return 0
