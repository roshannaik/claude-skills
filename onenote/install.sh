#!/usr/bin/env bash
# onenote/install.sh — install the onenote skill into ~/.claude/skills/
#
# Usage:
#   ./onenote/install.sh
#
# Creates a symlink: ~/.claude/skills/onenote -> <this directory>
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

# ---------------------------------------------------------------------------
# Report which required env vars are missing and where to put them.
# Only checks the live shell env — if a var is set somewhere but not exported
# to this shell, we'll still flag it, which is the safer default.
# ---------------------------------------------------------------------------

case "${SHELL:-}" in
  *zsh)  profile="~/.zshrc"  ;;
  *bash) profile="~/.bashrc" ;;
  *)     profile="your shell profile" ;;
esac

missing=()
[[ -z "${MS_CLIENT_ID:-}"   ]] && missing+=("MS_CLIENT_ID")
[[ -z "${GEMINI_API_KEY:-}" && -z "${GOOGLE_API_KEY:-}" ]] && missing+=("GEMINI_API_KEY")

if (( ${#missing[@]} > 0 )); then
  echo "Missing required environment variables:"
  for v in "${missing[@]}"; do
    case "$v" in
      MS_CLIENT_ID)
        echo "  - MS_CLIENT_ID    Azure app registration Client ID (for Microsoft Graph / OneNote access)"
        echo "                    Get one at https://portal.azure.com — see README.md §3"
        ;;
      GEMINI_API_KEY)
        echo "  - GEMINI_API_KEY  Google AI Studio API key (for semantic search embeddings)"
        echo "                    Get one free at https://aistudio.google.com/apikey — see README.md §4"
        ;;
    esac
  done
  echo ""
  echo "Add them to $profile:"
  for v in "${missing[@]}"; do
    echo "  export $v=\"...\""
  done
  echo ""
  echo "Then reload: source $profile"
  echo ""
fi

echo "Next steps:"
step=1
if (( ${#missing[@]} > 0 )); then
  echo "  $step. Set the environment variables shown above"
  step=$((step + 1))
fi
echo "  $step. Authenticate with Microsoft: python3 \"$target/scripts/onenote_setup.py\""
step=$((step + 1))
echo "  $step. Build the semantic-search index: python3 \"$target/scripts/build_embeddings.py\""
step=$((step + 1))
echo "  $step. (optional) Schedule a background sync — see README.md §'Keep the cache fresh'"
