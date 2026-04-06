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
- Cache: `~/.claude/skills/onenote/cache/onenote_cache.json` (never read directly — too large)
- Search index: `~/.claude/skills/onenote/cache/page_index.txt` (grep-able, ~9K tokens)

## Cache (AUTO — always do this)

**Never read `onenote_cache.json` directly** — it's 140K+ chars and will flood context.

Instead use the CLI which reads the cache internally:

```bash
# Search page titles across all notebooks (fastest — no API)
python3 ~/.claude/skills/onenote/scripts/onenote_ops.py search "supplements"

# If you know the notebook/section, go directly:
python3 ~/.claude/skills/onenote/scripts/onenote_ops.py list-pages "Health" "Supplements"
python3 ~/.claude/skills/onenote/scripts/onenote_ops.py read-page "Health" "Supplements" "Probiotics"
```

Cache auto-updates when `get_sections()` or `get_pages()` are called. The daemon keeps it warm.

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

```python
import asyncio, sys
sys.path.insert(0, str(__import__('pathlib').Path.home() / '.claude/skills/onenote/scripts'))
from onenote_setup import make_graph_client
from onenote_ops import get_sections, get_pages, find_page, update_page, create_page

async def main():
    client = make_graph_client()

    # Update an existing page
    page = await find_page(client, "Health", "Supplements", "My Stack")
    await update_page(client, page['id'], "<p>Updated content</p>")

    # Create a new page
    sections = await get_sections(client, "Home Stuff")
    sec = next(s for s in sections if s['name'] == 'Misc')
    await create_page(client, sec['id'], "New Page Title", "<p>Content here</p>")

asyncio.run(main())
```

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

- **Always read before writing** — fetch the page HTML first to preserve structure
- `find_page()` does case-insensitive title matching
- `update_page()` replaces the full body — include all existing content you want to keep
- `strip_html()` from onenote_ops gives clean readable text from page HTML
- Pages return HTML — strip for display, keep raw for updates
