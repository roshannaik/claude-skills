#!/usr/bin/env bash
# code-maintenance/install.sh — install the code-maintenance skill into ~/.claude/skills/
#
# Usage:
#   ./code-maintenance/install.sh
#
# Creates a symlink: ~/.claude/skills/code-maintenance -> <this directory>
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
echo "  code-maintenance is ready. Use /code-maintenance in Claude Code."
