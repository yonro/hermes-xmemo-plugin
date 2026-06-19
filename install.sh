#!/usr/bin/env bash
# Install the XMemo memory provider plugin into $HERMES_HOME/plugins/xmemo

set -euo pipefail

HERMES_HOME_ORIGINAL="${HERMES_HOME:-}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST="$HERMES_HOME/plugins/xmemo"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Prefer the plugin files inside the pip package layout if it exists.
if [ -d "$SCRIPT_DIR/src/hermes_xmemo/xmemo" ]; then
    SRC="$SCRIPT_DIR/src/hermes_xmemo/xmemo"
elif [ -d "$SCRIPT_DIR/xmemo" ]; then
    SRC="$SCRIPT_DIR/xmemo"
else
    echo "Error: plugin source directory not found" >&2
    echo "Run this script from the root of the hermes-xmemo-plugin repo." >&2
    exit 1
fi

# Warn when running under WSL without an explicit HERMES_HOME, because the
# default $HOME/.hermes may point to the WSL filesystem rather than the
# Windows Hermes home that the user actually uses.
if [ -z "$HERMES_HOME_ORIGINAL" ] && [ -n "${WSL_DISTRO_NAME:-}" ]; then
    echo "Warning: WSL detected and HERMES_HOME is not set." >&2
    echo "  Default install location: $DEST" >&2
    echo "  If your Windows Hermes home is elsewhere, set HERMES_HOME and re-run." >&2
fi

echo "Installing XMemo memory provider plugin to $DEST"

# Ensure the parent plugins directory exists before copying.
mkdir -p "$(dirname "$DEST")"

# Atomic-ish replacement: build in a temp directory, then swap.
DEST_TMP="${DEST}.tmp.$$"
rm -rf "$DEST_TMP"
cp -a "$SRC" "$DEST_TMP"

# Remove old install (including hidden files) and swap in the new one.
rm -rf "$DEST"
mv "$DEST_TMP" "$DEST"

if [ ! -f "$DEST/__init__.py" ]; then
    echo "Error: installation appears incomplete ($DEST/__init__.py missing)" >&2
    exit 1
fi

echo "Installed to $DEST"
echo "Run 'hermes memory setup xmemo' to configure."
