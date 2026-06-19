"""XMemo Hermes memory provider — pip-installable package.

This package does not run inside Hermes directly.  It ships the plugin files
and provides a small CLI that copies them into the user's Hermes plugins
directory, which is the only location Hermes scans for memory providers.
"""

from __future__ import annotations

try:
    from importlib.metadata import version
    __version__ = version("hermes-xmemo")
except Exception:  # pragma: no cover
    try:
        from hermes_xmemo._version import __version__
    except Exception:
        __version__ = "unknown"

__all__ = ["__version__"]
