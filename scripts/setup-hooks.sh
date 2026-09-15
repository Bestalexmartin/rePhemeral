#!/usr/bin/env bash
# Install rePhemeral's git hooks. Run once after cloning.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# --git-path answers relative to the -C directory, while mkdir and ln below
# resolve against the current one. Without an absolute path, running this
# from outside the repository installs the hook into whichever repository
# the shell is standing in.
hooks="$(git -C "$root" rev-parse --path-format=absolute --git-path hooks)"
mkdir -p "$hooks"
ln -sf ../../scripts/pre-commit "$hooks/pre-commit"
echo "Installed: pre-commit -> scripts/pre-commit"
if [ ! -L "$hooks/pre-commit" ]; then
  # Git Bash on Windows copies rather than links unless native symlinks
  # are enabled, and a copy does not follow later edits to the script.
  echo "NOTE: this system copied the hook rather than linking it. Re-run this" >&2
  echo "script after scripts/pre-commit changes, or the old copy keeps running." >&2
fi
if ! command -v gitleaks >/dev/null 2>&1; then
  echo "WARNING: gitleaks is not installed. The hook is fail-closed, so commits" >&2
  echo "will be blocked until you install it:" >&2
  echo "  sudo apt install gitleaks          (Debian, Ubuntu)" >&2
  echo "  brew install gitleaks              (macOS)" >&2
  echo "  winget install Gitleaks.Gitleaks   (Windows)" >&2
fi
