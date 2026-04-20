# Chunked + Multimodal Embeddings Plan

Upgrade semantic search from one-vector-per-page (text-only) to **chunk-level embeddings that include embedded media** (images + their OCR, PDFs, audio, video transcripts), so:

- Long/multi-topic pages don't get diluted or truncated.
- Query results point to *where* in a page the match lives (heading path / snippet / media descriptor).
- Images, PDFs, audio clips, and videos embedded in pages become directly searchable — currently they contribute nothing because `strip_html()` drops `<img>`/`<object>` tags and resource bytes are never downloaded.
- OneNote subpage hierarchy (level-0/1/2) is preserved in chunk routing.

## Motivation

- Current: 1 vector per page, HTML truncated at 32K chars (`onenote_embeddings.py:44`). Long journal pages lose tail content.
- Cached HTML retains `<img>`/`<object>` *references* but bytes are never downloaded. 202 / 998 pages have images (777 total: 693 PNG, 80 JPEG, 4 TIFF). 1 PDF, 1 MP4.
- `search-content` only surfaces literal keyword hits; paraphrased semantic queries have no location signal.
- Subpages currently flattened — `list_pages()` returns `{id, title, last_modified}` only, dropping `level`/`order` the Graph API provides.

## Model + vector space

Switch from `gemini-embedding-001` (text-only, 1024d) to **`gemini-embedding-2-preview`** (multimodal, **768d** via `output_dimensionality`). Unified space across text / image / audio / video / PDF — cosine similarity between a text query vector and any media vector just works.

Rebuild-on-model-change policy:
- Build detects `meta.model ≠ code default`, prints banner (old → new, chunk count, estimated calls), `Continue? [y/N]`. `--force` / `-y` skips prompt.
- Query-time: `meta.model ≠ code default` → hard-fail with exact rebuild command.

## Chunking

Adaptive mechanical strategy — **no LLM in the chunking loop** (deterministic, cheap, incremental-friendly). Walks the HTML in document order, producing chunks as it goes.

### Text chunks

1. Strip `<img>` / `<object>` from the body stream — they become media chunks separately.
2. Split content into *elements*: paragraphs (`<p>`), list items (top-level `<li>`), tables (`<table>`), horizontal rules (`<hr>`), headings (`<h1>`/`<h2>`), bold-standalone lines (`<p><b>X</b></p>` with no other text) treated as pseudo-headings.
3. Walk elements and **pack into 1.5K-char chunks** with this logic:
   - **Paragraph with ≥3 sentences AND ≥150 non-ws chars** → its own chunk (tight semantic unit).
   - **Paragraph that's shorter** → append to the active pack; close the pack when it approaches the 1.5K budget.
   - **List items** → treated like paragraphs.
   - **`<hr>`** or **pseudo-heading** → soft boundary: close the active pack before continuing.
   - **Table** → emit via table policy (below), not merged into surrounding packs.
   - **Paragraph >1.5K** → sliding-window split, 200-char overlap.
4. **Heading density awareness**:
   - If a heading section has **≥500 chars of content**, the heading is a **hard boundary** — close any active pack, start a fresh pack under the new heading path.
   - If the section has **<500 chars**, the heading is a **soft marker** — embed the heading inline in the current pack (`## Heading text\n\n<body>`), keep packing. This prevents heading-dense pages (e.g., 43 short headings) from producing 43 tiny chunks.
5. **Date-block detection** (journal style): if a paragraph starts with a clear date token (`Jul 30th`, `2024-07-30`, bold date prefix), treat the paragraph plus all following non-date paragraphs up to the next date as one "entry". Each entry participates in packing like a paragraph would.
6. **Routing header** prepended to every chunk:
   ```
   Notebook: <nb>
   Section: <sec>
   Page: <parent> > <subparent> > <title>       (subpage path when non-trivial)
   Heading path: <h1> > <h2>                    (most recent headings in scope)
   Headings covered: X; Y; Z                    (only when soft-packing crossed headings)
   ```

### Table chunks

1. **Small table** (<1.5K total chars) → one chunk for the whole table.
2. **Larger tables** → row-group chunks with the header row re-prefixed to each group. Group size is **adaptive to average row size**:
   - avg row body ≤150 chars → ~10 rows/group
   - 150–400 chars → ~5 rows/group
   - 400–1K chars → 1–2 rows/group
   - any cell >1K chars → 1 row/group + intra-cell windowing (below)
3. **Intra-cell windowing** — when a cell exceeds ~1K chars, treat that cell as a mini-body and paragraph-pack it. Each sub-chunk prepends:
   - the column header re-prefix,
   - all other (small) cells from the same row, compacted into one line,
   - so context carries even across cell sub-splits.

### Media chunks (per resource)

- `kind=image`: raw image bytes → multimodal vector.
- `kind=image_ocr`: if `.ocr.txt` has ≥30 non-whitespace chars, emit a sibling text chunk with the OCR body.
- `kind=pdf`: raw PDF bytes → multimodal vector.
- `kind=audio`: raw bytes → multimodal vector (+ transcript text chunk when one exists).
- `kind=video_transcript`: full audio transcript text → text chunk. Raw video bytes NOT embedded (avoids 120 s cap).

