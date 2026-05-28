#!python3
"""OneNote Graph API read operations.

Heavy msgraph/msal imports are deferred to first call so cache-only operations
(search, title lookup) stay fast.
"""
import asyncio

from onenote_cache import (
    _load_cache, lookup_notebook, lookup_section, lookup_page,
    update_sections_cache, update_pages_cache,
    load_content_cache, save_content_cache, strip_html, html_to_md,
)


async def get_notebooks(client=None):
    from onenote_setup import make_graph_client, list_notebooks
    if client is None:
        client = make_graph_client()
    return await list_notebooks(client)


async def get_sections(client, notebook_name: str) -> list:
    """Fetch sections for a notebook.

    Notebook-level lastModifiedDateTime is unreliable (frozen at creation for
    older notebooks) so we never use it as a freshness signal — sections are
    always fetched live.  If the cached notebook ID returns 404 (stale after a
    Graph migration) we re-discover the current ID via list_notebooks.
    """
    from onenote_setup import list_sections, list_notebooks
    nb = lookup_notebook(notebook_name)
    nb_id = nb.get('id') if nb else None

    if not nb_id:
        notebooks = await list_notebooks(client)
        nb_data = next((n for n in notebooks if n['name'].lower() == notebook_name.lower()), None)
        if not nb_data:
            raise ValueError(f"Notebook '{notebook_name}' not found.")
        nb_id = nb_data['id']

    try:
        sections = await list_sections(client, nb_id)
    except Exception:
        # Cached notebook ID is stale (e.g. Graph 404 after migration); re-discover.
        notebooks = await list_notebooks(client)
        nb_data = next((n for n in notebooks if n['name'].lower() == notebook_name.lower()), None)
        if not nb_data:
            raise ValueError(f"Notebook '{notebook_name}' not found.")
        nb_id = nb_data['id']
        sections = await list_sections(client, nb_id)

    update_sections_cache(notebook_name, sections, nb_id, notebook_modified='')
    return sections


async def get_pages(client, notebook_name: str, section_name: str) -> list:
    """Fetch pages. Uses cached section last_modified to skip re-fetch when unchanged."""
    from onenote_setup import list_pages, get_section_modified
    sec = lookup_section(notebook_name, section_name)

    if sec and sec.get('id'):
        sec_id = sec['id']
        if sec.get('last_modified') and sec.get('pages'):
            current_mod = await get_section_modified(client, sec_id)
            if current_mod == sec['last_modified']:
                return sec['pages']
    else:
        sections = await get_sections(client, notebook_name)
        sec_data = next((s for s in sections if s['name'].lower() == section_name.lower()), None)
        if not sec_data:
            raise ValueError(f"Section '{section_name}' not found in '{notebook_name}'.")
        sec_id = sec_data['id']

    pages = await list_pages(client, sec_id)
    try:
        sec_mod = await get_section_modified(client, sec_id)
    except Exception:
        sec_mod = ''
    update_pages_cache(notebook_name, section_name, pages, section_modified=sec_mod)
    return pages


_SECTION_PAGES_CONCURRENCY = 8   # max concurrent list_pages calls across all notebooks


async def refresh_notebook(client, notebook_name: str,
                           section_sem: asyncio.Semaphore = None) -> dict:
    """Refresh all sections + pages.

    section_sem: shared semaphore from the caller that caps total concurrent
    list_pages calls across all notebooks being refreshed in parallel.
    A per-call default is created when not provided (single-notebook callers).
    """
    from onenote_setup import list_pages
    sections = await get_sections(client, notebook_name)
    sem = section_sem or asyncio.Semaphore(_SECTION_PAGES_CONCURRENCY)

    async def _fetch(sec):
        async with sem:
            pages = await list_pages(client, sec['id'])
        update_pages_cache(notebook_name, sec['name'], pages)
        return len(pages)

    counts = await asyncio.gather(*[_fetch(s) for s in sections])
    return {'sections': len(sections), 'pages': sum(counts)}


