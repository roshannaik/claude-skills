#!/usr/bin/env python3
"""
OneNote operations via Microsoft Graph API.
Requires: ~/.claude/skills/onenote/scripts/onenote_setup.py (auth helper + Graph client)
Token cache: ~/.cache/ms_graph_token_cache.json (no login needed after first setup)

Performance design:
- Daemon mode (--serve): long-running async UNIX socket server, idles for 6 hours.
  All CLI calls check for a running daemon first; if found, delegate over socket
  (no Python startup or import cost). If not found, start daemon in background and
  run this call in-process (daemon ready for next call).
- In-memory JSON cache with mtime check: daemon never re-reads disk unless file changed.
- Heavy imports (msgraph, msal) deferred until first API call — cache/search ops are fast.
- Page content cached per page_id, invalidated by last_modified from Graph API.
- Parallel section/page fetches via asyncio.gather() on refresh.
"""
import asyncio, sys, os, re, json, time, argparse, warnings, signal, urllib.request
warnings.filterwarnings('ignore', category=Warning, module='urllib3')
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

REFS_DIR         = Path(__file__).parent.parent / 'cache'
CACHE_JSON       = REFS_DIR / 'onenote_cache.json'
PAGE_INDEX       = REFS_DIR / 'page_index.txt'
PAGE_CONTENT_DIR = REFS_DIR / 'page_content'
PAGE_CONTENT_DIR.mkdir(parents=True, exist_ok=True)  # ensure exists at import time
DAEMON_SOCK      = Path('/tmp/onenote_ops.sock')
DAEMON_PID       = Path('/tmp/onenote_ops.pid')
IDLE_TIMEOUT     = 6 * 3600  # seconds

# ---------------------------------------------------------------------------
# JSON cache — with in-memory mtime cache for daemon efficiency
# ---------------------------------------------------------------------------

_mem_cache: dict = {}
_mem_cache_mtime: float = 0.0

def _load_cache() -> dict:
    """Load JSON cache. In daemon mode, only re-reads disk when file has changed."""
    global _mem_cache, _mem_cache_mtime
    try:
        mtime = CACHE_JSON.stat().st_mtime if CACHE_JSON.exists() else 0.0
    except OSError:
        mtime = 0.0
    if mtime > _mem_cache_mtime:
        _mem_cache = json.loads(CACHE_JSON.read_text()) if CACHE_JSON.exists() else {}
        _mem_cache_mtime = mtime
    return _mem_cache

def _save_cache(cache: dict) -> None:
    global _mem_cache, _mem_cache_mtime
    cache['_meta'] = {'last_updated': datetime.now().strftime('%Y-%m-%d')}
    CACHE_JSON.write_text(json.dumps(cache, separators=(',', ':')))
    _mem_cache = cache
    _mem_cache_mtime = CACHE_JSON.stat().st_mtime
    _rebuild_page_index(cache)

def _rebuild_page_index(cache: dict) -> None:
    """Write page_index.txt: tab-separated title\\tnotebook\\tsection\\tpage_id"""
    lines = []
    for nb_name, nb_data in cache.items():
        if nb_name.startswith('_'):
            continue
        for sec_name, sec_data in nb_data.get('sections', {}).items():
            for page in sec_data.get('pages', []):
                title   = page['title'] if isinstance(page, dict) else page
                page_id = page.get('id', '') if isinstance(page, dict) else ''
                lines.append(f"{title}\t{nb_name}\t{sec_name}\t{page_id}")
    PAGE_INDEX.write_text('\n'.join(lines) + '\n')

# ---------------------------------------------------------------------------
# Cache lookup helpers — no API, no heavy imports
# ---------------------------------------------------------------------------

def lookup_notebook(name: str) -> dict:
    cache = _load_cache()
    return next((v for k, v in cache.items()
                 if not k.startswith('_') and k.lower() == name.lower()), None)

def lookup_section(notebook: str, section: str) -> dict:
    nb = lookup_notebook(notebook)
    if not nb:
        return None
    return next((v for k, v in nb.get('sections', {}).items()
                 if k.lower() == section.lower()), None)

