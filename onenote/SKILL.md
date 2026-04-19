---
name: onenote
description: Read OneNote notebooks via Microsoft Graph API. Use when asked to search or read content in any OneNote notebook (Health, Home Stuff, AI, Economy, HiFi, Hinduism, Spiritual Life, Family and Culture, All Hands, etc.). Semantic search over all pages via Gemini embeddings.
argument-hint: 'query "sleep supplements", read Health/Supplements, list sections in Home Stuff'
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

- Auth helper + Graph client: `~/Projects/skills/onenote/scripts/onenote_setup.py`
- Operations script: `~/Projects/skills/onenote/scripts/onenote_ops.py`
- Token cache: `~/.cache/ms_graph_token_cache.json` (no login needed)
- Metadata cache: `~/Projects/skills/onenote/cache/onenote_cache.json` (never read directly — too large)
- Title index: `~/Projects/skills/onenote/cache/page_index.txt` (page titles + paths)
- Page content cache: `~/Projects/skills/onenote/cache/page_content/*.html` (keyed by page ID)
- Embeddings: `~/Projects/skills/onenote/cache/embeddings.npz` (Gemini vectors, 1024-dim) + `embeddings_meta.json`

**Never read `onenote_cache.json` directly** — use the CLI, which reads it internally.

Requires `GEMINI_API_KEY` env var for semantic search (get one free at aistudio.google.com/apikey — no credit card needed). Uses `gemini-embedding-001` with MRL truncation to 1024 dims.

## Search strategies — pick the right tier

Escalate only as needed. Cheaper tiers first.

| Tier | When | Cost | Command |
|------|------|------|---------|
| **1. Semantic search** | Natural-language question, conceptual topic, "where is info about X" | 1 Gemini API call (~100 ms) | `onenote_ops.py query "<natural language query>"` |
| **2. Title search** | User named a page or you know the exact title | instant, no API | `onenote_ops.py search-title "<title keyword>"` |
| **3. Content grep** | Exact keyword in already-cached page HTML | ~100 ms, no API | `onenote_ops.py search-content "<keyword>"` |
| **4. Full page read** | After routing via any tier above, to get the actual content | 1 API call per page (cached after first fetch) | `onenote_ops.py read-page <nb> <sec> <page>` |

### Semantic search (Tier 1) — primary fast path

```bash
# Top 10 matches across all notebooks
python3 ~/Projects/skills/onenote/scripts/onenote_ops.py query "sleep supplements I take"

# Restrict to one notebook
python3 ~/Projects/skills/onenote/scripts/onenote_ops.py query "morning routine" --notebook Health --top-k 5
```

Output: `score  title  |  notebook / section` — pick the 1-3 most relevant pages, then fetch them.

### Standard workflow for content questions

```
1. query "<natural language query>"  # ~100 ms, 1 Gemini call, top-K with scores
2. Pick 1-3 target pages from the top results
3. read-page for each target    # ~instant if cached
4. Answer from content
```

## Building / rebuilding embeddings

Incremental — pages whose `last_modified` hasn't changed are reused.

```bash
# Incremental rebuild for all notebooks
python3 ~/Projects/skills/onenote/scripts/build_embeddings.py

# Force full rebuild (use after changing the model or the embed-text format)
python3 ~/Projects/skills/onenote/scripts/build_embeddings.py --force

# Limit to specific notebooks
python3 ~/Projects/skills/onenote/scripts/build_embeddings.py --notebook Health AI
```

Free tier on Google AI Studio (no card required) covers this. Pages without cached HTML are skipped — run `refresh` or `read-page` first to populate the content cache.

## Read Operations

```bash
# List all notebooks
python3 ~/Projects/skills/onenote/scripts/onenote_ops.py list-notebooks

# List sections in a notebook
python3 ~/Projects/skills/onenote/scripts/onenote_ops.py list-sections "Health"

# List pages in a section
python3 ~/Projects/skills/onenote/scripts/onenote_ops.py list-pages "Health" "Supplements"

# Read a page (plain text — default for reading/answering questions)
python3 ~/Projects/skills/onenote/scripts/onenote_ops.py read-page "Health" "Supplements" "My Stack"

# Read a page (raw HTML — when you need the markup, not just text)
python3 ~/Projects/skills/onenote/scripts/onenote_ops.py read-page-html "Health" "Supplements" "My Stack"
```

## Parallel Read Operations (inline Python)

Use these when a question spans multiple pages or sections — fetches run concurrently.

```python
import asyncio, sys
sys.path.insert(0, str(__import__('pathlib').Path.home() / 'Projects/skills/onenote/scripts'))
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

- **Answer concisely.** Lead with the direct answer to the user's question, then supporting detail only if it adds value. Prefer a short paragraph or a tight table over bulleted dumps. Skip source-page citations unless the user asks where it came from. Skip process narration ("I searched for X, then I read Y…") — the user doesn't need it.
- **Semantic search first.** For any content question ("where is info about X", "what do my notes say about Y"), start with `query`. Tier 2/3 are for when the user names an exact page or keyword.
- **Don't truncate long journal/log pages when searching within them.** When a top-ranked semantic hit is a daily log, treatment log, or any chronological journal-style page (e.g. `Treatment Log`, `Progress`, `Daily Notes`), read the full page — specific details often live deep inside a multi-month entry and will be missed by a default 4K-char slice. Either load the full content (pass `--full` in CLI, or read `p['content']` without slicing in inline Python) or grep within the HTML for the specific keyword. Don't conclude the answer isn't there based on a truncated read.
- **Never read `onenote_cache.json` directly** — use the CLI.
- **This skill is read-only** — `update_page` / `create_page` have been removed. Do not try to modify OneNote content from this skill.
- `find_page()` does case-insensitive, whitespace-insensitive title matching.
- `strip_html()` from onenote_ops gives clean readable text from page HTML.
