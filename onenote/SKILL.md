---
name: onenote
description: Read and search my notes in OneNote notebooks. Use when asked to find information in my notes, answer questions from them, just read pages or list sections and pages. Also handles sync requests ("sync", "refresh the cache").
argument-hint: 'query "my latest lab tests", read Health/Supplements, list sections in Home Stuff, sync'
allowed-tools: Bash, Read
author: Roshan Naik
metadata:
  {
    "openclaw":
      {
        "emoji": "📓",
        "os": ["darwin"],
        "requires":
          {
            "bins": ["python3"],
            "env": ["MS_CLIENT_ID", "GEMINI_API_KEY"],
          },
        "install":
          [
            {
              "id": "pip-deps",
              "kind": "pip",
              "packages": ["msal", "msgraph-sdk", "google-genai", "numpy", "beautifulsoup4"],
              "label": "Install Python dependencies",
            },
          ],
      },
  }
---

# OneNote Skill

All `scripts/...` and `cache/...` paths below are relative to this skill's directory — invoke them from wherever the harness has loaded this skill.

## Setup

- Auth + Graph client:   `scripts/onenote_setup.py`
- Main CLI:              `scripts/onenote_ops.py`
- Token cache:           `~/.cache/ms_graph_token_cache.json` (no login needed)
- Cache layout (`cache/`):
  - `onenote_cache.json` — notebook/section/page index (**never read directly**)
  - `page_index.txt` — grep-able `title\tnotebook\tsection\tpage_id`
  - `page_content/*.html` + `.meta` — HTML snapshots keyed by page ID
  - `page_resources/<rid>.{png,jpg,pdf,mp4,…}` + `.meta.json` — media bytes
  - `page_resources/<rid>.ocr.txt` / `.caption.txt` / `.transcript.txt` — derived text
  - `page_subjects.json` — per-page subject label (`self` / `general` / `<Person>`)
  - `embeddings.npz` + `embeddings_meta.json` — chunked multimodal index (768d)

Requires `MS_CLIENT_ID` in env for accessing MS Graph API.
Requires `GEMINI_API_KEY` in env for semantic search. Uses `gemini-embedding-2-preview` @ 768d with a unified text+image+PDF+audio vector space.

---

## Search strategies — pick the right tier

Escalate only as needed.

| Tier | When | Cost | Command |
|---|---|---|---|
| **1. Semantic search** | Natural-language question, conceptual topic, "what do my notes say about X". Surfaces both text and embedded media (images, PDFs, audio, video). | 1 Gemini embed call (~180–300 ms steady) | `scripts/onenote_ops.py query "<query>"` |
| **2. Title search** | User named a page or you know the exact title | instant, no API | `scripts/onenote_ops.py search-title "<title>"` |
| **3. Content grep** | Exact keyword over cached page HTML | ~100 ms, no API | `scripts/onenote_ops.py search-content "<keyword>"` |
| **4. Full page read** | After routing via a tier above | 1 API call first time, cached after | `scripts/onenote_ops.py read-page <nb> <sec> <page>` |

### Semantic search (Tier 1) — primary fast path

Chunked + multimodal: each query hits text chunks, page summaries, image OCR, scene captions, raw image/PDF/audio vectors, and audio/video transcripts in one unified 768-d space.

```bash
python3 scripts/onenote_ops.py query "<query>" \
    [--top-k 10] [--max-n 3] [--notebook NB] [--subject LIST] [--include-general] [--no-subject-filter]
```

Output format (one page block per hit):
```
SCORE  TITLE  |  NOTEBOOK / SECTION  [subject]
       CHUNK_SCORE  KIND             SNIPPET           (heading_path)
       ...
```
- `KIND` ∈ `text`, `summary`, `image`, `image_ocr`, `image_caption`, `pdf`, `audio`, `video_transcript`.
- `subject` shown on the page line; `[general]` is omitted — only `[self]`/`[Dad]`/`[Mom]`/… are shown.

### Subject-aware filtering (important)

Default behavior is **strict by subject**: when the query implies a person, results are restricted to that person's pages only.

- Auto-detect fires on:
  - First-person pronouns (`my / I / me / mine`) → subject = `self`.
  - Known person names (e.g., `Dad`, `Mom`, `Deekshma`, `Amit`, …) + possessives (`Dad's`) → add that subject.
