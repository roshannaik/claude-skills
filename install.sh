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
    linked=$((linked + 1))
  elif [[ -e "$target" ]]; then
    echo "  SKIPPED  $skill ($target exists and is not a symlink — remove it manually to install)"
    skipped=$((skipped + 1))
  else
    ln -s "$skill_dir" "$target"
    echo "  linked   $skill → $target"
    linked=$((linked + 1))
  fi
done

echo ""
echo "$linked skill(s) installed, $skipped skipped."

echo ""
echo "Environment variables each skill needs — add to ~/.zshrc, then: source ~/.zshrc"
echo ""
echo "  onenote           export MS_CLIENT_ID=...    export GEMINI_API_KEY=..."
echo "  office            export MS_CLIENT_ID=..."
echo "  code-maintenance  (none)"
echo ""
echo "  MS_CLIENT_ID    Azure app registration Client ID — https://portal.azure.com   (README.md §3)"
echo "  GEMINI_API_KEY  Google AI Studio key             — https://aistudio.google.com/apikey (README.md §4)"
echo ""
echo "First-time Microsoft auth (onenote + office share one token cache):"
echo "  python3 \"$REPO_DIR/onenote/scripts/onenote_setup.py\""
