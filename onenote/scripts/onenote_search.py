"""OneNote local search (no API calls).

- search_pages  — title grep via page_index.txt
- search_content — keyword grep across cached page HTML
- _build_compact_index — hierarchical routing index from Haiku summaries
  (kept for now; removed in Phase 5 when Haiku summaries are dropped)
"""
import json
import re
from pathlib import Path

from onenote_cache import _load_page_index, PAGE_CONTENT_DIR

SUMMARIES_DIR = Path(__file__).parent.parent / 'cache' / 'summaries'
_ROUTE_SEC_CHARS = 120


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


def _build_compact_index(notebooks: list) -> str:
    """Build compact routing index from .json summary files.
    Kept for backward compat; removed in Phase 5 with the summaries system."""
    parts = []
    for nb_name in notebooks:
        json_path = SUMMARIES_DIR / f'{nb_name}.json'
        if not json_path.exists():
            continue
        data = json.loads(json_path.read_text())
        nb_summary = data.get('notebook_summary', '')[:200]
        parts.append(f'# {nb_name}\n{nb_summary}\n')
        for sec_name, sec_data in data.get('sections', {}).items():
            sec_sum = sec_data.get('section_summary', '')
            sec_short = (sec_sum[:_ROUTE_SEC_CHARS].rsplit(' ', 1)[0]
                         if len(sec_sum) > _ROUTE_SEC_CHARS else sec_sum)
            page_titles = [
                pdata['title'].strip()
                for pdata in sec_data.get('pages', {}).values()
                if not pdata.get('summary', '').startswith('[Page is only a title')
            ]
            parts.append(f'\n## {sec_name} | {sec_short}')
            if page_titles:
                parts.append(f'Pages: {", ".join(page_titles)}')
    return '\n'.join(parts)