def lookup_page(notebook: str, section: str, title: str) -> dict:
    sec = lookup_section(notebook, section)
    if not sec:
        return None
    for page in sec.get('pages', []):
        t = page['title'] if isinstance(page, dict) else page
        if t.lower() == title.lower():
            return page if isinstance(page, dict) else {'title': t, 'id': '', 'last_modified': ''}
    return None

_page_index_cache: list = []
_page_index_mtime: float = 0.0

def _load_page_index() -> list:
    """Load page_index.txt into memory, re-reading only when file changes."""
    global _page_index_cache, _page_index_mtime
    try:
        mtime = PAGE_INDEX.stat().st_mtime if PAGE_INDEX.exists() else 0.0
    except OSError:
        mtime = 0.0
    if mtime > _page_index_mtime:
        if PAGE_INDEX.exists():
            lines = PAGE_INDEX.read_text().splitlines()
            _page_index_cache = [ln.split('\t') for ln in lines if ln.strip()]
        else:
            _page_index_cache = []
        _page_index_mtime = mtime
    return _page_index_cache

def search_pages(query: str, limit: int = None) -> list:
    """Search page_index.txt in-process (no subprocess). Fast — no API, no heavy imports.
    limit=None returns all matches; pass an int to cap results."""
    q = query.lower()
    hits = []
    for parts in _load_page_index():
        if len(parts) >= 3 and q in parts[0].lower():
            hits.append({'title': parts[0], 'notebook': parts[1], 'section': parts[2],
                         'id': parts[3] if len(parts) > 3 else ''})
    return hits[:limit] if limit else hits

# ---------------------------------------------------------------------------
# Page content cache — keyed by page_id, invalidated by last_modified
# ---------------------------------------------------------------------------

def _content_path(page_id: str) -> Path:
    safe = page_id.replace('!', '_').replace('/', '_')
    return PAGE_CONTENT_DIR / f'{safe}.html'

def load_content_cache(page_id: str, expected_modified: str) -> str:
    if not expected_modified or not page_id:
        return None
    p = _content_path(page_id)
    meta = p.with_suffix('.meta')
    if p.exists() and meta.exists() and meta.read_text().strip() == expected_modified.strip():
        return p.read_text()
    return None

def save_content_cache(page_id: str, html: str, last_modified: str) -> None:
    p = _content_path(page_id)
    p.write_text(html)
    if last_modified:
        p.with_suffix('.meta').write_text(last_modified)

# ---------------------------------------------------------------------------
# Cache update helpers — called after API fetches
# ---------------------------------------------------------------------------

def update_sections_cache(notebook_name: str, sections: list, notebook_id: str,
                           notebook_modified: str = '') -> None:
    """Update sections cache. Detects renames by matching on section ID."""
    cache = _load_cache()
    nb_key = next((k for k in cache if not k.startswith('_')
                   and k.lower() == notebook_name.lower()), notebook_name)
    existing_secs = cache.get(nb_key, {}).get('sections', {})

    # Build id→name reverse map of existing cache for rename detection
    id_to_existing_name = {v['id']: k for k, v in existing_secs.items() if v.get('id')}

    new_secs = {}
    for s in sections:
        old_name = id_to_existing_name.get(s['id'])  # None if new section
        # Carry forward existing pages (rename-safe: keyed by ID match)
        existing_entry = existing_secs.get(old_name or s['name'], {})
        new_secs[s['name']] = {
            'id':            s['id'],
            'last_modified': s.get('last_modified', ''),
            'pages':         existing_entry.get('pages', []),
        }

    cache[nb_key] = {
        'id':            notebook_id,
        'last_modified': notebook_modified,
        'sections':      new_secs,
    }
    _save_cache(cache)


