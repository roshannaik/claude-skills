# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Claude Code skill (`onenote`) that reads OneNote notebooks via Microsoft Graph and supports semantic search via Gemini embeddings. **Read-only by design** — write operations (`update_page`, `create_page`) have been removed from the skill. Installed as a symlink into `~/.claude/skills/onenote` by `install.sh`; edits to this repo take effect immediately — no build step.

Designed to be harness-agnostic: core logic is importable as plain Python modules (no Claude-Code-specific dependencies). The CLI is a thin wrapper; Codex or any other harness can `import` the modules directly.

`SKILL.md` is the consumer-facing contract (read by Claude when the skill is invoked). Keep it authoritative for usage patterns. `README.md` covers end-user setup. This file is for Claude when *modifying* the skill's code.

## Common commands

No build/lint/test infrastructure exists. Validate changes by running the CLI directly:

```bash
# Auth (first time only — device-code flow)
python3 scripts/onenote_setup.py

# Smoke-test the CLI
python3 scripts/onenote_ops.py list-notebooks
python3 scripts/onenote_ops.py search-title "<title keyword>"
python3 scripts/onenote_ops.py query "sleep supplements" --top-k 5

# (Re)build embeddings — incremental by default
python3 scripts/build_embeddings.py
python3 scripts/build_embeddings.py --force        # full rebuild
python3 scripts/build_embeddings.py --notebook Health AI
```

Required env vars:
- `MS_CLIENT_ID` — Azure app registration (`Notes.ReadWrite` scope is granted at the infra level; the skill uses read APIs only)
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) — Google AI Studio key for Gemini embeddings (build + query)

## Architecture

Six modules in `scripts/`, flat namespace. Heavy imports (`msgraph`, `msal`, `google-genai`, `numpy`) are deferred to first use so cache-only ops stay snappy.

| Module | Responsibility |
|---|---|
| `onenote_setup.py` | MSAL device-code auth, token cache at `~/.cache/ms_graph_token_cache.json`, thin Graph API wrappers. `make_graph_client()` is the entry point everything else imports. |
| `onenote_cache.py` | JSON cache (`onenote_cache.json`) with in-memory mtime cache, page index (`page_index.txt`), per-page HTML cache (`page_content/*.html` + `.meta`), lookup helpers, cache-update helpers with ID-based rename detection, `strip_html()`. |
| `onenote_api.py` | Graph API read ops — `get_notebooks`, `get_sections`, `get_pages`, `refresh_notebook`, `find_page`, `find_pages_batch`, `refresh_all_notebooks`. Freshness checks via `last_modified` to skip unchanged re-fetches. |
| `onenote_search.py` | `search_pages` (title grep) and `search_content` (HTML grep). Pure local, no API. |
| `onenote_embeddings.py` | Gemini embeddings build + query (`gemini-embedding-001`, MRL-truncated to 1024 dims). Stores `cache/embeddings.npz` (float32, L2-normalized) + `cache/embeddings_meta.json` (model, per-page `last_modified` for incremental rebuilds). Query path: single matmul, no vector DB. |
| `onenote_ops.py` | Thin CLI entry point. Re-exports everything from the above modules for backward compat with inline-Python usage (`from onenote_ops import find_page, ...`). |

`scripts/build_embeddings.py` is a standalone CLI for the embeddings build.

### Cache layout (`cache/`, gitignored)

- `onenote_cache.json` — single source of truth for notebook/section/page IDs + `last_modified`. Never read directly; always via `_load_cache()`. `_save_cache()` rewrites `page_index.txt` as a side-effect.
- `page_index.txt` — tab-separated `title\tnotebook\tsection\tpage_id`, rebuilt from the JSON. Powers fast title search without loading the 168KB JSON.
- `page_content/<safe_id>.html` + `.meta` — per-page HTML cache, invalidated when `.meta` (stores `last_modified`) no longer matches the JSON cache. `_content_path()` replaces `!` and `/` with `_` for filesystem safety.
- `embeddings.npz` — `{ids: (N,) string, vectors: (N, 1024) float32 L2-normalized}`. ~4 MB for ~1K pages.
- `embeddings_meta.json` — `{model, dim, built_at, pages: {page_id: {notebook, section, title, last_modified, text_len}}}`. Feeds both incremental rebuild (last_modified match) and query-time metadata join.

### Cache invariants to preserve

- Renames (section or page) must be detected by **ID**, not by name — see `update_sections_cache` / `update_pages_cache`. Existing entries are carried forward by matching IDs against prior state.
- Embedding vectors are **L2-normalized at build time** so cosine similarity reduces to a single dot product. If you add new vectors without normalizing, scores become meaningless.
- Read-only policy: do not reintroduce `update_page` / `create_page` / `_patch_page_content` / container-setter helpers. The skill is intentionally restricted to reads.

### Harness portability

- No subprocess calls to `claude` anywhere. Embeddings use the Google GenAI SDK directly, not a Claude CLI.
- No UNIX daemon, no `/tmp/*.sock` state. Previous design had one — removed to keep behavior identical across harnesses.
- CLI paths are hardcoded to `~/.claude/skills/onenote/cache/` for now; revisit if another harness needs a different cache root.

## Obsolete context

`onenote_redesign_context.md` (untracked) captured an earlier proposal for tiered Haiku summaries. That approach was superseded by Voyage embeddings — the summary system and its CLI (`routing-index`, `build_summaries.py`) have been removed. The doc is kept only as a reference to the decision history; don't build against it.
