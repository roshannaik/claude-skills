---
name: onenote
description: Read and write Roshan's OneNote notebooks via Microsoft Graph API. Use when asked to look up, read, update, or add content in any OneNote notebook (Health, Home Stuff, AI, Economy, HiFi, Hinduism, Spiritual Life, Family and Culture, All Hands, etc.). Supports listing sections/pages, reading page content, and creating/updating pages.
argument-hint: 'read Health/Supplements, list sections in Home Stuff, update page X'
allowed-tools: Bash, Read, Write, Edit
author: clawdi
---

# OneNote Skill

## Setup

- Auth helper + Graph client: `~/.claude/skills/onenote/scripts/onenote_setup.py`
- Operations script: `~/.claude/skills/onenote/scripts/onenote_ops.py`
- Token cache: `~/.cache/ms_graph_token_cache.json` (no login needed)
- Metadata cache: `~/.claude/skills/onenote/cache/onenote_cache.json` (never read directly — too large)
- Title index: `~/.claude/skills/onenote/cache/page_index.txt` (1K lines, page titles + paths)
- Page content cache: `~/.claude/skills/onenote/cache/page_content/*.html` (keyed by page ID)
- **Semantic summaries**: `~/.claude/skills/onenote/cache/summaries/<Notebook>.md` — hierarchical Haiku-generated summaries of notebook → sections → pages, built for semantic routing

**Never read `onenote_cache.json` directly** — use the CLI, which reads it internally.

## Search strategies — pick the right tier

Escalate only as needed. Cheaper tiers first.

| Tier | When | Cost | Command |
|------|------|------|---------|
| **1. Title search** | User names a page, or you know the exact title | instant, no API | `onenote_ops.py search "<title keyword>"` |
| **2. Content grep** | Exact keyword match in already-cached pages | ~100ms, no API | `onenote_ops.py search-content "<keyword>"` |
| **3. Semantic summary** | "Where is info about X?" / conceptual, not just keyword | instant, no API, no subprocess | `onenote_ops.py routing-index [--notebook Health]` → you pick pages inline |
| **4. Full page read** | After routing via any tier above, to get the actual content | 1 API call per page (cached after first fetch) | `onenote_ops.py read-page <nb> <sec> <page>` |

### Semantic routing (Tier 3) — primary fast path for content questions

Print the compact routing index (~2.4K tokens) and pick the 1-3 best-matching pages yourself inline. No subprocess, no extra model call, works with any underlying model.

```bash
# Print compact index — you read this and pick targets
python3 ~/.claude/skills/onenote/scripts/onenote_ops.py routing-index --notebook Health
```

The index is hierarchical: notebook summary → per-section summary + page titles. Read it, identify the 1-3 pages most relevant to the query, then fetch only those.

```bash
# Fetch the target pages
python3 ~/.claude/skills/onenote/scripts/onenote_ops.py read-page "Health" "<Section>" "<Page>"
```

Available notebooks with summaries:
```bash
ls ~/.claude/skills/onenote/cache/summaries/*.json
```

If the target notebook has no summary yet, fall through to Tier 1/2 or build one (see "Building summaries").

### Standard workflow for content questions

```
1. routing-index --notebook Health    # ~instant, ~2.4K tokens into context
2. Pick 1-3 target pages from the index
3. read-page for each target          # ~instant if cached
4. Answer from content
```


## Building summaries

Summaries are built per-notebook via Haiku. First build for a notebook takes ~5–10 min; subsequent builds are incremental (only pages with changed `last_modified` are re-summarized).

```bash
# Build summaries for a notebook (uses claude -p --model haiku per page)
python3 ~/.claude/skills/onenote/scripts/build_summaries.py "Health" --concurrency 10 --max-chars 80000
```

Outputs:
- `cache/summaries/<Notebook>.md` — hierarchical markdown (read by the LLM for routing)
- `cache/summaries/<Notebook>.json` — machine-readable state (used for incremental regen)

Re-run the same command after pages are edited; only changed pages hit Haiku. To force regeneration of specific pages, delete their entries from the `.json` file and re-run.

## Read Operations

```bash
# List all notebooks
python3 ~/.claude/skills/onenote/scripts/onenote_ops.py list-notebooks

# List sections in a notebook
python3 ~/.claude/skills/onenote/scripts/onenote_ops.py list-sections "Health"

# List pages in a section
python3 ~/.claude/skills/onenote/scripts/onenote_ops.py list-pages "Health" "Supplements"

# Read a page (plain text — default for reading/answering questions)
python3 ~/.claude/skills/onenote/scripts/onenote_ops.py read-page "Health" "Supplements" "My Stack"

# Read a page (raw HTML — use when planning to update)
python3 ~/.claude/skills/onenote/scripts/onenote_ops.py read-page-html "Health" "Supplements" "My Stack"
```

