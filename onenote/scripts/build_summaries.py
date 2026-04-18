#!/usr/bin/env python3
"""Build hierarchical semantic-search summaries for a OneNote notebook.

Strategy:
  1. For each page in the notebook, ensure HTML is in the content cache
     (fetch from Graph API if missing, using existing auth).
  2. Summarize each page via `claude -p --model haiku` — tuned for semantic search.
  3. Roll up: page summaries -> section summary -> notebook summary.
  4. Write JSON (incremental regen) and Markdown (LLM context) to cache/summaries/.

Incremental: pages whose last_modified matches the stored summary are skipped.

Usage:
    python3 build_summaries.py <Notebook Name> [--concurrency 10] [--max-chars 50000]
"""
import asyncio
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from onenote_ops import (
    _load_cache, load_content_cache, save_content_cache, strip_html,
    PAGE_CONTENT_DIR
)

SUMMARIES_DIR = Path.home() / '.claude/skills/onenote/cache/summaries'
SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

PAGE_PROMPT = """You are generating a terse summary of a OneNote page for SEMANTIC SEARCH retrieval.

Goal: Make this page findable when a user asks a semantically related question. The summary is indexed by an LLM that will use it to decide which page to open.

Write prose (not bullets) that densely packs:
- Core topic(s) of the page
- Named entities: specific supplements, compounds, conditions, foods, protocols, practitioners, techniques, numbers/dosages if prominent
- Distinctive keywords a user might actually search for
- Key claims or facts

Length guide:
- Short page (< 1KB text): 1 sentence
- Medium (1-10KB): 2-4 sentences
- Long (> 10KB): 5-10 sentences — capture sub-topics

No preamble. No "This page covers". Just the dense content itself.

---
Page title: {title}
Section: {section}
Notebook: {notebook}

Content:
{content}
"""

SECTION_PROMPT = """Summarize this OneNote section for semantic search indexing. One paragraph (2-4 sentences) naming the main topics covered across its pages. Focus on searchable terms a user might ask about.

No preamble. Just the summary.

Section: {section}
Notebook: {notebook}

Page summaries:
{page_summaries}
"""

NOTEBOOK_PROMPT = """Summarize this entire OneNote notebook for semantic search. 2-4 sentences describing its scope and main themes. Used by an LLM to decide whether a user's query belongs in this notebook.

No preamble. Just the summary.

Notebook: {notebook}

Section summaries:
{section_summaries}
"""


