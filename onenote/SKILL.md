---
name: onenote
description: Read OneNote notebooks via Microsoft Graph API. Use when asked to search or read content in any OneNote notebook (Health, Home Stuff, AI, Economy, HiFi, Hinduism, Spiritual Life, Family and Culture, All Hands, Interviews, etc.). Chunked multimodal semantic search over all pages + embedded images/PDFs/audio/video via Gemini embeddings.
argument-hint: 'query "my panchakarma oils", read Health/Supplements, list sections in Home Stuff'
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

## Setup

- Auth + Graph client:   `~/Projects/skills/onenote/scripts/onenote_setup.py`
- Main CLI:              `~/Projects/skills/onenote/scripts/onenote_ops.py`
- Token cache:           `~/.cache/ms_graph_token_cache.json` (no login needed)
- Cache layout (`~/Projects/skills/onenote/cache/`):
  - `onenote_cache.json` — notebook/section/page index (**never read directly**)
  - `page_index.txt` — grep-able `title\tnotebook\tsection\tpage_id`
  - `page_content/*.html` + `.meta` — HTML snapshots keyed by page ID
  - `page_resources/<rid>.{png,jpg,pdf,mp4,…}` + `.meta.json` — media bytes
  - `page_resources/<rid>.ocr.txt` / `.caption.txt` / `.transcript.txt` — derived text
  - `page_subjects.json` — per-page subject label (`self` / `general` / `<Person>`)
  - `embeddings_v2.npz` + `embeddings_v2_meta.json` — chunked multimodal index (768d)
  - `embeddings.npz` (+`_meta.json`) — legacy v1 page-level index (optional)

Requires `GEMINI_API_KEY` for semantic search. V2 uses `gemini-embedding-2-preview` @ 768d with a unified text+image+PDF+audio vector space.

---

## Search strategies — pick the right tier

Escalate only as needed.

| Tier | When | Cost | Command |
|---|---|---|---|
| **1. Semantic search (v2)** | Natural-language question, conceptual topic, "what do my notes say about X". Surfaces both text and embedded media (images, PDFs, audio, video). | 1 Gemini embed call (~180–300 ms steady) | `onenote_ops.py query --v2 "<query>"` |
| **2. Title search** | User named a page or you know the exact title | instant, no API | `onenote_ops.py search-title "<title>"` |
| **3. Content grep** | Exact keyword over cached page HTML | ~100 ms, no API | `onenote_ops.py search-content "<keyword>"` |
| **4. Full page read** | After routing via a tier above | 1 API call first time, cached after | `onenote_ops.py read-page <nb> <sec> <page>` |

### Semantic search (Tier 1) — primary fast path

**Always pass `--v2`** — it's chunked + multimodal (hits image OCR, scene captions, audio transcripts, page summaries, headings, and table row-groups). The v1 path without `--v2` still works but is page-level text-only and should not be preferred.

```bash
python3 ~/Projects/skills/onenote/scripts/onenote_ops.py query --v2 "<query>" \
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
1. query --v2 "<question>"     → top-K pages × max-N chunks
2. Re-chunk hit pages locally  → pull matched chunks' actual text
3. Synthesize answer from chunks; cite each source page.
```

Use the matched chunks (via `onenote_chunks.chunk_page(...)` → look up by `chunk_id`) — don't re-slice raw HTML, since chunks are what retrieval actually ranked.

**Citation format:** `Page Title — Notebook / Section [subject-if-non-general]`.
- *Tea tannin composition — Health / Colitis / Good/Bad Foods [self]*
- *S3 durability — Interviews / System Design / Cloud Obj store (S3/GCS)* (no tag — general)

---

## Reading pages

```bash
# List structure
onenote_ops.py list-notebooks
onenote_ops.py list-sections "Health"
onenote_ops.py list-pages "Health" "Supplements"

# Read a page (plain text — the usual one for answering questions)
onenote_ops.py read-page "Health" "Supplements" "My Stack"

# Raw HTML (when markup matters)
onenote_ops.py read-page-html "Health" "Supplements" "My Stack"
```

### Long journal / log pages

**Don't truncate long journal/log pages when searching within them.** When a top-ranked semantic hit is a daily log, treatment log, or chronological journal (`Treatment Log`, `Progress`, `Daily Notes`, etc.), read the full page — specific entries often live deep inside a multi-month entry and will be missed by a default 4K-char slice. Pass `--full` in the CLI or read `p['content']` unsliced in inline Python.

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

V2 embedding hits on media kinds directly:
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
sys.path.insert(0, str(__import__('pathlib').Path.home() / 'Projects/skills/onenote/scripts'))
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
# v2 ingest — full pipeline (media → subjects → embeddings)
python3 scripts/onenote_ops.py fetch-media --all           # fetch resources + OCR/caption/transcript
python3 scripts/classify_subjects.py                       # per-page subject labels
python3 scripts/build_embeddings.py --v2                   # chunked embeddings (768d)

# Force full rebuild (after model/format change)
python3 scripts/build_embeddings.py --v2 --force

# Media utilities
python3 scripts/onenote_ops.py fetch-media "<page>"        # one page
python3 scripts/onenote_ops.py render-page "<page>"        # browser-viewable HTML w/ local image srcs
python3 scripts/onenote_ops.py gc-media [--dry-run]        # drop orphaned resource bytes
```

All three ingest steps are incremental — unchanged content is carried forward via `last_modified` checks. First full build ≈ $2 of paid-tier Gemini usage; incremental refreshes are pennies.

---

## Rules

- **Answer concisely.** Lead with the direct answer; supporting detail only if it adds value. Prefer a short paragraph or tight table over bulleted dumps. Skip process narration ("I searched X, then read Y…").
- **Always cite pages**, using `Title — Notebook / Section [subject-if-non-self-and-non-general]`. Don't dump page IDs.
- **Semantic search first** (tier 1, `--v2`) for any content question. Tier 2/3 are for when the user names an exact page or keyword.
- **Decide `--include-general` carefully.** If the query needs reference/protocol/normal-range info to be answerable, pass it. Otherwise default strict.
- **Long journal/log pages: don't truncate.** Load full content or grep within HTML — specific entries are often deep in multi-month logs.
- **Never read `onenote_cache.json` directly** — use the CLI.
- **Read-only skill.** `update_page` / `create_page` have been removed. Do not try to modify OneNote content from this skill.
- **`find_page()`** does case-insensitive, whitespace-insensitive title matching.
- **`strip_html()`** from `onenote_ops` gives clean readable text from page HTML.
