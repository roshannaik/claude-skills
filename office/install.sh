#!/usr/bin/env bash
# office/install.sh — install the office skill into ~/.claude/skills/
#
# Usage:
#   ./office/install.sh
#
# Creates a symlink: ~/.claude/skills/office -> <this directory>
# Existing symlinks are updated; non-symlink paths are left untouched.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_NAME="$(basename "$SKILL_DIR")"
SKILLS_DIR="$HOME/.claude/skills"

mkdir -p "$SKILLS_DIR"
target="$SKILLS_DIR/$SKILL_NAME"

if [[ -L "$target" ]]; then
  current="$(readlink "$target")"
  if [[ "$current" == "$SKILL_DIR" ]]; then
    echo "  ok       $SKILL_NAME (already linked)"
  else
    ln -sfn "$SKILL_DIR" "$target"
    echo "  updated  $SKILL_NAME → $SKILL_DIR"
  fi
elif [[ -e "$target" ]]; then
  echo "  SKIPPED  $SKILL_NAME ($target exists and is not a symlink — remove it manually to install)"
  exit 1
else
  ln -s "$SKILL_DIR" "$target"
  echo "  linked   $SKILL_NAME → $target"
fi

echo ""
echo "Next steps:"
echo "  1. Set MS_CLIENT_ID in your shell profile (see README.md)"
echo "  2. Authenticate once via the onenote skill setup, or run:"
echo "     python3 -c \"import sys; sys.path.insert(0, '$target'); from office_ops import get_auth_token; get_auth_token()\""
