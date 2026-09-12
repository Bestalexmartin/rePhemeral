#!/usr/bin/env bash
# Install rePhemeral's git hooks. Run once after cloning.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hooks="$(git -C "$root" rev-parse --git-path hooks)"
mkdir -p "$hooks"
ln -sf ../../scripts/pre-commit "$hooks/pre-commit"
echo "Installed: pre-commit -> scripts/pre-commit"
if ! command -v gitleaks >/dev/null 2>&1; then
  echo "WARNING: gitleaks is not installed. The hook is fail-closed, so commits" >&2
  echo "will be blocked until you install it:" >&2
  echo "  sudo apt install gitleaks   (Debian, Ubuntu)" >&2
  echo "  brew install gitleaks       (macOS)" >&2
fi
