#!python3
"""OneNote local cache layer.

Responsibilities:
- JSON cache of notebook/section/page IDs + last_modified (cache/onenote_cache.json)
- Tab-separated page index rebuilt from the JSON (cache/page_index.txt)
- Per-page HTML content cache (cache/page_content/*.html + .meta)
- Lookup helpers (notebook/section/page by name, case-insensitive)
- Cache update helpers with ID-based rename detection

All readers go through _load_cache() — never read the JSON directly. It has an
in-memory mtime cache so the daemon doesn't re-parse on every call.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

REFS_DIR         = Path(__file__).parent.parent / 'cache'
CACHE_JSON       = REFS_DIR / 'onenote_cache.json'
PAGE_INDEX       = REFS_DIR / 'page_index.txt'
PAGE_CONTENT_DIR = REFS_DIR / 'page_content'
PAGE_CONTENT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Atomic write — tempfile + os.replace (POSIX atomic rename)
# ---------------------------------------------------------------------------
# Concurrent readers never see a partial file: the rename either hasn't
# happened yet (they see the old file) or has happened (they see the new one).

def atomic_write(path: Path, data, *, binary: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.tmp.{os.getpid()}')
    mode = 'wb' if binary else 'w'
    with open(tmp, mode) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

# ---------------------------------------------------------------------------
# JSON cache — with in-memory mtime cache
# ---------------------------------------------------------------------------

_mem_cache: dict = {}
_mem_cache_mtime: float = 0.0

def _load_cache() -> dict:
    """Load JSON cache. Re-reads disk only when file has changed."""
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
    atomic_write(CACHE_JSON, json.dumps(cache, separators=(',', ':')))
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
    atomic_write(PAGE_INDEX, '\n'.join(lines) + '\n')

# ---------------------------------------------------------------------------
# Page index — tab-separated rows, cached in memory
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Lookup helpers — no API, no heavy imports
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Normalize a name for comparison: lowercase + strip whitespace.
    OneNote titles sometimes carry trailing spaces that aren't visible in the UI."""
    return (s or '').strip().lower()


def lookup_notebook(name: str) -> dict:
    cache = _load_cache()
    return next((v for k, v in cache.items()
                 if not k.startswith('_') and _norm(k) == _norm(name)), None)

def lookup_section(notebook: str, section: str) -> dict:
    nb = lookup_notebook(notebook)
    if not nb:
        return None
    return next((v for k, v in nb.get('sections', {}).items()
                 if _norm(k) == _norm(section)), None)

def lookup_page(notebook: str, section: str, title: str) -> dict:
    sec = lookup_section(notebook, section)
    if not sec:
        return None
    for page in sec.get('pages', []):
        t = page['title'] if isinstance(page, dict) else page
        if _norm(t) == _norm(title):
            return page if isinstance(page, dict) else {'title': t, 'id': '', 'last_modified': ''}
    return None

# ---------------------------------------------------------------------------
# Content cache — keyed by page_id, invalidated by last_modified
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
    # Write HTML first, then .meta — readers key off .meta. If reader arrives
    # between the two atomic renames, .meta still shows the OLD timestamp, so
    # load_content_cache returns None (forces a refetch) instead of returning
    # new HTML paired with an old meta.
    p = _content_path(page_id)
    atomic_write(p, html)
    if last_modified:
        atomic_write(p.with_suffix('.meta'), last_modified)

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

    id_to_existing_name = {v['id']: k for k, v in existing_secs.items() if v.get('id')}

    new_secs = {}
    for s in sections:
        old_name = id_to_existing_name.get(s['id'])
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

    existing_pages = cache[nb_key]['sections'][sec_key].get('pages', [])
    id_to_old_title = {p['id']: p['title'] for p in existing_pages if p.get('id')}

    new_pages = []
    for p in pages:
        old_title = id_to_old_title.get(p['id'])
        if old_title and old_title != p['title']:
            pass  # rename detected; title updated below
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
