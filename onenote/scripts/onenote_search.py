"""OneNote local search (no API calls).

- search_pages   — title grep via page_index.txt
- search_content — keyword grep across cached page HTML

Semantic search lives in onenote_embeddings.semantic_search — prefer it over
these for anything beyond exact-title / exact-keyword lookups.
"""
import re

from onenote_cache import _load_page_index, PAGE_CONTENT_DIR


def search_pages(query: str, limit: int = None) -> list:
    """Search page_index.txt in-process. Fast — no API, no heavy imports."""
    q = query.lower()
    hits = []
    for parts in _load_page_index():
        if len(parts) >= 3 and q in parts[0].lower():
            hits.append({'title': parts[0], 'notebook': parts[1], 'section': parts[2],
                         'id': parts[3] if len(parts) > 3 else ''})
    return hits[:limit] if limit else hits


def search_content(query: str, context_chars: int = 200, limit: int = None) -> list:
    """Grep cached page HTML. No API — offline only."""
    q = query.lower()
    id_meta = {}
    for parts in _load_page_index():
        if len(parts) >= 4:
            id_meta[parts[3]] = {'title': parts[0], 'notebook': parts[1], 'section': parts[2]}

    hits = []
    for html_file in sorted(PAGE_CONTENT_DIR.glob('*.html')):
        page_id_safe = html_file.stem
        page_id_candidates = [page_id_safe.replace('_', '!', 2)]
        meta = None
        for pid in page_id_candidates:
            if pid in id_meta:
                meta = id_meta[pid]
                break
        if meta is None:
            for pid, m in id_meta.items():
                safe = pid.replace('!', '_').replace('/', '_')
                if safe == page_id_safe:
                    meta = m
                    break
        if meta is None:
            continue

        text = re.sub(r'<[^>]+>', ' ', html_file.read_text())
        text = re.sub(r'\s+', ' ', text)
        text_lower = text.lower()

        snippets = []
        idx = 0
        while True:
            i = text_lower.find(q, idx)
            if i == -1:
                break
            start = max(0, i - context_chars // 2)
            end = min(len(text), i + len(q) + context_chars // 2)
            snippets.append(text[start:end].strip())
            idx = i + 1

        if snippets:
            hits.append({**meta, 'snippets': snippets})

    return hits[:limit] if limit else hits
