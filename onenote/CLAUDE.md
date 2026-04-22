# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Claude Code skill (`onenote`) that reads OneNote notebooks via Microsoft Graph and supports chunked, multimodal semantic search via Gemini embeddings. **Read-only by design** — write operations (`update_page`, `create_page`) have been removed. Installed as a symlink into `~/.claude/skills/onenote` by `install.sh`; edits to this repo take effect immediately — no build step.

Harness-agnostic: core logic is importable as plain Python modules (no Claude-Code-specific dependencies). The CLI is a thin wrapper; Codex or any other harness can `import` the modules directly or shell out to `onenote_ops.py`.

Three docs have distinct audiences — don't conflate them:
- `SKILL.md` — consumer contract, read by Claude when the skill is invoked. Keep authoritative for usage patterns / decision rules.
- `README.md` — end-user setup, CLI reference, composability patterns.
- `CLAUDE.md` (this file) — for Claude when *modifying* the skill's code.

## Common commands

No build/lint/test infrastructure. Validate changes by running the CLI directly:

```bash
# Auth (first time only — device-code flow)
python3 scripts/onenote_setup.py

# Smoke-test the CLI (cache-only ops — no auth needed after first cache fill)
python3 scripts/onenote_ops.py list-notebooks
python3 scripts/onenote_ops.py search-title "<title keyword>"
python3 scripts/onenote_ops.py query "<natural-language query>" --top-k 5
python3 scripts/onenote_ops.py query "<q>" --format json --with-text --explain
python3 scripts/onenote_ops.py query-by-page "Notebook / Section / Title" --top-k 5
python3 scripts/onenote_ops.py get-chunk "<page_id>#t0001" --format json

# Full ingest pipeline (incremental; each step carries forward unchanged items)
python3 scripts/onenote_ops.py fetch-media --all     # raw bytes + OCR/caption/transcribe
python3 scripts/classify_subjects.py                 # per-page subject labels
python3 scripts/build_embeddings.py                  # chunked embeddings, 768d
python3 scripts/build_embeddings.py --force          # full rebuild (after model/format change)

# Keep cache fresh in one shot (detects dirty notebooks via last_modified; re-embeds the delta)
python3 scripts/sync.py
python3 scripts/sync.py status     # idle | running (reports pid + start time)
python3 scripts/sync.py unstick    # kill a hung sync
```

Required env vars:
- `MS_CLIENT_ID` — Azure app registration (delegated `Notes.Read` + `User.Read` scopes)
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) — Google AI Studio key; used for embeddings, OCR/caption, transcription, subject classification

## Architecture

All modules live in `scripts/`, flat namespace. Heavy imports (`msgraph`, `msal`, `google-genai`, `numpy`) are deferred to first use so cache-only ops stay snappy.

