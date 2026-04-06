#!/usr/bin/env bash
# install.sh — symlink each skill in this repo into ~/.claude/skills/
#
# Usage:
#   ./install.sh
#
# For each subdirectory containing a SKILL.md with an "author:" field,
# creates a symlink: ~/.claude/skills/<skill> -> <repo>/<skill>
# Existing symlinks are updated; non-symlink directories are left untouched.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="$HOME/.claude/skills"

mkdir -p "$SKILLS_DIR"

linked=0
skipped=0

for skill_dir in "$REPO_DIR"/*/; do
  skill="$(basename "$skill_dir")"
  skill_md="$skill_dir/SKILL.md"

  # Only process dirs that have a SKILL.md with author: field
  [[ -f "$skill_md" ]] || continue
  grep -q "^author:" "$skill_md" || continue

  target="$SKILLS_DIR/$skill"

  if [[ -L "$target" ]]; then
    current="$(readlink "$target")"
    if [[ "$current" == "$skill_dir" || "$current" == "${skill_dir%/}" ]]; then
      echo "  ok       $skill (already linked)"
    else
      ln -sfn "$skill_dir" "$target"
      echo "  updated  $skill → $skill_dir"
    fi
    ((linked++))
  elif [[ -e "$target" ]]; then
    echo "  SKIPPED  $skill ($target exists and is not a symlink — remove it manually to install)"
    ((skipped++))
  else
    ln -s "$skill_dir" "$target"
    echo "  linked   $skill → $target"
    ((linked++))
  fi
done

echo ""
echo "$linked skill(s) installed, $skipped skipped."

if grep -rq "MS_CLIENT_ID" "$REPO_DIR"/*/scripts/*.py 2>/dev/null; then
  echo ""
  echo "The onenote and office skills require a Microsoft app Client ID."
  echo "Add to your shell profile (~/.zshrc or ~/.bashrc):"
  echo "  export MS_CLIENT_ID=\"your-azure-app-client-id\""
  echo ""
  echo "Then authenticate once:"
  echo "  python3 ~/.claude/skills/onenote/scripts/onenote_setup.py"
fi
