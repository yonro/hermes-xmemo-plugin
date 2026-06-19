#!/usr/bin/env bash
set -euo pipefail

# One-liner installer for the XMemo Hermes memory provider plugin.
# Downloads the latest plugin source from GitHub and runs the local install.sh.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/yonro/hermes-xmemo-plugin/main/install-remote.sh | bash

REPO_URL="https://github.com/yonro/hermes-xmemo-plugin.git"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "Downloading XMemo Hermes plugin..."
git clone --depth 1 "$REPO_URL" "$TMP_DIR/hermes-xmemo-plugin"

cd "$TMP_DIR/hermes-xmemo-plugin"
bash install.sh
