#!/usr/bin/env bash
# uninstall.sh — remove symlinks created by install.sh
#
# Usage:
#   ./uninstall.sh
#
# Only removes symlinks that point into this repo — leaves everything else untouched.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="$HOME/.claude/skills"

removed=0
skipped=0

for skill_dir in "$REPO_DIR"/*/; do
  skill="$(basename "$skill_dir")"
  target="$SKILLS_DIR/$skill"

  if [[ -L "$target" ]]; then
    current="$(readlink "$target")"
    if [[ "$current" == "$skill_dir" || "$current" == "${skill_dir%/}" ]]; then
      rm "$target"
      echo "  removed  $target"
      ((removed++))
    else
      echo "  skipped  $skill (symlink points elsewhere: $current)"
      ((skipped++))
    fi
  elif [[ -e "$target" ]]; then
    echo "  skipped  $skill ($target is not a symlink — not touching it)"
    ((skipped++))
  fi
done

echo ""
echo "$removed skill(s) removed, $skipped skipped."