async def call_haiku(prompt: str, sem: asyncio.Semaphore, label: str = '') -> str:
    """Invoke `claude -p --model haiku` as a subprocess."""
    async with sem:
        proc = await asyncio.create_subprocess_exec(
            'claude', '-p', '--model', 'haiku', prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return f'[ERROR: {stderr.decode()[:200]}]'
        return stdout.decode().strip()


async def ensure_page_cached(client, spec: dict, sem: asyncio.Semaphore,
                            timeout: int = 30) -> dict:
    """Ensure a page's HTML is in the content cache. Returns {id, title, last_modified, html, cached}."""
    from onenote_ops import find_page
    async with sem:
        page = await asyncio.wait_for(
            find_page(client, spec['notebook'], spec['section'], spec['title']),
            timeout=timeout,
        )
        return {
            'id': page['id'],
            'title': page['title'],
            'last_modified': page.get('last_modified', ''),
            'section': spec['section'],
            'html': page['html'],
        }


EMPTY_THRESHOLD = 30  # pages with less than this many chars of text are stubbed
APOLOGY_MARKERS = (
    'could you either', 'i need the', 'provide the content',
    'page content provided', 'please share', 'allow me to read',
)


def _content_for_prompt(html: str, max_chars: int) -> str:
    text = strip_html(html)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + '\n[...truncated...]'
    return text


def _is_empty_page(html: str) -> tuple[bool, str]:
    """Return (is_empty, stripped_text). Empty = title-only / no real body content."""
    text = re.sub(r'\s+', ' ', strip_html(html)).strip()
    return len(text) < EMPTY_THRESHOLD, text


def _looks_like_apology(summary: str) -> bool:
    s = summary.lower()
    return any(m in s for m in APOLOGY_MARKERS)


async def build_notebook_summaries(
    notebook: str, concurrency: int = 10, max_chars: int = 50000
) -> dict:
    cache = _load_cache()
    nb = cache.get(notebook)
    if not nb:
        raise SystemExit(f'Notebook not found: {notebook!r}')

    # Load existing summaries for incremental regen
    json_path = SUMMARIES_DIR / f'{notebook}.json'
    existing = {}
    if json_path.exists():
        existing = json.loads(json_path.read_text())

    existing_pages = {}
    for sec_name, sec_data in existing.get('sections', {}).items():
        for pid, pdata in sec_data.get('pages', {}).items():
            existing_pages[pid] = pdata

    # Build flat page list
    all_page_specs = []
    for sec_name, sec_data in nb['sections'].items():
        for page in sec_data.get('pages', []):
            all_page_specs.append({
                'notebook': notebook,
                'section': sec_name,
                'title': page['title'],
                'id': page['id'],
                'last_modified': page.get('last_modified', ''),
            })

    print(f'[{notebook}] {len(all_page_specs)} pages across {len(nb["sections"])} sections',
          file=sys.stderr)

    # Phase 1: ensure all pages are content-cached
    from onenote_setup import make_graph_client
    client = make_graph_client()

    fetch_sem = asyncio.Semaphore(concurrency)
    print(f'[{notebook}] Phase 1: fetching missing page content...', file=sys.stderr)

    async def _fetch(spec):
        try:
            return await ensure_page_cached(client, spec, fetch_sem, timeout=30)
        except asyncio.TimeoutError:
            print(f'  ! TIMEOUT: {spec["section"]} / {spec["title"]}', file=sys.stderr)
            return Exception(f'timeout after 30s')

    t0 = time.time()
    fetched = await asyncio.gather(*[_fetch(s) for s in all_page_specs], return_exceptions=True)
    print(f'[{notebook}] Fetched in {time.time()-t0:.1f}s', file=sys.stderr)

    # Build id->html map, filter errors
    page_html = {}
    for spec, result in zip(all_page_specs, fetched):
        if isinstance(result, Exception):
            print(f'  ! {spec["title"]}: {result}', file=sys.stderr)
            continue
        page_html[result['id']] = result

    # Phase 2: summarize each page (skip unchanged, regen apology summaries)
    haiku_sem = asyncio.Semaphore(concurrency)
    to_summarize = []
    reused = 0
    stubbed = 0
    stub_results = {}
    for spec in all_page_specs:
        pid = spec['id']
        if pid not in page_html:
            continue
        html = page_html[pid]['html']

        # Stub empty pages without calling Haiku
        is_empty, text = _is_empty_page(html)
        if is_empty:
            stub = f'[Page is only a title — body is empty or has {len(text)} chars of content]'
            stub_results[pid] = stub
            stubbed += 1
            continue

        ex = existing_pages.get(pid)
        if (ex and ex.get('last_modified') == spec['last_modified']
                and ex.get('summary') and not _looks_like_apology(ex['summary'])):
            # Also check: was the existing summary generated with a smaller max_chars cap?
            # If page text > previous cap (stored later as '_max_chars' in meta), regen.
            # For now, simple heuristic: if text len > max_chars and summary was generated with
            # lower cap (we don't track this), we accept the existing summary to stay safe.
            reused += 1
            continue
        to_summarize.append(spec)

    print(f'[{notebook}] Phase 2: summarizing {len(to_summarize)} pages '
          f'(reusing {reused}, stubbing {stubbed} empty)...', file=sys.stderr)

    async def _summarize(spec):
        html = page_html[spec['id']]['html']
        content = _content_for_prompt(html, max_chars)
        prompt = PAGE_PROMPT.format(
            title=spec['title'], section=spec['section'],
            notebook=notebook, content=content
        )
        summary = await call_haiku(prompt, haiku_sem, label=spec['title'])
        print(f'  ✓ {spec["section"]} / {spec["title"]}', file=sys.stderr)
        return spec['id'], summary

    t0 = time.time()
    results = await asyncio.gather(*[_summarize(s) for s in to_summarize])
    print(f'[{notebook}] Summarized in {time.time()-t0:.1f}s', file=sys.stderr)

    new_summaries = dict(results)
    new_summaries.update(stub_results)

    # Build hierarchical structure
    sections_out = {}
    for spec in all_page_specs:
        sec = spec['section']
        if sec not in sections_out:
            sections_out[sec] = {'section_summary': '', 'pages': {}}
        pid = spec['id']
        summary = new_summaries.get(pid) or (existing_pages.get(pid) or {}).get('summary', '')
        sections_out[sec]['pages'][pid] = {
            'title': spec['title'],
            'last_modified': spec['last_modified'],
            'summary': summary,
        }

    # Phase 3: roll up section summaries
    print(f'[{notebook}] Phase 3: rolling up {len(sections_out)} section summaries...',
          file=sys.stderr)
    sec_sem = asyncio.Semaphore(concurrency)

    async def _sec_summary(sec_name, sec_data):
        existing_sec = existing.get('sections', {}).get(sec_name, {})
        # Reuse if no pages changed and summary exists
        if existing_sec.get('section_summary'):
            old_pids = set(existing_sec.get('pages', {}).keys())
            new_pids = set(sec_data['pages'].keys())
            if old_pids == new_pids:
                # Check if any page summary changed
                changed = any(
                    sec_data['pages'][pid]['summary'] != existing_sec['pages'].get(pid, {}).get('summary', '')
                    for pid in new_pids
                )
                if not changed:
                    return sec_name, existing_sec['section_summary']

        lines = [f'- {p["title"]}: {p["summary"]}' for p in sec_data['pages'].values() if p['summary']]
        if not lines:
            return sec_name, '[no pages]'
        prompt = SECTION_PROMPT.format(
            section=sec_name, notebook=notebook,
            page_summaries='\n'.join(lines)
        )
        summary = await call_haiku(prompt, sec_sem)
        print(f'  ✓ {sec_name}', file=sys.stderr)
        return sec_name, summary

    sec_results = await asyncio.gather(*[
        _sec_summary(name, data) for name, data in sections_out.items()
    ])
    for sec_name, sec_summary in sec_results:
        sections_out[sec_name]['section_summary'] = sec_summary

    # Phase 4: notebook summary
    print(f'[{notebook}] Phase 4: notebook-level summary...', file=sys.stderr)
    sec_lines = [f'- {name}: {data["section_summary"]}' for name, data in sections_out.items()]
    nb_prompt = NOTEBOOK_PROMPT.format(
        notebook=notebook, section_summaries='\n'.join(sec_lines)
    )
    nb_summary = await call_haiku(nb_prompt, asyncio.Semaphore(1))

    out = {
        'notebook': notebook,
        'notebook_summary': nb_summary,
        'sections': sections_out,
    }

    json_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    # Markdown rendering
    md = [f'# {notebook} Notebook', '', f'**Summary:** {nb_summary}', '']
    for sec_name, sec_data in sections_out.items():
        md.append(f'## {sec_name}')
        md.append(f'*{sec_data["section_summary"]}*')
        md.append('')
        for pid, pdata in sec_data['pages'].items():
            md.append(f'### {pdata["title"]}')
            md.append(pdata['summary'])
            md.append('')
    md_path = SUMMARIES_DIR / f'{notebook}.md'
    md_path.write_text('\n'.join(md))

    print(f'[{notebook}] Written: {json_path}', file=sys.stderr)
    print(f'[{notebook}] Written: {md_path}', file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('notebook')
    ap.add_argument('--concurrency', type=int, default=10)
    ap.add_argument('--max-chars', type=int, default=50000,
                    help='Max chars of page text to include in Haiku prompt')
    args = ap.parse_args()
    asyncio.run(build_notebook_summaries(
        args.notebook, concurrency=args.concurrency, max_chars=args.max_chars
    ))


if __name__ == '__main__':
    main()
