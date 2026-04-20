#!python3
"""Classify each cached OneNote page's subject using Gemini flash.

Outputs `cache/page_subjects.json`:
    {
      "<page_id>": "self" | "general" | "<Person>",
      ...
    }

Candidates are derived from the cache:
  - "self"       = about the user personally
  - "general"    = educational / reference / protocol content not tied to a person
  - "<Person>"   = a specific other person (Dad, Mom, Deekshma, Amit, ...)

Idempotent: reads existing page_subjects.json and only classifies pages not
already present (use --force to reclassify everything). Manual overrides in
cache/subject_overrides.json take precedence at query time.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from onenote_cache import _load_cache, _content_path, load_content_cache, strip_html, REFS_DIR
from onenote_media import parse_resources, load_ocr, load_caption


PAGE_SUBJECTS_JSON  = REFS_DIR / 'page_subjects.json'
SUBJECT_OVERRIDES   = REFS_DIR / 'subject_overrides.json'

CLASSIFIER_MODEL    = 'gemini-2.5-flash'
BODY_CHAR_CAP       = 1800
OCR_SNIPPET_CAP     = 400
CONCURRENCY         = 8           # parallel classifier calls


def _gather_candidates(cache: dict) -> list:
    """Return the person-name candidate list."""
    names = set()

    health = cache.get('Health', {}).get('sections', {})
    # Explicit person-named sections
    for sec in ('Dad', 'Mom', 'Deekshma', 'Suchi'):
        if sec in health:
            names.add(sec)
    # Suchi often appears as a mixed-in person even without her own section
    names.add('Suchi')

    # Health/Others pages each about a distinct person
    TRAILING = re.compile(
        r'\s+(uncle|aunty|tiddi|info|topics?|cancer|c19|'
        r'mom|dad|info\b).*$', re.I)
    for p in health.get('Others', {}).get('pages', []):
        title = p.get('title', '').strip()
        # Normalize curly apostrophes to straight, then take first chunk
        # before " - " / apostrophe-s / apostrophe
        norm = title.replace('\u2019', "'").replace('\u2018', "'")
        first = re.split(r"\s-\s|'s\b|'", norm, maxsplit=1)[0].strip()
        first = TRAILING.sub('', first).strip()
        if first and len(first) <= 20:
            names.add(first)

    # Whole Suchi health notebook
    if 'Suchi health' in cache:
        names.add('Suchi')

    return sorted(names)


def _load_page_subjects() -> dict:
    if PAGE_SUBJECTS_JSON.exists():
        return json.loads(PAGE_SUBJECTS_JSON.read_text())
    return {}


def _atomic_write(path: Path, data: str):
    tmp = path.with_name(path.name + f'.tmp.{os.getpid()}')
    tmp.write_text(data)
    os.replace(tmp, path)


def _page_context(pid: str, html: str) -> tuple:
    """Return (body_snippet, ocr_snippets)."""
    body = strip_html(html)[:BODY_CHAR_CAP]
    ocr_snippets = []
    for ref in parse_resources(html):
        if ref['kind'] != 'image':
            continue
        txt = load_ocr(ref['resource_id']).strip()
        if len(txt) >= 20:
            ocr_snippets.append(f'- {ref["filename"]}: {txt[:OCR_SNIPPET_CAP]}')
        else:
            cap = load_caption(ref['resource_id']).strip()
            if len(cap) >= 20:
                ocr_snippets.append(f'- {ref["filename"]} (caption): {cap[:OCR_SNIPPET_CAP]}')
        if len(ocr_snippets) >= 3:
            break
    return body, ocr_snippets


def _build_prompt(page_meta: dict, body: str, ocr_snippets: list, candidates: list) -> str:
    cand_list = '\n'.join(f'  - {c}' for c in ['self', 'general'] + list(candidates))
    ocr_block = '\n'.join(ocr_snippets) or '(none)'
    return (
        'Classify the subject of this OneNote page. The page belongs to Roshan, '
        'who uses this notebook for personal notes plus reference material.\n\n'
        'Output exactly one label from the list below (copy the label verbatim, '
        'nothing else on the line):\n'
        f'{cand_list}\n\n'
        'Guidance:\n'
        '- "self": page is about Roshan himself (his personal journal, his tests, '
        'his treatments, his plans, his observations).\n'
        '- "general": reference / educational / protocol / research content, not '
        'tied to any specific person (anatomy, nutrients, drug mechanisms, etc.).\n'
        '- <Person name>: page is about that specific other person (their health, '
        'their tests, their care).\n\n'
        'Note: a person-named section is a hint but not authoritative. A page '
        'under "Health / Dad" containing only general thyroid reference info is '
        '"general", not "Dad". A page under "Health / Colitis" that is clearly '
        'Roshan\'s personal journal is "self".\n\n'
        'Page metadata:\n'
        f'  Notebook: {page_meta["notebook"]}\n'
        f'  Section : {page_meta["section"]}\n'
        f'  Title   : {page_meta["title"]}\n\n'
        'Body excerpt:\n'
        f'{body}\n\n'
        'OCR / caption excerpts from embedded images:\n'
        f'{ocr_block}\n\n'
        'Label:'
    )


def classify_pages(force: bool = False, pages_file: str = None) -> dict:
    from google import genai
    api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        raise SystemExit('GEMINI_API_KEY not set.')
    client = genai.Client(api_key=api_key)

    cache = _load_cache()
    candidates = _gather_candidates(cache)
    valid_labels = set(['self', 'general'] + candidates)
    print(f'Candidates: {candidates}', file=sys.stderr)

    existing = {} if force else _load_page_subjects()
    targets = []

    # Build target list
    if pages_file:
        want = set(l.strip() for l in Path(pages_file).read_text().splitlines()
                   if l.strip() and not l.startswith('#'))
    else:
        want = None

    for nb, nbd in cache.items():
        if nb.startswith('_'): continue
        for sec, sd in nbd.get('sections', {}).items():
            for p in sd.get('pages', []):
                if not isinstance(p, dict) or not p.get('id'): continue
                pid = p['id']
                label_key = f'{nb} / {sec} / {p["title"]}'
                if want is not None and label_key not in want and pid not in want:
                    continue
                if pid in existing:
                    continue
                html = load_content_cache(pid, p.get('last_modified', ''))
                if html is None:
                    continue
                targets.append((pid, nb, sec, p['title'], html))

    print(f'Classifying {len(targets)} pages '
          f'(skipping {len(existing)} already labelled)...', file=sys.stderr)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    out = dict(existing)
    lock = threading.Lock()
    t0 = time.time()
    CHECKPOINT_EVERY = 40

    def _classify_one(task):
        pid, nb, sec, title, html = task
        body, ocr_snippets = _page_context(pid, html)
        prompt = _build_prompt({'notebook': nb, 'section': sec, 'title': title},
                                body, ocr_snippets, candidates)
        for attempt in range(6):
            try:
                resp = client.models.generate_content(
                    model=CLASSIFIER_MODEL, contents=[prompt])
                label = (resp.text or '').strip().split('\n')[0].strip()
                return pid, nb, sec, title, label, None
            except Exception as e:
                msg = str(e)
                is_transient = ('429' in msg or '503' in msg or 'UNAVAILABLE' in msg
                                or 'RESOURCE_EXHAUSTED' in msg or 'rate' in msg.lower())
                if attempt == 5 or not is_transient:
                    return pid, nb, sec, title, None, msg[:160]
                time.sleep(min(30, 2 * (2 ** attempt)))
        return pid, nb, sec, title, None, 'exhausted retries'

    done = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(_classify_one, t): t for t in targets}
        for fut in as_completed(futures):
            pid, nb, sec, title, label, err = fut.result()
            done += 1
            if err:
                errors += 1
                print(f'  ! {nb}/{sec}/{title}: {err}', file=sys.stderr)
                continue
            if label not in valid_labels:
                fallback = sec if sec in valid_labels else 'general'
                print(f'  ? unexpected label "{label[:40]}" for {nb}/{sec}/{title} '
                      f'→ falling back to "{fallback}"', file=sys.stderr)
                label = fallback
            with lock:
                out[pid] = label
                if done % 20 == 0 or done == len(targets):
                    print(f'  [{done}/{len(targets)}] {label:10s}  '
                          f'{nb} / {sec} / {title[:40]}  '
                          f'({(time.time()-t0):.0f}s, {done/max((time.time()-t0),1)*60:.0f}/min)',
                          file=sys.stderr)
                if done % CHECKPOINT_EVERY == 0 or done == len(targets):
                    _atomic_write(PAGE_SUBJECTS_JSON, json.dumps(out, indent=2))

    _atomic_write(PAGE_SUBJECTS_JSON, json.dumps(out, indent=2))
    print(f'Done. {len(out)} labels saved to {PAGE_SUBJECTS_JSON} '
          f'in {time.time()-t0:.0f}s', file=sys.stderr)

    # Summary of label distribution
    from collections import Counter
    dist = Counter(out.values())
    print('Distribution:', file=sys.stderr)
    for k, v in sorted(dist.items(), key=lambda x: -x[1]):
        print(f'  {k:15s} {v:>5}', file=sys.stderr)

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true',
                    help='Reclassify every page (ignore existing labels)')
    ap.add_argument('--pages-file', metavar='PATH',
                    help='Limit to pages in this file (one identifier per line)')
    args = ap.parse_args()
    classify_pages(force=args.force, pages_file=args.pages_file)


if __name__ == '__main__':
    main()