## Parallel Read Operations (inline Python)

Use these when a question spans multiple pages or sections — fetches run concurrently.

```python
import asyncio, sys
sys.path.insert(0, str(__import__('pathlib').Path.home() / '.claude/skills/onenote/scripts'))
from onenote_setup import make_graph_client
from onenote_ops import find_pages_batch, refresh_all_notebooks

async def main():
    client = make_graph_client()

    # Read multiple pages at once (e.g. answer a question spanning several pages)
    pages = await find_pages_batch(client, [
        {'notebook': 'Health', 'section': 'Supplements', 'page': 'Probiotics'},
        {'notebook': 'Health', 'section': 'Supplements', 'page': 'My Stack'},
        {'notebook': 'Health', 'section': 'Exercise', 'page': 'Routine'},
    ])
    for p in pages:
        print(p['title'], p.get('error', p['content'][:200]))

    # Refresh all notebooks in parallel (instead of one by one)
    summary = await refresh_all_notebooks(client)
    print(summary)  # {'Health': {'sections': 5, 'pages': 43}, ...}

asyncio.run(main())
```

## Write Operations (inline Python)

For pages with a single note container, the standard write pattern is:

1. Read the page to get its HTML
2. Call `get_container_html(html)` to extract the container's inner HTML
3. Inspect the structure (list, table, dated entries, sections, etc.) and decide where the new content belongs
4. Build the modified inner HTML with the new content inserted at the right location
5. Call `update_page` with `set_container_html(html, modified_inner)`

```python
import asyncio, sys
sys.path.insert(0, str(__import__('pathlib').Path.home() / '.claude/skills/onenote/scripts'))
from onenote_setup import make_graph_client
from onenote_ops import get_sections, get_pages, find_page, update_page, get_container_html, set_container_html, create_page

async def main():
    client = make_graph_client()

    # Write to a single-container page at the right location
    page = await find_page(client, "AI", "MyAgent", "TODO")
    inner = get_container_html(page['html'])
    # Inspect `inner`, decide where the new item belongs, then:
    modified_inner = inner + "<p>new todo item</p>"   # example: append at end
    await update_page(client, page['id'], set_container_html(page['html'], modified_inner))

    # Create a new page
    sections = await get_sections(client, "Home Stuff")
    sec = next(s for s in sections if s['name'] == 'Misc')
    await create_page(client, sec['id'], "New Page Title", "<p>Content here</p>")

asyncio.run(main())
```

### Write rules

- **Always use `get_container_html` / `set_container_html`** for single-container pages — never blindly append to the raw body.
- Read `inner` HTML before writing — inspect the structure (list items, table rows, dated entries, headings) and insert at the semantically correct location.
- Both helpers raise `ValueError` if the page has zero or multiple containers.
- `update_page` replaces the full page body — always reconstruct from the original `html` via `set_container_html` to avoid losing content.
- For full rewrites (rare), pass new body HTML directly to `update_page`.

## Parsing Note Containers

OneNote pages use absolute-positioned `<div>` blocks as note containers — each is a separate visual block on the canvas. Always parse by containers when reading structured pages, not by flattening all HTML into one blob.

```python
import re
containers = re.findall(
    r'<div style="position:absolute;[^"]*">(.*?)(?=<div style="position:absolute|</body>)',
    html, re.DOTALL
)
for i, c in enumerate(containers, 1):
    text = re.sub(r'<[^>]+>', ' ', c)
    text = re.sub(r'\s+', ' ', text).strip()
    print(f"[{i}] {text[:120]}")
```

When asked "what's in X", list containers first if the page appears to have multiple independent blocks.

## Rules

- **Route with summaries before fetching pages.** For any content question ("where is info about X", "what does my notes say about Y"), prefer Tier 3 (semantic summary) over blindly reading pages. Only escalate to full page reads after the summary has narrowed candidates.
- **Never read `onenote_cache.json` directly** — use the CLI.
- **Use `get_container_html` / `set_container_html`** for targeted writes to single-container pages.
- Both container helpers raise `ValueError` if the page does not have exactly one container.
- `update_page()` replaces the full body — always read the page HTML first and include all content you want to keep.
- `find_page()` does case-insensitive title matching.
- `strip_html()` from onenote_ops gives clean readable text from page HTML.
- Pages return HTML — strip for display, keep raw for updates.
- After writing/editing a page, re-run `build_summaries.py <notebook>` to refresh its summary (incremental — only the edited page regenerates).
