"""XMemo Hermes memory provider — pip-installable package.

This package does not run inside Hermes directly.  It ships the plugin files
and provides a small CLI that copies them into the user's Hermes plugins
directory, which is the only location Hermes scans for memory providers.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
