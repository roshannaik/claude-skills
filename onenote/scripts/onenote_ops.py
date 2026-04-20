#!python3
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
from onenote_media import (  # noqa: F401
    parse_resources, fetch_resource, download_resources_for_page,
    load_resource, is_cached, PAGE_RESOURCES_DIR,
    ocr_image, transcribe_resource, process_derived_artifacts, gc_media,
    render_hydrated_html, save_hydrated_html, PAGE_RENDERED_DIR,
)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# fetch-media helpers
# ---------------------------------------------------------------------------

def _iter_all_pages():
    """Yield (notebook, section, title, page_id, last_modified) for every cached page."""
    cache = _load_cache()
    for nb_name, nb_data in cache.items():
        if nb_name.startswith('_'):
            continue
        for sec_name, sec_data in nb_data.get('sections', {}).items():
            for page in sec_data.get('pages', []):
                if isinstance(page, dict) and page.get('id'):
                    yield (nb_name, sec_name, page['title'], page['id'],
                           page.get('last_modified', ''))


def _resolve_identifier(ident: str):
    """Resolve a page identifier (raw page_id OR 'Notebook / Section / Title')
    to (notebook, section, title, page_id, last_modified). Returns None on miss."""
    ident = ident.strip()
    if not ident:
        return None
    # Page ID heuristic: OneNote IDs contain '!' and start with '0-'.
    if '!' in ident and '/' not in ident.split('!', 1)[0]:
        for row in _iter_all_pages():
            if row[3] == ident:
                return row
        return None
    # Path form: "Notebook / Section / Title"
    parts = [p.strip() for p in ident.split('/')]
    if len(parts) < 3:
        return None
    nb, sec = parts[0], parts[1]
    title = '/'.join(parts[2:])  # titles may contain '/'
    for row in _iter_all_pages():
        if (row[0].lower() == nb.lower() and row[1].lower() == sec.lower()
                and row[2].strip().lower() == title.strip().lower()):
            return row
    return None


def _read_pages_file(path: str) -> list:
    lines = Path(path).read_text().splitlines()
    return [ln for ln in (l.strip() for l in lines) if ln and not ln.startswith('#')]


async def _fetch_media_one(client, row, *, skip_derived: bool = False) -> dict:
    """Download resources for a single page row, then run OCR/transcription.
    Returns summary dict."""
    nb, sec, title, pid, lm = row
    html = load_content_cache(pid, lm)
    if html is None:
        return {'page': f'{nb} / {sec} / {title}', 'page_id': pid,
                'error': 'no cached HTML (run read-page first or refresh)'}
    try:
        results = await download_resources_for_page(client, pid, html)
    except Exception as e:
        return {'page': f'{nb} / {sec} / {title}', 'page_id': pid, 'error': str(e)}
    fetched = sum(1 for r in results if r.get('fetched'))
    already = sum(1 for r in results if r.get('cached') and not r.get('fetched'))
    errors  = [r for r in results if r.get('error')]

    derived = []
    if not skip_derived:
        rids = [r['resource_id'] for r in results if r.get('cached')]
        derived = process_derived_artifacts(rids)

    return {
        'page': f'{nb} / {sec} / {title}', 'page_id': pid,
        'total': len(results), 'fetched': fetched, 'already_cached': already,
        'errors': errors, 'results': results, 'derived': derived,
    }