### Page-summary chunk (one per page)

- Whole body, capped ~5K chars.
- `kind=summary`, `chunk_id=<page_id>#summary`.
- Catches broad queries where no individual chunk wins.

**Media chunks** (per resource):
- `kind=image`: raw image bytes → multimodal vector (scene/document semantics).
- `kind=image_ocr`: if Gemini-flash OCR returns **≥30 non-whitespace chars**, emit a sibling text chunk with the OCR text (captures "Vedolizumab" on a prescription screenshot etc.). If <30 chars, skip — rely on scene vector only.
- `kind=pdf`: raw PDF bytes → multimodal vector.
- `kind=audio`: raw audio bytes → multimodal vector + (optional) transcript text chunk.
- `kind=video_transcript`: video files → full audio transcript via Gemini `generate_content` saved as `<safe_id>.transcript.txt`, embedded as text. Raw video bytes NOT sent to the embedder (avoids 120 s cap).

**Page-summary chunk** (one per page):
- Whole page body, capped at ~5K chars, one extra row per page.
- Catches broad queries ("anything about nutrition?") where no single chunk wins strongly.
- `kind=summary`, `chunk_id=<page_id>#summary`.

## Subpage hierarchy

**Plumbing is in place but inactive**: `onenote_setup.list_pages` requests `level`/`order`; `onenote_cache.update_pages_cache` persists them and derives `parent_page_id` via a linear pass.

**Gap**: Microsoft Graph v1.0 *and* beta **do not return `level`/`order` for consumer/personal OneNote accounts** (verified via direct REST call — `$select=level,order` is silently dropped; `$orderby=order` works server-side but values never appear in the JSON). For now all pages land with `level=0, order=0, parent_page_id=''` and the routing header falls back to bare page title.

If Microsoft re-exposes these fields (or the account switches to a business tenant), the code path picks them up automatically — no migration needed.

Known minor gap (independent of the above): pure page reordering without content edits may not bump section `last_modified`; manual `refresh` or `--force` needed to pick up.

## Storage

### `embeddings.npz`
```
ids     : (N,)   string   — chunk_id
vectors : (N,768) float32 — L2-normalized
kinds   : (N,)   string   — {"text","summary","image","image_ocr","pdf","audio","video_transcript"}
```

Chunk ID scheme:
- `<page_id>#t0000`, `<page_id>#t0001` — text chunks in order
- `<page_id>#summary` — page summary chunk
- `<page_id>#media/<resource_id>` — image / pdf / audio raw
- `<page_id>#media/<resource_id>#ocr` — image OCR sibling
- `<page_id>#media/<resource_id>#transcript` — audio/video transcript

### `embeddings_meta.json`
```json
{
  "model": "gemini-embedding-2-preview",
  "dim": 768,
  "built_at": "...",
  "pages": {
    "<page_id>": {
      "notebook": "...", "section": "...", "title": "...",
      "level": 0, "order": 12, "parent_page_id": "...",
      "last_modified": "...",
      "chunk_ids": ["<page_id>#t0000", "<page_id>#summary", "<page_id>#media/<rid>"]
    }
  },
  "chunks": {
    "<chunk_id>": {
      "kind": "text",
      "page_id": "...",
      "heading_path": ["H1", "H2"],
      "char_start": 0, "char_end": 1500
    },
    "<page_id>#media/<rid>": {
      "kind": "image",
      "page_id": "...",
      "resource_id": "...",
      "mime": "image/png",
      "filename": "IMG_1234.png",
      "size_bytes": 48213
    }
  }
}
```

Page-level `last_modified` drives incremental rebuild: on page change, drop all its chunk_ids, re-extract, re-embed.

### `cache/page_resources/` (new)
```
<safe_resource_id>.<ext>             # raw bytes (png/jpg/pdf/mp4/...)
<safe_resource_id>.meta.json         # {mime, filename, kind, orig_url, size, fetched_at, page_ids:[...]}
<safe_resource_id>.ocr.txt           # (images only, when OCR returns ≥30 chars)
<safe_resource_id>.transcript.txt    # (audio/video)
```
- `safe_resource_id`: `!` → `_`, `/` → `_` (matches `_content_path()` scheme).
- Shared resources (same ID on multiple pages) cached once, `page_ids` list tracks uses.
- Bytes kept indefinitely by default; `--gc-media` removes unreferenced bytes. Derived text (`.ocr.txt` / `.transcript.txt`) always preserved.

## Build pipeline

### Phase 1 — media fetch (new module `onenote_media.py`)

```python
parse_resources(html) -> list[dict]
    # [{resource_id, url, kind, mime, filename}, ...]
fetch_resource(client, resource_id) -> bytes
cache_resource(resource_id, bytes, meta) / load_resource(resource_id)
download_resources_for_page(client, page_id, html) -> list[dict]
    # idempotent; updates page_ids list in meta
ocr_image(client, resource_id) -> str          # Gemini flash; write .ocr.txt if >=30 chars
transcribe_av(client, resource_id) -> str      # Gemini flash; write .transcript.txt
```

