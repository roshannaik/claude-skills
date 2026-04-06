---
name: code-maintenance
description: Holistic audit and cleanup of code and artifacts (scripts, docs, configs) that have grown incrementally. Finds bugs introduced by stitching, dead code, duplication, stale docs, and readability issues — without changing behavior or efficiency.
argument-hint: 'code-maintenance ~/.claude/skills/onenote, code-maintenance src/api.py + SKILL.md'
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
author: clawdi
---

# Cleanup Skill

For codebases that grow conversation-by-conversation. Each increment is fine in isolation but drift accumulates: wrong function names from renaming, dead imports from rewrites, duplicate helpers that were never merged, stale docs that describe yesterday's code.

## Scope

The skill accepts an optional argument:

| Invocation | Scope |
|---|---|
| `/cleanup` | Current working directory and all subdirectories |
| `/cleanup path/to/dir` | That directory and all subdirectories (recursive) |
| `/cleanup path/to/file.py` | That single file only |
| `/cleanup file1.py file2.py SKILL.md` | Exactly those files |

If no argument is given, default to the current working directory.

When scope is a directory, include **all** readable files recursively — scripts, docs, configs, fixtures, any hand-maintained artifacts. Skip binary files, `__pycache__`, `.git`, and build/output directories (`dist/`, `node_modules/`, `*.egg-info/`, etc.).

---

## Step 1 — Read everything

Read **all** files in scope before forming any opinion. Don't triage on filenames alone — bugs often live in the file you'd least expect.

- Scripts (`.py`, `.js`, `.ts`, etc.)
- Docs / SKILL.md / README
- Config / JSON artifacts
- Any generated index or cache files that are hand-maintained

For large files (>300 lines), read in chunks. Don't skip sections.

---

## Step 2 — Audit by category

Work through each category. Collect **all findings** before fixing anything.

### A — Bugs from stitching
Code that references something that was renamed, moved, or made private during incremental edits.

- Wrong function name (public vs private, old vs new name)
- Wrong module/path reference
- Incorrect argument (wrong key, wrong field name from old schema)
- `_` prefix inconsistency (function defined private, called as public or vice versa)

### B — Dead code
Code that is imported, defined, or configured but never actually used.

- Unused imports
- Functions defined but never called
- Variables assigned but never read
- `async def` with no `await` inside (can be made sync, or `async` removed)
- `lambda x: f(x)` instead of just `f`
- Redundant intermediate variable (`x = y; return x` → `return y`)

### C — Duplication
Two things doing the same job.

- Two functions with identical or near-identical bodies (one should delegate to the other)
- Copy-pasted logic that could be a shared helper
- Repeated string literals that should be constants

### D — Stale documentation
Docs that describe code that no longer exists, or code that exists but isn't documented.

- SKILL.md examples using old function names, old paths, old CLI flags
- Docstrings that describe old parameter names or return shapes
- Comments referencing behavior that changed
- Missing docs for new functions added without updating README/SKILL.md

### E — Simplification opportunities
Equivalent code that can be expressed more simply without affecting behavior.

- Overly complex expressions where simpler ones are identical (`x if x else None` → `x or None`)
- Multi-step operations that collapse to one (`a = f(); return a` → `return f()`)
- Function that delegates through a chain of wrappers when a direct call would suffice
- Conditional that can be replaced by `or` / `and` short-circuit

Only apply when the simplified form is **provably identical** in all cases — not just the common path.

### F — Readability noise
Accumulated clutter that doesn't affect behavior.

- Multiple consecutive blank lines (more than 2)
- Dead comments (`# TODO` that was resolved, `# removed`, etc.)
- Inconsistent style in a single file (e.g., some section headers use `---`, others don't)

---

## Step 3 — Report findings

Before making any change, output a **short inventory** grouped by category. Be specific: file, line number, what's wrong.

Format:

```
A — Bugs
  onenote_ops.py:537  refresh_all_notebooks calls load_cache() — should be _load_cache()

B — Dead code
  office_ops.py:14    import asyncio — unused (main_async has no awaits)
  office_ops.py:215   excel_list_sheets duplicates excel_list_sheets_with_ids body

C — Duplication
  (none)

D — Stale docs
  SKILL.md:42   inline Python example imports from wrong path

E — Simplification
  office_ops.py:440  max_rows = args.max_rows if args.max_rows else None → args.max_rows or None

F — Readability
  onenote_ops.py:737  three blank lines
```

If a category has nothing, say `(none)` — don't omit it. Omitting makes the user wonder if you checked.

Ask for confirmation before fixing **only if** there are A (bug) findings — those carry risk. For B–E, proceed directly.

---

## Step 4 — Fix in priority order

1. **A first** — bugs break things
2. **B next** — dead code removal is safe and makes the rest easier to assess
3. **C** — merge duplicates (always make the simpler delegate to the richer one, not the other way)
4. **D** — update docs to match the code you just fixed
5. **E** — simplifications, only where provably identical
6. **F** — readability noise last

For each fix:
- Use Edit (not Write) unless a full rewrite is clearly better
- One logical change per Edit call — don't bundle unrelated fixes
- Never change behavior, signatures, or efficiency as a side effect of cleanup

---

## Step 5 — Final check

After all edits:

1. Re-read each modified file to confirm no accidental changes slipped in
2. Grep for any remaining references to the old name/path if you renamed something
3. If the scope contains any tests (files matching `test_*`, `*_test.*`, `tests/`, `spec/`, etc.), ask the user whether to run them before declaring done
4. State concisely what was changed and why — one line per fix

---

## Rules

- **Behavior preservation is the highest priority.** Every change must leave observable behavior identical — same outputs, same side effects, same error conditions, same performance characteristics.
- **When in doubt, skip it.** If you cannot confidently assert that a change is behavior-neutral, flag it in the report and leave the code untouched. Do not apply the change speculatively.
- **Tie-breaker: always choose the option that more obviously preserves behavior**, even if the alternative looks cleaner. Readability is a nice-to-have; correctness is not.
- **Never add** features, error handling, logging, or comments to code you didn't touch.
- **Never reformat** code that wasn't already the focus of a fix (don't reindent a whole file to fix one line).
- **Stale docs**: update to match the current code — don't "improve" the docs beyond what's needed to make them accurate.
- If scope is large (10+ files), break into multiple passes — one category at a time.
