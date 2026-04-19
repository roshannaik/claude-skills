#!python3
"""OneNote Graph API read operations.

Heavy msgraph/msal imports are deferred to first call so cache-only operations
(search, title lookup) stay fast.
"""
import asyncio

from onenote_cache import (
    _load_cache, lookup_notebook, lookup_section, lookup_page,
    update_sections_cache, update_pages_cache,
    load_content_cache, save_content_cache, strip_html,
)


async def get_notebooks(client=None):
    from onenote_setup import make_graph_client, list_notebooks
    if client is None:
        client = make_graph_client()
    return await list_notebooks(client)


async def get_sections(client, notebook_name: str) -> list:
    """Fetch sections. Uses cached last_modified to skip re-fetch when unchanged."""
    from onenote_setup import list_sections, list_notebooks, get_notebook_modified
    nb = lookup_notebook(notebook_name)

    if nb and nb.get('id'):
        nb_id = nb['id']
        if nb.get('last_modified') and nb.get('sections'):
            current_mod = await get_notebook_modified(client, nb_id)
            if current_mod == nb['last_modified']:
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


async def refresh_notebook(client, notebook_name: str) -> dict:
    """Refresh all sections + pages in parallel via asyncio.gather()."""
    from onenote_setup import list_pages
    sections = await get_sections(client, notebook_name)

    async def _fetch(sec):
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
        return {'id': page_id, 'title': page_title, 'content': strip_html(html), 'html': html}

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
    return {'id': page['id'], 'title': page['title'], 'content': strip_html(html), 'html': html}


async def find_pages_batch(client=None, page_specs: list[dict] = None) -> list[dict]:
    """Fetch multiple pages in parallel.

    page_specs = [{'notebook': ..., 'section': ..., 'page': ...}, ...]
    Failed pages include an 'error' key instead of content.

    `client` is optional. If every page in the batch is a cache hit, no Graph
    client is ever constructed.
    """
    async def _fetch(spec):
        try:
            return await find_page(client=client, notebook_name=spec['notebook'],
                                   section_name=spec['section'], page_title=spec['page'])
        except Exception as e:
            return {'title': spec.get('page', ''), 'content': '', 'html': '', 'error': str(e)}
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