- Multiple detected subjects (e.g., "compare my and Dad's X") → union (`{self, Dad}`).

**General reference pages** (anatomy, nutrients, drug mechanisms, protocols, lab reference ranges, etc.) are `subject=general`. By default they're **excluded** when any subject is detected. Add them back with `--include-general` when the query needs reference context:

- `--include-general`: use when the query asks to interpret, explain, understand, or compare — e.g., *"precautions before my thyroid test"*, *"interpret my iron levels"*, *"how does my panchakarma protocol work"*.
- Default (strict): use when the query asks about specific recorded data — e.g., *"my last iron level"*, *"Dad's meds"*, *"what did I eat on Jul 30 2021"*.

**Decision rule:** apply `--include-general` when the question *can't* be answered from personal records alone (needs reference knowledge).

Override flags:
- `--subject self,Dad`: force an explicit subject set (skips auto-detect).
- `--subject all` / `--no-subject-filter`: disable filtering entirely.

### Standard workflow

```
1. query "<question>"          → top-K pages × max-N chunks
2. Re-chunk hit pages locally  → pull matched chunks' actual text
3. Synthesize answer from chunks; cite each source page.
```

Use the matched chunks (via `onenote_chunks.chunk_page(...)` → look up by `chunk_id`) — don't re-slice raw HTML, since chunks are what retrieval actually ranked.

**When to skip step 2 and read the full page instead:**

- **Read the full page** when the query is a broad survey ("what do my notes say about X", "tell me about X", "everything about X") AND the top hit scores ≥0.75 AND its title directly matches the topic. The page likely has many more relevant chunks than `--max-n` surfaces.
- **Chunks are sufficient** when the query is a narrow factual lookup ("what dose of X do I take", "what was my last Y result", "what is the half-life of Z"). The answer is a single discrete fact; the top chunk contains it and reading more is noise.

When reading full pages, use `read-pages` for 3+ pages; parallel `read-page` Bash calls for exactly 2.

**Citation format:** `Page Title — Notebook / Section [subject-if-non-general]`.
- *Tea tannin composition — Health / Colitis / Good/Bad Foods [self]*
- *S3 durability — Interviews / System Design / Cloud Obj store (S3/GCS)* (no tag — general)

**Harnesses without an LLM:** The script does retrieval only; synthesis and the `--include-general` decision depend on a harness-level LLM. When invoked from a plain shell or a non-LLM automation, output the CLI rows directly; the caller supplies `--include-general` / `--strict-subject` / `--subject` explicitly.

---

## Reading pages

```bash
# List structure
scripts/onenote_ops.py list-notebooks
scripts/onenote_ops.py list-sections "Health"
scripts/onenote_ops.py list-pages "Health" "Supplements"

# Read a page (plain text — the usual one for answering questions)
scripts/onenote_ops.py read-page "Health" "Supplements" "My Stack"

# Raw HTML (when markup matters)
scripts/onenote_ops.py read-page-html "Health" "Supplements" "My Stack"

# Read multiple pages in parallel (flat triplets: NOTEBOOK SECTION PAGE ...)
scripts/onenote_ops.py read-pages \
    "Health" "Supplements" "My Stack" \
    "Health" "Supplements" "Probiotics"
```

### Parallel reads

When reading 3 or more pages, use `read-pages` instead of separate `read-page` calls. It accepts page specs as flat triplets (NOTEBOOK SECTION PAGE repeated) and fetches all pages concurrently via `asyncio.gather()` in a single subprocess. Output is delimited blocks — `=== Title (Notebook / Section) ===` followed by plain text content. A page that errors emits `ERROR: ...` in its block; the remaining pages still succeed.

For exactly 2 pages that are likely warm-cache hits, parallel `read-page` Bash calls are fine.

### Long journal / log pages

`read-page` returns the full page content. Pipe through `head -c N` if you only need a peek.

### Parsing note containers

OneNote pages use absolute-positioned `<div>` blocks as note containers — each is a separate visual block. When asking "what's in X", prefer container-based parsing over flattened text:

```python
import re
containers = re.findall(
    r'<div style="position:absolute;[^"]*">(.*?)(?=<div style="position:absolute|</body>)',
    html, re.DOTALL,
)
for i, c in enumerate(containers, 1):
    text = re.sub(r'<[^>]+>', ' ', c)
    text = re.sub(r'\s+', ' ', text).strip()
    print(f"[{i}] {text[:120]}")
```