async def find_page(client=None, notebook_name: str = None, section_name: str = None,
                    page_title: str = None) -> dict:
    """Find a page and return its content.

    Fast path   (0 API calls, no client needed): page ID cached + content fresh.
    Medium path (1 API call):  page ID cached, content stale/missing.
    Slow path   (2+ API calls): page ID not cached, fetches via API.

    `client` is optional. Only constructed (and msal/msgraph imported) if the
    cache miss path is hit.
    """
    def _lazy_client():
        nonlocal client
        if client is None:
            from onenote_setup import make_graph_client
            client = make_graph_client()
        return client

    cached = lookup_page(notebook_name, section_name, page_title)

    if cached and cached.get('id'):
        page_id  = cached['id']
        last_mod = cached.get('last_modified', '')
        html = load_content_cache(page_id, last_mod)
        if html is None:
            from onenote_setup import get_page_content
            html = await get_page_content(_lazy_client(), page_id)
            save_content_cache(page_id, html, last_mod)
        return {'id': page_id, 'title': page_title, 'content': html_to_md(html), 'html': html}

    pages = await get_pages(_lazy_client(), notebook_name, section_name)
    q = (page_title or '').strip().lower()
    page = next((p for p in pages if p['title'].strip().lower() == q), None)
    if not page:
        raise ValueError(f"Page '{page_title}' not found in {notebook_name}/{section_name}. "
                         f"Available: {[p['title'] for p in pages]}")
    html = load_content_cache(page['id'], page.get('last_modified', ''))
    if html is None:
        from onenote_setup import get_page_content
        html = await get_page_content(_lazy_client(), page['id'])
        save_content_cache(page['id'], html, page.get('last_modified', ''))
    return {'id': page['id'], 'title': page['title'], 'content': html_to_md(html), 'html': html}


_FETCH_CONCURRENCY = 8   # max simultaneous Graph page-content requests
_FETCH_TIMEOUT_S   = 60  # per-page timeout; hung requests become errors, not hangs


async def fetch_pages_by_id(client=None, items: list[dict] = None,
                            on_progress=None, force_refetch: bool = False) -> list[dict]:
    """Fetch pages by page_id directly, bypassing title-based lookup.

    items = [{'page_id': ..., 'last_modified': ..., 'label': '<for progress>'}, ...]

    Use when the caller already knows the exact page_id (e.g. sync.py iterating
    over snapshot diffs). Avoids the title-collision bug in find_page where
    lookup_page returns the first match — duplicate titles within a section
    cause one of the pages to never get fetched.

    force_refetch: skip the local HTML cache and always pull from Graph. Used
    when syncing with --force at section/page scope to recover pages whose
    Graph lastModifiedDateTime is frozen at creation time.

    on_progress: optional callable(item, result) called after each completion.
    """
    def _lazy_client():
        nonlocal client
        if client is None:
            from onenote_setup import make_graph_client
            client = make_graph_client()
        return client

    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch(item):
        import time
        pid = item['page_id']
        lm  = item.get('last_modified', '')
        try:
            html = None if force_refetch else load_content_cache(pid, lm)
            from_cache = html is not None
            t0 = time.perf_counter()
            if html is None:
                from onenote_setup import get_page_content
                async with sem:
                    html = await asyncio.wait_for(
                        get_page_content(_lazy_client(), pid),
                        timeout=_FETCH_TIMEOUT_S,
                    )
                save_content_cache(pid, html, lm)
            elapsed_ms = round((time.perf_counter() - t0) * 1000)
            result = {'id': pid, 'html': html,
                      'html_bytes': len(html.encode()),
                      'elapsed_ms': elapsed_ms,
                      'from_cache': from_cache}
        except asyncio.TimeoutError:
            result = {'id': pid, 'error': f'timed out after {_FETCH_TIMEOUT_S}s'}
        except Exception as e:
            result = {'id': pid, 'error': str(e)}
        if on_progress is not None:
            on_progress(item, result)
        return result

    return list(await asyncio.gather(*[_fetch(i) for i in items]))


async def find_pages_batch(client=None, page_specs: list[dict] = None,
                           on_progress=None) -> list[dict]:
    """Fetch multiple pages in parallel.

    page_specs = [{'notebook': ..., 'section': ..., 'page': ...}, ...]
    Failed pages include an 'error' key instead of content.

    `client` is optional. If every page in the batch is a cache hit, no Graph
    client is ever constructed.

    on_progress: optional callable() called after each page completes (success or failure).
    """
    async def _fetch(spec):
        try:
            result = await find_page(client=client, notebook_name=spec['notebook'],
                                     section_name=spec['section'], page_title=spec['page'])
        except Exception as e:
            result = {'title': spec.get('page', ''), 'content': '', 'html': '', 'error': str(e)}
        if on_progress is not None:
            on_progress(spec, result)
        return result
    return list(await asyncio.gather(*[_fetch(s) for s in page_specs]))


async def refresh_all_notebooks(client) -> dict:
    """Refresh all notebooks in parallel.
    Returns {notebook_name: {'sections': N, 'pages': N}}."""
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