def update_pages_cache(notebook_name: str, section_name: str, pages: list,
                        section_modified: str = '') -> None:
    """Update pages cache. Detects renames by matching on page ID."""
    cache = _load_cache()
    nb_key = next((k for k in cache if not k.startswith('_')
                   and k.lower() == notebook_name.lower()), notebook_name)
    if nb_key not in cache:
        cache[nb_key] = {'id': '', 'sections': {}}
    sec_key = next((k for k in cache[nb_key]['sections']
                    if k.lower() == section_name.lower()), section_name)
    if sec_key not in cache[nb_key]['sections']:
        cache[nb_key]['sections'][sec_key] = {'id': '', 'last_modified': '', 'pages': []}

    # Rename detection: build id→old_title map from cached pages
    existing_pages = cache[nb_key]['sections'][sec_key].get('pages', [])
    id_to_old_title = {p['id']: p['title'] for p in existing_pages if p.get('id')}

    new_pages = []
    for p in pages:
        old_title = id_to_old_title.get(p['id'])
        if old_title and old_title != p['title']:
            pass  # title updated below — rename detected, no special action needed
        new_pages.append({
            'title':         p['title'],
            'id':            p['id'],
            'last_modified': p.get('last_modified', ''),
        })

    cache[nb_key]['sections'][sec_key]['pages'] = new_pages
    if section_modified:
        cache[nb_key]['sections'][sec_key]['last_modified'] = section_modified
    _save_cache(cache)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def strip_html(html: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', text).strip()

# ---------------------------------------------------------------------------
# API operations — heavy imports deferred until first call
# ---------------------------------------------------------------------------

async def get_notebooks(client=None):
    from onenote_setup import make_graph_client, list_notebooks
    if client is None:
        client = make_graph_client()
    return await list_notebooks(client)

async def get_sections(client, notebook_name: str) -> list:
    """Fetch sections. Uses cached notebook ID and last_modified to skip re-fetch when unchanged."""
    from onenote_setup import list_sections, list_notebooks, get_notebook_modified
    nb = lookup_notebook(notebook_name)

    if nb and nb.get('id'):
        nb_id = nb['id']
        # Freshness check: lightweight single-item fetch
        if nb.get('last_modified') and nb.get('sections'):
            current_mod = await get_notebook_modified(client, nb_id)
            if current_mod == nb['last_modified']:
                # Cache is fresh — return sections without re-fetching
                return [{'id': v['id'], 'name': k, 'last_modified': v.get('last_modified', '')}
                        for k, v in nb['sections'].items()]
    else:
        notebooks = await list_notebooks(client)
        nb_data = next((n for n in notebooks if n['name'].lower() == notebook_name.lower()), None)
        if not nb_data:
            raise ValueError(f"Notebook '{notebook_name}' not found.")
        nb_id = nb_data['id']

    sections = await list_sections(client, nb_id)
    nb_mod = nb.get('last_modified', '') if nb else ''
    # Get fresh notebook last_modified if we didn't already fetch it
    if not nb_mod:
        try:
            nb_mod = await get_notebook_modified(client, nb_id)
        except Exception:
            pass
    update_sections_cache(notebook_name, sections, nb_id, notebook_modified=nb_mod)
    return sections


async def get_pages(client, notebook_name: str, section_name: str) -> list:
    """Fetch pages. Uses cached section last_modified to skip re-fetch when unchanged."""
    from onenote_setup import list_pages, get_section_modified
    sec = lookup_section(notebook_name, section_name)

    if sec and sec.get('id'):
        sec_id = sec['id']
        # Freshness check: lightweight single-item fetch
        if sec.get('last_modified') and sec.get('pages'):
            current_mod = await get_section_modified(client, sec_id)
            if current_mod == sec['last_modified']:
                # Cache is fresh — return pages without re-fetching
                return sec['pages']
    else:
        sections = await get_sections(client, notebook_name)
        sec_data = next((s for s in sections if s['name'].lower() == section_name.lower()), None)
        if not sec_data:
            raise ValueError(f"Section '{section_name}' not found in '{notebook_name}'.")
        sec_id = sec_data['id']

    pages = await list_pages(client, sec_id)
    # Fetch current section last_modified to store for next freshness check
    try:
        sec_mod = await get_section_modified(client, sec_id)
    except Exception:
        sec_mod = ''
    update_pages_cache(notebook_name, section_name, pages, section_modified=sec_mod)
    return pages

async def refresh_notebook(client, notebook_name: str) -> dict:
    """Refresh all sections + all pages in parallel via asyncio.gather()."""
    from onenote_setup import list_pages
    sections = await get_sections(client, notebook_name)

    async def _fetch(sec):
        pages = await list_pages(client, sec['id'])
        update_pages_cache(notebook_name, sec['name'], pages)
        return len(pages)

    counts = await asyncio.gather(*[_fetch(s) for s in sections])
    return {'sections': len(sections), 'pages': sum(counts)}

# ---------------------------------------------------------------------------
# Background page-ID pre-population
# ---------------------------------------------------------------------------

PREPOP_CONCURRENCY = 8
_PREPOP_LOG        = Path('/tmp/onenote_prepop.log')
_PREPOP_STATUS     = Path('/tmp/onenote_prepop_status.json')
_prepop_cancel     = False   # set by signal handler to abort gracefully


async def _fetch_section_with_retry(client, nb_name: str, sec_name: str,
                                     sec_id: str, cached_mod: str,
                                     max_retries: int = 4) -> tuple:
    """Fetch + cache pages for one section. Returns (page_count, status_str).
    Retries on 429 with exponential backoff: 2s, 4s, 8s, 16s (max 60s)."""
    from onenote_setup import list_pages, get_section_modified
    for attempt in range(max_retries):
        try:
            pages   = await list_pages(client, sec_id)
            try:
                sec_mod = await get_section_modified(client, sec_id)
            except Exception:
                sec_mod = cached_mod
            update_pages_cache(nb_name, sec_name, pages, section_modified=sec_mod)
            return len(pages), 'ok'
        except Exception as e:
            msg = str(e)
            if '429' in msg or 'throttl' in msg.lower() or 'TooManyRequests' in msg:
                wait = min(2 * (2 ** attempt), 60)   # 2, 4, 8, 16 … 60 s
                await asyncio.sleep(wait)
            else:
                return 0, f'err:{msg[:80]}'
    return 0, 'throttled'


async def prepopulate_page_ids(client=None, concurrency: int = PREPOP_CONCURRENCY,
                                log_file: Path = None) -> dict:
    """
    Pre-populate page IDs for all sections in the local cache.

    Design:
    - Skips sections where every cached page already has an ID (safe to re-run)
    - Concurrency=8 (proven safe for personal Graph accounts, no 429s in probe)
    - Retries 429/throttle with exponential backoff, up to 4 attempts per section
    - Writes cache incrementally — crash or SIGTERM loses only the in-flight batch
    - Live progress: \\r updates to stderr (foreground) or log_file (daemon)
    - Machine-readable status written to /tmp/onenote_prepop_status.json
    """
    global _prepop_cancel
    _prepop_cancel = False

    from onenote_setup import make_graph_client
    if client is None:
        client = make_graph_client()

    # Build work list — skip sections already fully populated
    cache = _load_cache()
    work, skip_count = [], 0
    for nb_name, nb_data in cache.items():
        if nb_name.startswith('_'):
            continue
        for sec_name, sec_data in nb_data.get('sections', {}).items():
            sec_id = sec_data.get('id')
            if not sec_id:
                continue
            pages = sec_data.get('pages', [])
            if pages and all(isinstance(p, dict) and p.get('id') for p in pages):
                skip_count += 1
                continue
            work.append((nb_name, sec_name, sec_id, sec_data.get('last_modified', '')))

    total       = len(work)
    done        = 0
    errors      = 0
    total_pages = 0
    t_start     = time.perf_counter()
    sem         = asyncio.Semaphore(concurrency)

    def _write_progress():
        elapsed = max(time.perf_counter() - t_start, 0.001)
        rate    = done / elapsed
        pct     = done / total if total else 1.0
        bar_w   = 24
        filled  = int(bar_w * pct)
        bar     = ('=' * filled + ('>' if filled < bar_w else '') +
                   ' ' * max(bar_w - filled - 1, 0))
        line    = (f"prepop [{bar}] {done}/{total} secs "
                   f"| {rate:.1f}/s | skip={skip_count} err={errors} pages={total_pages}")
        if log_file:
            Path(log_file).write_text(line + '\n')
        else:
            print(f"\r{line}", end='', file=sys.stderr, flush=True)
        try:
            _PREPOP_STATUS.write_text(json.dumps({
                'done': done, 'total': total, 'skip': skip_count,
                'errors': errors, 'pages': total_pages,
                'rate': round(rate, 2),
                'elapsed': round(elapsed, 1), 'running': True,
            }))
        except Exception:
            pass

    async def _worker(nb_name, sec_name, sec_id, cached_mod):
        nonlocal done, errors, total_pages
        if _prepop_cancel:
            return
        async with sem:
            if _prepop_cancel:
                return
            count, status = await _fetch_section_with_retry(
                client, nb_name, sec_name, sec_id, cached_mod)
            done        += 1
            total_pages += count
            if status != 'ok':
                errors += 1
            _write_progress()

    # Register signal handlers for graceful shutdown (asyncio-safe)
    loop = asyncio.get_event_loop()
    def _on_cancel():
        global _prepop_cancel
        _prepop_cancel = True
    try:
        loop.add_signal_handler(signal.SIGTERM, _on_cancel)
        loop.add_signal_handler(signal.SIGINT,  _on_cancel)
    except (NotImplementedError, RuntimeError):
        pass  # non-Unix or already in signal handler context

    _write_progress()
    tasks = [asyncio.create_task(_worker(*args)) for args in work]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        _prepop_cancel = True
        for t in tasks:
            t.cancel()

    if not log_file:
        print('', file=sys.stderr)  # newline after final \r

    elapsed = time.perf_counter() - t_start
    result  = {
        'done': done, 'total': total, 'skip': skip_count,
        'errors': errors, 'pages': total_pages,
        'elapsed': round(elapsed, 1), 'cancelled': _prepop_cancel,
    }
    try:
        _PREPOP_STATUS.write_text(json.dumps({**result, 'running': False}))
    except Exception:
        pass
    if log_file:
        verb = 'cancelled' if _prepop_cancel else 'complete'
        Path(log_file).write_text(
            f"{verb}: {done}/{total} sections, {total_pages} pages, "
            f"{errors} errors in {elapsed:.1f}s\n"
        )
    return result


async def _background_prepopulate() -> None:
    """Daemon background task: pre-populate page IDs 5 s after daemon start."""
    await asyncio.sleep(5)   # let daemon fully settle first
    try:
        from onenote_setup import make_graph_client
        client = make_graph_client()
        await prepopulate_page_ids(client, log_file=_PREPOP_LOG)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        try:
            _PREPOP_LOG.write_text(f"prepopulate failed: {e}\n")
        except Exception:
            pass


async def find_page(client, notebook_name: str, section_name: str, page_title: str) -> dict:
    """Find a page and return its content.

    Fast path  (0 API calls): page ID cached + content fresh.
    Medium path (1 API call): page ID cached, content stale/missing.
    Slow path  (2+ API calls): page ID not cached, fetches via API.
    """
    from onenote_setup import get_page_content
    cached = lookup_page(notebook_name, section_name, page_title)

    if cached and cached.get('id'):
        page_id  = cached['id']
        last_mod = cached.get('last_modified', '')
        html = load_content_cache(page_id, last_mod)
        if html is None:
            html = await get_page_content(client, page_id)
            save_content_cache(page_id, html, last_mod)
        return {'id': page_id, 'title': page_title, 'content': strip_html(html), 'html': html}

    pages = await get_pages(client, notebook_name, section_name)
    page  = next((p for p in pages if p['title'].lower() == page_title.lower()), None)
    if not page:
        raise ValueError(f"Page '{page_title}' not found in {notebook_name}/{section_name}. "
                         f"Available: {[p['title'] for p in pages]}")
    html = load_content_cache(page['id'], page.get('last_modified', ''))
    if html is None:
        html = await get_page_content(client, page['id'])
        save_content_cache(page['id'], html, page.get('last_modified', ''))
    return {'id': page['id'], 'title': page['title'], 'content': strip_html(html), 'html': html}

async def find_pages_batch(client, page_specs: list[dict]) -> list[dict]:
    """Fetch multiple pages in parallel.

    page_specs = [{'notebook': ..., 'section': ..., 'page': ...}, ...]
    Returns list of {id, title, content, html} dicts.
    Failed pages include an 'error' key instead of content.
    """
    async def _fetch(spec):
        try:
            return await find_page(client, spec['notebook'], spec['section'], spec['page'])
        except Exception as e:
            return {'title': spec.get('page', ''), 'content': '', 'html': '', 'error': str(e)}
    return list(await asyncio.gather(*[_fetch(s) for s in page_specs]))


async def refresh_all_notebooks(client) -> dict:
    """Refresh all notebooks in parallel.

    Returns {notebook_name: {'sections': N, 'pages': N}} for each notebook.
    """
    cache = _load_cache()
    notebooks = [k for k in cache if not k.startswith('_')]

    async def _refresh(nb_name):
        try:
            result = await refresh_notebook(client, nb_name)
            return nb_name, result
        except Exception as e:
            return nb_name, {'error': str(e)}

    results = await asyncio.gather(*[_refresh(nb) for nb in notebooks])
    return dict(results)


def _patch_page_content(page_id: str, patch_body: list) -> None:
    """Send a PATCH request to the OneNote page content endpoint."""
    from onenote_setup import get_access_token
    token = get_access_token()
    url = f'https://graph.microsoft.com/v1.0/me/onenote/pages/{page_id}/content'
    data = json.dumps(patch_body).encode('utf-8')
    req = urllib.request.Request(
        url, data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        method='PATCH',
    )
    with urllib.request.urlopen(req):
        pass


async def update_page(client, page_id: str, new_html_content: str):
    """Replace the entire body of a OneNote page."""
    _patch_page_content(page_id, [{"target": "body", "action": "replace", "content": new_html_content}])


_CONTAINER_RE = re.compile(
    r'(<div\b[^>]*style="[^"]*position:absolute[^"]*"[^>]*>)(.*?)(</div>)',
    re.DOTALL | re.IGNORECASE,
)


def _get_body(html: str) -> str:
    m = re.search(r'<body[^>]*>(.*)</body>', html, re.DOTALL | re.IGNORECASE)
    if not m:
        raise ValueError("Could not parse page body.")
    return m.group(1)


def get_container_html(html: str) -> str:
    """Return the inner HTML of the single note container in a page.

    Raises ValueError if the page has zero or multiple note containers.
    Use this to read the container before deciding where to insert new content.
    """
    matches = _CONTAINER_RE.findall(_get_body(html))
    if len(matches) == 0:
        raise ValueError("Page has no note containers.")
    if len(matches) > 1:
        raise ValueError(
            f"Page has {len(matches)} note containers — only single-container pages are supported."
        )
    return matches[0][1]  # group 2: inner HTML


def set_container_html(html: str, new_inner: str) -> str:
    """Return the page body HTML with the single container's inner HTML replaced by new_inner.

    The return value is the body content ready to pass directly to update_page().
    Raises ValueError if the page has zero or multiple note containers.
    """
    body = _get_body(html)
    matches = list(_CONTAINER_RE.finditer(body))
    if len(matches) == 0:
        raise ValueError("Page has no note containers.")
    if len(matches) > 1:
        raise ValueError(
            f"Page has {len(matches)} note containers — only single-container pages are supported."
        )
    m = matches[0]
    return body[:m.start(2)] + new_inner + body[m.end(2):]


async def create_page(client, section_id: str, title: str, html_body: str):
    html = f"""<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body>{html_body}</body></html>"""
    return await client.post(
        f"/me/onenote/sections/{section_id}/pages",
        data=html.encode('utf-8'),
        headers={"Content-Type": "text/html"}
    )

# ---------------------------------------------------------------------------
# Daemon — async UNIX socket server, 6-hour idle shutdown
# ---------------------------------------------------------------------------

_daemon_last_active: float = 0.0


async def _daemon_dispatch(req: dict) -> list:
    """Route a daemon request to the appropriate function. Returns list of output lines."""
    cmd = req.get('cmd')

    if cmd == 'search':
        limit = req.get('limit', 20)
        if limit == 0:
            limit = None
        all_hits = search_pages(req['query'])
        hits = all_hits[:limit] if limit else all_hits
        lines = [f"{h['title']}  |  {h['notebook']} / {h['section']}" for h in hits]
        if len(all_hits) > len(hits):
            lines.insert(0, f"[{len(all_hits)} matches — showing {len(hits)}, use --limit N for more]")
        return lines or ['No results.']

    # Remaining commands need an API client — import lazily so token cost
    # is only paid when an actual API call is required
    from onenote_setup import make_graph_client
    client = make_graph_client()

    if cmd == 'list-notebooks':
        nbs = await get_notebooks(client)
        return [n['name'] for n in nbs]

    elif cmd == 'list-sections':
        secs = await get_sections(client, req['notebook'])
        return [s['name'] for s in secs]

    elif cmd == 'list-pages':
        pages = await get_pages(client, req['notebook'], req['section'])
        return [p['title'] for p in pages]

    elif cmd == 'read-page':
        result = await find_page(client, req['notebook'], req['section'], req['page'])
        content = result['content']
        max_chars = req.get('max_chars', 4000)
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + f'\n... [truncated — {len(result["content"])} chars total, use --full for complete content]'
        return content.splitlines()

    elif cmd == 'read-page-html':
        result = await find_page(client, req['notebook'], req['section'], req['page'])
        return result['html'].splitlines()

    elif cmd == 'refresh':
        stats = await refresh_notebook(client, req['notebook'])
        return [f"Refreshed '{req['notebook']}': {stats['sections']} sections, {stats['pages']} pages"]

    else:
        raise ValueError(f"Unknown command: {cmd}")


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global _daemon_last_active
    _daemon_last_active = time.time()
    try:
        line = await reader.readline()
        req  = json.loads(line.decode().strip())
        lines = await _daemon_dispatch(req)
        resp  = json.dumps({'status': 'ok', 'lines': lines}) + '\n'
    except Exception as e:
        resp = json.dumps({'status': 'error', 'message': str(e)}) + '\n'
    writer.write(resp.encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _idle_watchdog(server: asyncio.AbstractServer) -> None:
    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        if time.time() - _daemon_last_active > IDLE_TIMEOUT:
            print(f"Daemon idle for {IDLE_TIMEOUT//3600}h — shutting down.", file=sys.stderr)
            server.close()
            if DAEMON_SOCK.exists():
                DAEMON_SOCK.unlink()
            if DAEMON_PID.exists():
                DAEMON_PID.unlink()
            os._exit(0)


def run_daemon() -> None:
    """Start the UNIX socket daemon. Called when --serve is passed."""
    global _daemon_last_active
    _daemon_last_active = time.time()

    # Clean up stale socket
    if DAEMON_SOCK.exists():
        DAEMON_SOCK.unlink()

    DAEMON_PID.write_text(str(os.getpid()))

    async def _serve():
        server = await asyncio.start_unix_server(_handle_client, path=str(DAEMON_SOCK))
        asyncio.create_task(_idle_watchdog(server))
        asyncio.create_task(_background_prepopulate())
        print(f"Daemon started (pid {os.getpid()}, idle timeout {IDLE_TIMEOUT//3600}h)", file=sys.stderr)
        async with server:
            await server.serve_forever()

    try:
        asyncio.run(_serve())
    finally:
        for p in (DAEMON_SOCK, DAEMON_PID):
            if p.exists():
                p.unlink()


# ---------------------------------------------------------------------------
# Daemon client — called from CLI when daemon is running
# ---------------------------------------------------------------------------

def _daemon_running() -> bool:
    if not DAEMON_PID.exists() or not DAEMON_SOCK.exists():
        return False
    try:
        pid = int(DAEMON_PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def _start_daemon_bg() -> None:
    """Start the daemon as a detached background process."""
    import subprocess
    subprocess.Popen(
        [sys.executable, __file__, '--serve'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _call_daemon(request: dict) -> list:
    """Send a request to the running daemon and return output lines."""
    import socket as _socket
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    # Retry briefly in case daemon just started
    for attempt in range(10):
        try:
            sock.connect(str(DAEMON_SOCK))
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if attempt == 9:
                raise RuntimeError("Could not connect to daemon socket.")
            time.sleep(0.2)
    try:
        sock.sendall((json.dumps(request) + '\n').encode())
        data = b''
        while not data.endswith(b'\n'):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    resp = json.loads(data.decode())
    if resp['status'] == 'error':
        raise RuntimeError(resp['message'])
    return resp['lines']


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main_async(args):
    cmd_map = {k: v for k, v in vars(args).items() if k != 'cmd' and v is not None}
    cmd_map['cmd'] = args.cmd

    # search can run without daemon (fast anyway — no API)
    if args.cmd == 'search':
        limit = args.limit if args.limit > 0 else None
        all_hits = search_pages(args.query)
        hits = all_hits[:limit] if limit else all_hits
        lines = [f"{h['title']}  |  {h['notebook']} / {h['section']}" for h in hits]
        if len(all_hits) > len(hits):
            lines.insert(0, f"[{len(all_hits)} matches — showing {len(hits)}, use --limit N for more]")
        for line in (lines or ['No results.']):
            print(line)
        return

    # Try daemon for all other commands
    if _daemon_running():
        try:
            for line in _call_daemon(cmd_map):
                print(line)
            return
        except Exception:
            pass  # Fall through to direct execution if daemon fails

    # Start daemon for next call, run this one directly
    if not _daemon_running():
        _start_daemon_bg()

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
        result = await find_page(client, args.notebook, args.section, args.page)
        content = result['content']
        max_chars = 0 if args.full else args.max_chars
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + f'\n... [truncated — {len(result["content"])} chars total, use --full for complete content]'
        print(content)

    elif args.cmd == 'read-page-html':
        result = await find_page(client, args.notebook, args.section, args.page)
        print(result['html'])

    elif args.cmd == 'refresh':
        stats = await refresh_notebook(client, args.notebook)
        print(f"Refreshed '{args.notebook}': {stats['sections']} sections, {stats['pages']} pages")

    elif args.cmd == 'prepopulate':
        result = await prepopulate_page_ids(client)
        verb = 'Cancelled' if result['cancelled'] else 'Done'
        print(f"{verb}: {result['done']}/{result['total']} sections populated, "
              f"{result['pages']} pages, {result['errors']} errors in {result['elapsed']}s "
              f"({result['skip']} already complete)")

    elif args.cmd == 'prepopulate-status':
        if _PREPOP_STATUS.exists():
            s = json.loads(_PREPOP_STATUS.read_text())
            state = 'running' if s.get('running') else ('cancelled' if s.get('cancelled') else 'done')
            print(f"Status: {state} | {s['done']}/{s['total']} sections | "
                  f"{s['pages']} pages | {s['errors']} errors | "
                  f"{s.get('rate', 0):.1f}/s | {s['elapsed']}s elapsed")
        elif _PREPOP_LOG.exists():
            print(_PREPOP_LOG.read_text().strip())
        else:
            print("No pre-population run found.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='OneNote CLI')
    parser.add_argument('--serve', action='store_true',
                        help='Run as background daemon (started automatically)')
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

    p = sub.add_parser('search')
    p.add_argument('query', help='Search page titles (grep, no API)')
    p.add_argument('--limit', type=int, default=20,
                   help='Max results to show (default 20). Use 0 for all.')

    sub.add_parser('prepopulate',
                   help='Pre-populate page IDs for all sections (live progress bar)')
    sub.add_parser('prepopulate-status',
                   help='Show status of last pre-population run')

    args = parser.parse_args()

    if args.serve:
        run_daemon()
    elif not args.cmd:
        parser.print_help()
    else:
        asyncio.run(main_async(args))
