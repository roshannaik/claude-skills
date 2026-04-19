#!/usr/bin/env python3
"""OneNote CLI entry point (read-only).

Heavy lifting lives in sibling modules:
  - onenote_cache   — JSON cache, page index, content cache, update helpers
  - onenote_api     — Graph API read ops (get_notebooks, find_page, refresh_*)
  - onenote_search  — title / content grep, routing index

All names below are re-exported for backward compat with inline-Python usage
(`from onenote_ops import find_page, ...`).
"""
import asyncio
import sys
import warnings
import argparse
from pathlib import Path

warnings.filterwarnings('ignore', category=Warning, module='urllib3')
sys.path.insert(0, str(Path(__file__).parent))

# ---------------------------------------------------------------------------
# Re-exports for backward compatibility
# ---------------------------------------------------------------------------

from onenote_cache import (  # noqa: F401
    REFS_DIR, CACHE_JSON, PAGE_INDEX, PAGE_CONTENT_DIR,
    _load_cache, _save_cache, _rebuild_page_index,
    _load_page_index,
    lookup_notebook, lookup_section, lookup_page,
    _content_path, load_content_cache, save_content_cache,
    update_sections_cache, update_pages_cache,
    strip_html,
)
from onenote_search import (  # noqa: F401
    search_pages, search_content,
)
from onenote_api import (  # noqa: F401
    get_notebooks, get_sections, get_pages, refresh_notebook,
    find_page, find_pages_batch, refresh_all_notebooks,
)
from onenote_embeddings import semantic_search  # noqa: F401

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main_async(args):
    if args.cmd == 'search-title':
        limit = args.limit if args.limit > 0 else None
        all_hits = search_pages(args.query)
        hits = all_hits[:limit] if limit else all_hits
        lines = [f"{h['title']}  |  {h['notebook']} / {h['section']}" for h in hits]
        if len(all_hits) > len(hits):
            lines.insert(0, f"[{len(all_hits)} matches — showing {len(hits)}, use --limit N for more]")
        for line in (lines or ['No results.']):
            print(line)
        return

    if args.cmd == 'search-content':
        limit = args.limit if args.limit > 0 else None
        hits = search_content(args.query, context_chars=args.context, limit=limit)
        if not hits:
            print('No results in cached pages.')
            return
        for h in hits:
            print(f"\n{'='*60}")
            print(f"  {h['title']}  |  {h['notebook']} / {h['section']}")
            print(f"  ({len(h['snippets'])} occurrence{'s' if len(h['snippets']) != 1 else ''})")
            for i, snip in enumerate(h['snippets'][:3], 1):
                print(f"\n  [{i}] ...{snip}...")
            if len(h['snippets']) > 3:
                print(f"\n  ... and {len(h['snippets']) - 3} more occurrence(s)")
        return

    if args.cmd == 'query':
        hits = semantic_search(args.query, top_k=args.top_k, notebook=args.notebook)
        if not hits:
            print('No results.')
            return
        for h in hits:
            print(f"{h['score']:.3f}  {h['title']}  |  {h['notebook']} / {h['section']}")
        return

    # For read-page / read-page-html, skip eager client construction — find_page
    # lazy-creates a client only on cache miss. All other commands need it.
    if args.cmd in ('read-page', 'read-page-html'):
        client = None
    else:
        from onenote_setup import make_graph_client
        client = make_graph_client()

    if args.cmd == 'list-notebooks':
        for n in await get_notebooks(client):
            print(n['name'])

    elif args.cmd == 'list-sections':
        for s in await get_sections(client, args.notebook):
            print(s['name'])

    elif args.cmd == 'list-pages':
        for p in await get_pages(client, args.notebook, args.section):
            print(p['title'])

    elif args.cmd == 'read-page':
        result = await find_page(client=client, notebook_name=args.notebook,
                                 section_name=args.section, page_title=args.page)
        content = result['content']
        max_chars = 0 if args.full else args.max_chars
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + f'\n... [truncated — {len(result["content"])} chars total, use --full for complete content]'
        print(content)

    elif args.cmd == 'read-page-html':
        result = await find_page(client=client, notebook_name=args.notebook,
                                 section_name=args.section, page_title=args.page)
        print(result['html'])

    elif args.cmd == 'refresh':
        stats = await refresh_notebook(client, args.notebook)
        print(f"Refreshed '{args.notebook}': {stats['sections']} sections, {stats['pages']} pages")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='OneNote CLI')
    sub = parser.add_subparsers(dest='cmd')

    sub.add_parser('list-notebooks')

    p = sub.add_parser('list-sections')
    p.add_argument('notebook')

    p = sub.add_parser('list-pages')
    p.add_argument('notebook')
    p.add_argument('section')

    p = sub.add_parser('read-page')
    p.add_argument('notebook')
    p.add_argument('section')
    p.add_argument('page')
    p.add_argument('--max-chars', type=int, default=4000, dest='max_chars',
                   help='Truncate content at N chars (default 4000). Use --full to disable.')
    p.add_argument('--full', action='store_true', help='Return full content without truncation')

    p = sub.add_parser('read-page-html')
    p.add_argument('notebook')
    p.add_argument('section')
    p.add_argument('page')

    p = sub.add_parser('refresh')
    p.add_argument('notebook', help='Refresh all sections + pages in parallel')

    p = sub.add_parser('search-title')
    p.add_argument('query', help='Search page titles (grep, no API)')
    p.add_argument('--limit', type=int, default=20,
                   help='Max results to show (default 20). Use 0 for all.')

    p = sub.add_parser('search-content')
    p.add_argument('query', help='Search cached page content (no API — offline only)')
    p.add_argument('--limit', type=int, default=0,
                   help='Max pages to show (default 0 = all). Use N to cap.')
    p.add_argument('--context', type=int, default=200,
                   help='Characters of context around each match (default 200).')

    p = sub.add_parser('query',
                       help='Semantic search across all pages using Gemini embeddings')
    p.add_argument('query', help='Natural language query')
    p.add_argument('--top-k', type=int, default=10, dest='top_k',
                   help='Number of results to return (default 10)')
    p.add_argument('--notebook', metavar='NOTEBOOK',
                   help='Restrict search to a single notebook (case-insensitive)')

    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
    else:
        asyncio.run(main_async(args))