def _print_media_summary(summaries: list) -> None:
    total_fetched = total_cached = total_errors = total_refs = 0
    ocr_written = ocr_exists = ocr_empty = ocr_err = 0
    tx_written  = tx_exists  = tx_empty  = tx_err  = 0
    for s in summaries:
        hdr = f"{s['page']}"
        if 'error' in s:
            print(f"  ! {hdr} — {s['error']}")
            continue
        print(f"  {hdr}: {s['total']} refs — {s['fetched']} fetched, "
              f"{s['already_cached']} already cached"
              + (f", {len(s['errors'])} error(s)" if s['errors'] else ''))
        total_fetched += s['fetched']
        total_cached  += s['already_cached']
        total_errors  += len(s['errors'])
        total_refs    += s['total']
        by_rid = {d['resource_id']: d for d in s.get('derived', [])}
        for r in s['results']:
            flag = '✓ new' if r.get('fetched') else ('· cached' if r.get('cached') else '✗ err')
            size = r.get('size_bytes', 0)
            err  = f" — {r['error']}" if r.get('error') else ''
            derived = by_rid.get(r['resource_id'])
            derived_note = ''
            if derived:
                status = derived.get('status', '')
                if r['kind'] == 'image':
                    if   status == 'written': ocr_written += 1; derived_note = f"  ocr:{derived['chars']}ch"
                    elif status == 'exists':  ocr_exists  += 1; derived_note = f"  ocr:{derived.get('chars',0)}ch (cached)"
                    elif status == 'empty':   ocr_empty   += 1; derived_note = "  ocr:empty"
                    elif status == 'error':   ocr_err     += 1; derived_note = f"  ocr:ERR {derived.get('error','')[:60]}"
                elif r['kind'] in ('audio', 'video'):
                    if   status == 'written': tx_written += 1; derived_note = f"  tx:{derived['chars']}ch"
                    elif status == 'exists':  tx_exists  += 1; derived_note = f"  tx:{derived.get('chars',0)}ch (cached)"
                    elif status == 'empty':   tx_empty   += 1; derived_note = "  tx:empty"
                    elif status == 'error':   tx_err     += 1; derived_note = f"  tx:ERR {derived.get('error','')[:60]}"
            print(f"      [{flag}] {r['kind']:6s} {size:>9} B  {r['filename']}{err}{derived_note}")
    print()
    print(f"Summary: {total_refs} refs across {len(summaries)} pages — "
          f"{total_fetched} fetched, {total_cached} already cached, {total_errors} errors")
    if ocr_written or ocr_exists or ocr_empty or ocr_err:
        print(f"  OCR: {ocr_written} new, {ocr_exists} cached, "
              f"{ocr_empty} empty, {ocr_err} errors")
    if tx_written or tx_exists or tx_empty or tx_err:
        print(f"  Transcripts: {tx_written} new, {tx_exists} cached, "
              f"{tx_empty} empty, {tx_err} errors")


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
        if args.v2:
            from onenote_embeddings_v2 import semantic_search as semantic_search_v2
            subj_list = None
            if args.subject:
                # comma-split
                subj_list = [s.strip() for s in args.subject.split(',') if s.strip()]
            pages = semantic_search_v2(args.query,
                                        top_k_pages=args.top_k,
                                        max_n_per_page=args.max_n,
                                        notebook=args.notebook,
                                        subject=subj_list,
                                        no_subject_filter=args.no_subject_filter,
                                        include_general=args.include_general)
            if not pages:
                print('No results.')
                return
            detected = pages[0].get('_detected_subjects') if pages else None
            ig_used = pages[0].get('_include_general') if pages else None
            if detected:
                general_note = ' + general' if ig_used else ''
                print(f"[filter: subject ∈ {{{', '.join(detected)}}}{general_note}]")
            for p in pages:
                best = p['chunks'][0]['score']
                subj_note = f"  [{p.get('subject','')}]" if p.get('subject') else ''
                print(f"{best:.3f}  {p['title']}  |  {p['notebook']} / {p['section']}{subj_note}")
                for c in p['chunks']:
                    hp = ' > '.join(c.get('heading_path', []) or [])
                    hp_note = f'  ({hp})' if hp else ''
                    print(f"       {c['score']:.3f}  {c['kind']:17s} {c['snippet']}{hp_note}")
            return

        hits = semantic_search(args.query, top_k=args.top_k, notebook=args.notebook)
        if not hits:
            print('No results.')
            return
        for h in hits:
            print(f"{h['score']:.3f}  {h['title']}  |  {h['notebook']} / {h['section']}")
        return

    # Commands that never need a Graph client: find_page lazy-creates one only
    # on cache miss; the rest are pure local (read cache, call Gemini, etc.).
    if args.cmd in ('read-page', 'read-page-html', 'gc-media', 'render-page',
                    'query', 'search-title', 'search-content'):
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

    elif args.cmd == 'fetch-media':
        # Resolve the target page set
        rows = []
        if args.all:
            rows = list(_iter_all_pages())
        else:
            idents = list(args.pages or [])
            if args.pages_file:
                idents.extend(_read_pages_file(args.pages_file))
            if not idents:
                print('Error: provide page identifiers, --pages-file, or --all.',
                      file=sys.stderr)
                return
            unresolved = []
            for ident in idents:
                row = _resolve_identifier(ident)
                if row is None:
                    unresolved.append(ident)
                else:
                    rows.append(row)
            if unresolved:
                print(f"Warning: {len(unresolved)} identifier(s) unresolved:",
                      file=sys.stderr)
                for u in unresolved:
                    print(f"  - {u}", file=sys.stderr)
            if not rows:
                return

        summaries = []
        for row in rows:
            summary = await _fetch_media_one(client, row, skip_derived=args.no_derived)
            summaries.append(summary)
        _print_media_summary(summaries)

    elif args.cmd == 'render-page':
        rows = []
        idents = list(args.pages or [])
        if args.pages_file:
            idents.extend(_read_pages_file(args.pages_file))
        if not idents:
            print('Error: provide page identifiers or --pages-file.', file=sys.stderr)
            return
        for ident in idents:
            row = _resolve_identifier(ident)
            if row is None:
                print(f'  ! unresolved: {ident}', file=sys.stderr)
                continue
            rows.append(row)

        for nb, sec, title, pid, lm in rows:
            html = load_content_cache(pid, lm)
            if html is None:
                print(f'  ! {nb} / {sec} / {title} — no cached HTML')
                continue
            path, summary = save_hydrated_html(pid, html)
            r = len(summary['rewritten']); m = len(summary['missing'])
            print(f'  {nb} / {sec} / {title}')
            print(f'    -> {path}  ({r} rewritten, {m} missing)')
            for miss in summary['missing']:
                print(f'       missing: {miss["kind"]:6s} {miss["filename"]}')

    elif args.cmd == 'gc-media':
        result = gc_media(dry_run=args.dry_run)
        suffix = ' (DRY RUN)' if result['dry_run'] else ''
        print(f"gc-media{suffix}: {len(result['deleted'])} orphaned file(s), "
              f"{result['kept']} kept, "
              f"{result['orphaned_bytes'] / 1024:.1f} KB reclaimable")
        for d in result['deleted'][:20]:
            print(f"  {'would delete' if result['dry_run'] else 'deleted'}: "
                  f"{Path(d['path']).name}  ({d['size_bytes']} B)")
        if len(result['deleted']) > 20:
            print(f"  ... and {len(result['deleted']) - 20} more")


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

    p = sub.add_parser('fetch-media',
                       help='Download + cache embedded media (images, PDFs, audio, video) for pages')
    p.add_argument('pages', nargs='*',
                   help='Page identifiers: page_id or "Notebook / Section / Title"')
    p.add_argument('--pages-file', metavar='PATH',
                   help='File with one page identifier per line (# lines ignored)')
    p.add_argument('--all', action='store_true',
                   help='Fetch media for every cached page')
    p.add_argument('--no-derived', action='store_true',
                   help='Skip OCR / transcription; just fetch raw bytes')

    p = sub.add_parser('render-page',
                       help='Write browser-viewable HTML with images rewritten to local file:// URIs')
    p.add_argument('pages', nargs='*',
                   help='Page identifiers: page_id or "Notebook / Section / Title"')
    p.add_argument('--pages-file', metavar='PATH',
                   help='File with one page identifier per line (# lines ignored)')

    p = sub.add_parser('gc-media',
                       help='Delete raw resource bytes no longer referenced by any cached page')
    p.add_argument('--dry-run', action='store_true',
                   help='Report what would be deleted without deleting')

    p = sub.add_parser('query',
                       help='Semantic search across all pages using Gemini embeddings')
    p.add_argument('query', help='Natural language query')
    p.add_argument('--top-k', type=int, default=10, dest='top_k',
                   help='Number of pages to return (default 10)')
    p.add_argument('--max-n', type=int, default=3, dest='max_n',
                   help='[v2] Max chunks per page in results (default 3)')
    p.add_argument('--notebook', metavar='NOTEBOOK',
                   help='Restrict search to a single notebook (case-insensitive)')
    p.add_argument('--v2', action='store_true',
                   help='Use chunked + multimodal v2 embeddings')
    p.add_argument('--subject', metavar='LIST',
                   help='[v2] Comma-separated subject labels (self, Dad, Mom, ...) '
                        'to restrict results. Overrides auto-detection. Use "all" '
                        'to disable filtering.')
    p.add_argument('--no-subject-filter', action='store_true',
                   help='[v2] Disable automatic subject-aware filtering')
    p.add_argument('--include-general', action='store_true',
                   help="[v2] Also include general reference pages alongside "
                        "person-specific ones. Default is strict (person-only).")

    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
    else:
        asyncio.run(main_async(args))