---

## Media-aware retrieval (images, PDFs, audio, video)

Embedding hits on media kinds directly:
- `image` — raw multimodal vector of the image bytes (scene semantics)
- `image_ocr` — sibling text chunk from Gemini-flash OCR (≥30 non-ws chars)
- `image_caption` — sibling text chunk from Gemini-flash scene caption (only when OCR was empty)
- `pdf` — raw multimodal vector
- `audio` — raw multimodal vector (+ optional transcript text chunk)
- `video_transcript` — Gemini transcription text; raw video bytes are not embedded

Media-rich pages (diagrams, screenshots with text, charts, prescription images, lab report PDFs) surface naturally via semantic queries — no extra flags needed.

---

## Parallel read (inline Python)

For questions spanning multiple pages — fetches run concurrently:

```python
import asyncio, sys
sys.path.insert(0, 'scripts')  # or absolute path to this skill's scripts/ dir
from onenote_setup import make_graph_client
from onenote_ops import find_pages_batch, refresh_all_notebooks

async def main():
    client = make_graph_client()
    pages = await find_pages_batch(client, [
        {'notebook': 'Health', 'section': 'Supplements', 'page': 'Probiotics'},
        {'notebook': 'Health', 'section': 'Supplements', 'page': 'My Stack'},
    ])
    for p in pages:
        print(p['title'], p.get('error', p['content'][:200]))

asyncio.run(main())
```

---

## Building / rebuilding the index

Usually already done (via background sync). If stale or missing:

```bash
# Ingest pipeline (media → subjects → embeddings)
python3 scripts/onenote_ops.py fetch-media --all           # fetch resources + OCR/caption/transcript
python3 scripts/classify_subjects.py                       # per-page subject labels
python3 scripts/build_embeddings.py                        # chunked embeddings (768d)

# Force full rebuild (after model/format change)
python3 scripts/build_embeddings.py --force

# Media utilities
python3 scripts/onenote_ops.py fetch-media "<page>"        # one page
python3 scripts/onenote_ops.py render-page "<page>"        # browser-viewable HTML w/ local image srcs
python3 scripts/onenote_ops.py gc-media [--dry-run]        # drop orphaned resource bytes

# Concurrency / zombie recovery (fetch-media holds an fcntl lock)
python3 scripts/onenote_ops.py fetch-media --status        # show owner pid/start time, or idle
python3 scripts/onenote_ops.py fetch-media --unstick       # SIGTERM (then SIGKILL @5s) a hung owner
```

All three ingest steps are incremental — unchanged content is carried forward via `last_modified` checks. First full build ≈ $2 of paid-tier Gemini usage; incremental refreshes are pennies.

---

## Sync

If the user asks to sync, refresh the cache, or update the cache, run:

```bash
python3 scripts/sync.py 2>&1
```

Report the summary line from the output (pages added/modified/deleted, embed rebuilt). Do not run a sync unless the user explicitly requests it.

---

## Rules

- **Answer concisely.** Lead with the direct answer; supporting detail only if it adds value. Prefer a short paragraph or tight table over bulleted dumps. Skip process narration ("I searched X, then read Y…").
- **Always cite pages**, using `Title — Notebook / Section [subject-if-non-self-and-non-general]`. Don't dump page IDs.
- **Semantic search first** (tier 1) for any content question. Tier 2/3 are for when the user names an exact page or keyword.
- **Decide `--include-general` carefully.** If the query needs reference/protocol/normal-range info to be answerable, pass it. Otherwise default strict.
- **`read-page` returns full content.**
- **Don't re-read pages already in context.** Before calling `read-page` or `read-pages`, scan the current conversation for prior reads of the same page (by title). If the content is already present, use it directly — skip the call.
- **Reading 3+ pages? Use `read-pages`** — one subprocess, async concurrent fetches. Use parallel `read-page` Bash calls only for exactly 2 pages that are likely cache hits.
- **Never read `cache/onenote_cache.json` directly** — use the CLI.
- **Read-only skill.** `update_page` / `create_page` have been removed. Do not try to modify OneNote content from this skill.
- **`find_page()`** does case-insensitive, whitespace-insensitive title matching.
- **`strip_html()`** from `onenote_ops` gives clean readable text from page HTML.
