"""Allow ``python -m hermes_xmemo install``."""

from __future__ import annotations

import sys

from hermes_xmemo.install import main

if __name__ == "__main__":
    sys.exit(main())