CLI (new subcommand in `onenote_ops.py`):
- `fetch-media <page_id_or_title>` — per-page (prototype).
- `fetch-media --all` — bulk walker over all cached pages.
- `fetch-media --pages-file <path>` — batch for prototype page set.

### Phase 2 — chunked multimodal embeddings (new `onenote_chunks.py` + revised `onenote_embeddings.py`)

`onenote_chunks.py`:
```python
chunk_page(page_id, html, page_meta) -> list[Chunk]
    # Text chunks (h1/h2 split + windowing + tables)
    # + one summary chunk
    # + one media chunk per resource
    # + image_ocr sibling when applicable
    # + video_transcript sibling for videos
```

Embedding loop:
- Text batches: `embed_content(contents=[text, ...])` up to BATCH_SIZE.
- Media batches: up to 6 items/request per API limits.
- `output_dimensionality=768`. L2-normalize + NaN-guard (reuse existing safety).
- Retry / checkpoint / atomic write logic reused from current implementation.

Incremental build:
- Page `last_modified` unchanged → carry all chunk rows forward.
- Changed → drop all rows for page, re-download resources as needed, re-chunk, re-embed.
- Resource rows immutable by resource_id → re-embedded only if a new resource ID appears.

### Prototype isolation

Write to separate files while prototyping:
- `cache/embeddings_v2.npz`, `cache/embeddings_v2_meta.json`.
- `build_embeddings.py --prototype --pages-file <path>`.
- `query --prototype`.
- Flip default + remove v1 after validation.

## Query path

`semantic_search()`:
1. Embed query (`task_type=RETRIEVAL_QUERY`).
2. Single matmul against all chunk vectors.
3. **Top-K pages × max-N chunks per page** (defaults **K=10, N=3**): walk descending scores, cap per-page count at N, stop once K distinct pages have at least one hit.
4. Per returned chunk:
   - `kind=text`: heading_path + ~200-char snippet.
   - `kind=summary`: marker `[page summary]` + first ~200 chars.
   - `kind=image`: `[image '<filename>']` on `<parent > title>`.
   - `kind=image_ocr`: `[text in image '<filename>']` + OCR snippet.
   - `kind=pdf`: `[pdf '<filename>']`.
   - `kind=audio` / `video_transcript`: transcript snippet or `[audio '<filename>']`.
5. Group output by page; within a page show multiple chunks in descending score.

## `--gc-media` semantics

Two entry points, same core:
- `build_embeddings.py --gc-media`: after a successful build, run GC.
- `onenote_ops.py gc-media [--dry-run]`: standalone cleanup.

Default behavior:
- Scan `cache/page_resources/` for raw byte files (`*.png`, `*.jpg`, `*.pdf`, `*.mp4`, ...).
- Delete any whose `resource_id` is no longer referenced in `cache/onenote_cache.json` HTMLs.
- Preserve `.meta.json`, `.ocr.txt`, `.transcript.txt` always.

Future optional `--aggressive`: also delete raw bytes for *referenced* resources once they're fully embedded+OCR'd/transcribed. Re-downloadable from Graph. Off by default.

## Prototype page set

3 confirmed, 1 pending:
- ✅ `Health / Colitis / Treatment Log` → `0-376efa6e5e443c189f88b8faadc0d91f!1-58B31B88585CA325!2547`
- ✅ `Health / Colitis / Test Results` → `0-f4bfccbd2c8e48fa961285a81276ece7!131-58B31B88585CA325!2547`
- ✅ `Health / Colitis / Good/Bad Foods` → `0-7ae1f8c2828c4c03ad2959969e2799cf!19-58B31B88585CA325!2547`
- ⚠️ `AI / Tools / Test` — not found. `AI` notebook has sections `Approve / MyAgent / Prompts`; no page named `Test`. Awaiting correction.

Stored in `cache/prototype_pages.txt`, consumed by `--pages-file`.

## Rollout order

1. **Phase 1 prototype** — `onenote_media.py`, `fetch-media` CLI (per-page + `--all` + `--pages-file`), OCR/transcribe, `--gc-media`. Exercise on prototype page set. Verify: parsing complete, bytes land, meta clean, re-runs idempotent, OCR threshold sensible.
2. **Subpage plumbing** — `list_pages` captures level/order, `update_pages_cache` persists, parent derivation one-shot. Refresh cache to pick up.
3. **Phase 2 prototype** — `onenote_chunks.py` + chunked+multimodal build → `embeddings_v2.npz`. `query --prototype`. Evaluate recall/precision on the prototype set.
4. **Tune** — chunk budget (1.5K default, adjust if needed), Top-K/N defaults, OCR threshold.
5. **Production flip** — full-corpus rebuild, replace v1 files, remove `MAX_CHARS=32000` + old model constants.
