# Code Maintenance Skill

Holistic audit and cleanup of code and artifacts (scripts, docs, configs) that have grown incrementally. Finds bugs introduced by stitching, dead code, duplication, stale docs, and readability issues — without changing behavior or efficiency.

---

## Prerequisites

None. This skill requires no external tools, APIs, or environment variables beyond Claude Code itself.

---

## Installation

```bash
git clone https://github.com/roshannaik/claude-skills.git
cd claude-skills
./code-maintenance/install.sh
```

This creates a symlink `~/.claude/skills/code-maintenance` pointing to the cloned repo directory. No files are copied — edits in the repo are reflected immediately, and `git pull` is all you need to update.

To uninstall:

```bash
./code-maintenance/uninstall.sh
```

---

## Usage

Once installed, invoke in Claude Code with an optional path argument:

```
/code-maintenance                          # audit current working directory
/code-maintenance path/to/dir             # audit a specific directory (recursive)
/code-maintenance path/to/file.py         # audit a single file
/code-maintenance file1.py file2.py README.md   # audit specific files
```

Or describe what you want in natural language — Claude Code will invoke the skill automatically.

---

## What it does

The skill works in five steps:

1. **Read** all files in scope before forming any opinion
2. **Audit** by category: bugs from stitching (A), dead code (B), duplication (C), stale docs (D), simplification opportunities (E), readability noise (F)
3. **Report** findings grouped by category with file and line number before making any changes
4. **Fix** in priority order (A → B → C → D → E → F), asking for confirmation only on bug-category changes
5. **Verify** each modified file after edits and summarize what changed

Behavior preservation is the highest priority — every change leaves observable behavior identical.