| Module | Responsibility |
|---|---|
| `onenote_setup.py` | MSAL device-code auth, token cache at `~/.cache/ms_graph_token_cache.json`, thin Graph API wrappers. `make_graph_client()` is the entry point every Graph caller imports. |
| `onenote_cache.py` | JSON cache (`cache/onenote_cache.json`) with mtime coherency, page index (`page_index.txt`), per-page HTML cache (`page_content/*.html` + `.meta`), lookup helpers, cache-update helpers with ID-based rename detection, `strip_html()`, `atomic_write` / `atomic_savez`. |
| `onenote_api.py` | Graph API read ops — `get_notebooks`, `get_sections`, `get_pages`, `refresh_notebook`, `find_page`, `find_pages_batch`, `refresh_all_notebooks`. Freshness checks via `last_modified` skip unchanged re-fetches. |
| `onenote_search.py` | `search_pages` (title grep) and `search_content` (HTML grep). Pure local, no API. |
| `onenote_media.py` | Resource fetcher (images/PDFs/audio/video from `/onenote/resources/{id}/content`) + Gemini-flash OCR, scene-caption (empty-OCR fallback), audio/video transcription. Stores raw bytes + derived `.ocr.txt` / `.caption.txt` / `.transcript.txt` siblings in `cache/page_resources/`. Also `gc_media` (orphan cleanup), `render_hydrated_html` (browser-viewable HTML rewrite). |
| `onenote_chunks.py` | **Adaptive mechanical chunker.** Walks top-level HTML blocks in document order; paragraph-packs short paragraphs, treats multi-sentence paragraphs as own chunks, row-atomic chunks per table row with header re-prefix, balanced-tag scanning for nested tables. Emits typed `Chunk` objects: `text`, `summary`, `image`, `image_ocr`, `image_caption`, `pdf`, `audio`, `video_transcript`, `audio_transcript`. Text chunks carry `embed_text`; media chunks carry `resource_id`/`mime`/`filename`. |
| `onenote_embeddings.py` | Gemini embeddings build + query, unified text+media vector space (`gemini-embedding-2-preview` @ 768d). Stores `cache/embeddings.npz` (float32, L2-normalized, with `kinds` array) + `cache/embeddings_meta.json`. Query path: single matmul; subject-aware filtering; top-K pages × max-N chunks/page. Public API: `semantic_search`, `query_by_page` (uses an existing page's summary vector — skips Gemini call), `get_chunk_text` (re-chunks host page to recover `embed_text`). |
| `onenote_genai.py` | Shared Gemini client + `with_retry` helper (exponential backoff on 429/503/`RESOURCE_EXHAUSTED`). All LLM call sites go through it. |
| `onenote_lock.py` | `fcntl.flock`-based process lock. Used by `fetch-media` (not by `query` — reads are concurrent-safe). Lockfile body carries `{pid, started_at, hostname}` so `unstick` can SIGTERM/SIGKILL a hung owner even when `flock` didn't auto-release. |
| `onenote_ops.py` | Thin CLI entry point. Re-exports everything from the above modules for backward compat with inline-Python usage (`from onenote_ops import find_page, ...`). |
| `classify_subjects.py` | One-off (+ incremental) per-page subject classifier (`self` / `general` / `<Person>`). Writes `cache/page_subjects.json`; `cache/subject_overrides.json` patches it at query time. |
| `build_embeddings.py` | Standalone CLI wrapper for `onenote_embeddings.build_embeddings`. Used by `sync.py` and for manual rebuilds. |
| `sync.py` | Single-shot cache sync orchestrator. Detects dirty notebooks via `last_modified`, refreshes them, prunes orphans, triggers incremental embedding rebuild. fcntl-locked + self-kill via SIGALRM after `--max-duration`; JSONL log at `cache/sync.log`. Designed to be safe to fire from cron / launchd / keystroke. |

### Cache layout (`cache/`, gitignored)

- `onenote_cache.json` — single source of truth for notebook/section/page IDs + `last_modified`. Never read directly; always via `_load_cache()`. `_save_cache()` rewrites `page_index.txt` as a side-effect.
- `page_index.txt` — tab-separated `title\tnotebook\tsection\tpage_id`, rebuilt from the JSON. Powers fast title search without loading the big JSON.
- `page_content/<safe_id>.html` + `.meta` — per-page HTML cache, invalidated when `.meta` (stores `last_modified`) no longer matches the JSON cache. `_content_path()` replaces `!` and `/` with `_` for filesystem safety.
- `page_resources/<safe_rid>.<ext>` — raw media bytes fetched from Graph.
- `page_resources/<safe_rid>.meta.json` — `{mime, filename, kind, size_bytes, fetched_at, page_ids: [...]}`.
- `page_resources/<safe_rid>.ocr.txt` / `.caption.txt` / `.transcript.txt` — derived text from Gemini flash. Each has a paired `.meta.json` with a content hash of the source bytes so the derived artifact re-runs only when the bytes change.
- `page_rendered/<safe_id>.html` — browser-viewable HTML with `file://`-rewritten image srcs (from `render-page`).
- `page_subjects.json` — `{page_id: "self"|"general"|"<Person>"}`; classifier output. `subject_overrides.json` takes precedence.
- `embeddings.npz` — `{ids: (N,) str, vectors: (N, 768) float32 L2-normalized, kinds: (N,) str}`. One row per **chunk** (not per page). A page typically contributes ~12 chunks (one per text chunk/table row + one summary + one per media resource + OCR/caption/transcript siblings).
- `embeddings_meta.json` — `{model, dim, built_at, pages: {page_id: {notebook, section, title, last_modified, chunk_ids: [...]}}, chunks: {chunk_id: {kind, page_id, heading_path, resource_id, filename, char_count, extra}}}`. **`embed_text` is intentionally NOT stored** — recovered on demand by re-chunking the host page (`get_chunk_text` / `query --with-text`).
- `.sync.lock` / `.sync.heartbeat` / `.sync.state.json` / `sync.log` / `sync.launchd.log` — sync runtime state.

### Cache invariants to preserve

- Renames (section or page) must be detected by **ID**, not by name — see `update_sections_cache` / `update_pages_cache`. Existing entries are carried forward by matching IDs against prior state.
- Embedding vectors are **L2-normalized at build time** so cosine similarity reduces to a single dot product. If you add new vectors without normalizing, scores become meaningless.
- **Chunk-level identity**: chunk IDs are `{page_id}#t{seq:04d}` / `{page_id}#summary` / `{page_id}#media/{rid}` / `{page_id}#media/{rid}#ocr|#caption|#transcript`. The chunker is deterministic — re-chunking the same HTML reproduces the same IDs, which is why `get_chunk_text` can recover `embed_text` without storing it.
- **Incremental rebuild**: `build_embeddings` carries a page forward unchanged iff `last_modified` matches AND every chunk_id in the prior `pages[pid].chunk_ids` is present in `embeddings.npz`. Otherwise it drops the page's rows and re-chunks.
- **Model/dim change** → full rebuild auto-triggered by `build_embeddings` (wipes state if `meta.model`/`meta.dim` disagree with current module constants).
- **Read-only policy**: do not reintroduce `update_page` / `create_page` / `_patch_page_content` / container-setter helpers. The skill is intentionally restricted to reads.

### Harness portability

- No subprocess calls to `claude` anywhere. Embeddings / OCR / classification go through the Google GenAI SDK directly.
- No UNIX daemon, no `/tmp/*.sock` state. A prior design had one — removed to keep behavior identical across harnesses.
- Cache root is derived at runtime from `Path(__file__).parent.parent / 'cache'` (see `REFS_DIR` in `onenote_cache.py`), so invocations through any symlink — `~/.claude/skills/onenote/...`, `~/.openclaw/workspace/skills/onenote/...`, or the repo directly — all land on the same shared cache.
- Only one process must hold certain locks at a time: `sync` (whole-pipeline sync) and `fetch-media` (resource download). Query and read paths are lock-free and concurrent-safe.

### Gemini usage — where and why

| Call site | Model | Why |
|---|---|---|
| Embeddings (build + query) | `gemini-embedding-2-preview` @ 768d | Unified text+image+PDF+audio vector space; one query embed hits every modality |
| OCR / scene caption / transcription | `gemini-2.5-flash` | Cheapest paid option at batch scale; runs once per resource and caches |
| Subject classification | `gemini-2.5-flash` | Same |
| Query synthesis (RAG answer) | **Not Gemini** — the orchestrating Claude harness writes the final answer | Free via subscription, instant, no 503s |

All `gemini-*` calls go through `onenote_genai.with_retry` for exponential backoff on 429 / 503 / `RESOURCE_EXHAUSTED`.
