#!/usr/bin/env bash
# office/uninstall.sh — remove the office skill symlink from ~/.claude/skills/
#
# Usage:
#   ./office/uninstall.sh
#
# Only removes the symlink if it points into this directory — leaves everything else untouched.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_NAME="$(basename "$SKILL_DIR")"
SKILLS_DIR="$HOME/.claude/skills"
target="$SKILLS_DIR/$SKILL_NAME"

if [[ -L "$target" ]]; then
  current="$(readlink "$target")"
  if [[ "$current" == "$SKILL_DIR" ]]; then
    rm "$target"
    echo "  removed  $target"
  else
    echo "  skipped  $SKILL_NAME (symlink points elsewhere: $current)"
  fi
elif [[ -e "$target" ]]; then
  echo "  skipped  $SKILL_NAME ($target is not a symlink — not touching it)"
else
  echo "  skipped  $SKILL_NAME (not installed)"
fi
