"""Voyage embeddings for OneNote pages.

Builds cache/embeddings.npz (ids + L2-normalized vectors) and
cache/embeddings_meta.json (model + per-page last_modified for incremental rebuild).

Query-time lookup is a single matmul — no vector DB, no ANN index.
1K pages × 1024-dim float32 = ~4MB total.

Requires VOYAGE_API_KEY environment variable.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from onenote_cache import (
    _load_cache, REFS_DIR, PAGE_CONTENT_DIR, strip_html,
    load_content_cache,
)

EMBEDDINGS_NPZ  = REFS_DIR / 'embeddings.npz'
EMBEDDINGS_META = REFS_DIR / 'embeddings_meta.json'

MODEL            = 'voyage-3'
EMBED_DIM        = 1024
MAX_CHARS        = 32000          # per-page cap before sending to Voyage
BATCH_SIZE       = 64             # texts per Voyage API call
MAX_BATCH_TOKENS = 900_000        # voyage-3 limit is 1M tokens/batch; leave headroom


# ---------------------------------------------------------------------------
# Loading existing state
# ---------------------------------------------------------------------------

def _load_meta() -> dict:
    if EMBEDDINGS_META.exists():
        return json.loads(EMBEDDINGS_META.read_text())
    return {'model': MODEL, 'pages': {}}


def _load_vectors() -> tuple:
    """Return (ids_list, vectors_dict) — vectors_dict maps page_id -> np.ndarray."""
    import numpy as np
    if not EMBEDDINGS_NPZ.exists():
        return [], {}
    data = np.load(EMBEDDINGS_NPZ, allow_pickle=False)
    ids = [s for s in data['ids'].tolist()]
    vecs = data['vectors']
    return ids, {pid: vecs[i] for i, pid in enumerate(ids)}


# ---------------------------------------------------------------------------
# Building embedding text per page
# ---------------------------------------------------------------------------

def _page_text(notebook: str, section: str, title: str, html: str) -> str:
    """Shape the text fed to Voyage. Header adds routing signal (notebook/section)
    in case the body is sparse."""
    body = strip_html(html)
    header = f"Notebook: {notebook}\nSection: {section}\nTitle: {title}\n\n"
    combined = header + body
    if len(combined) > MAX_CHARS:
        combined = combined[:MAX_CHARS]
    return combined


def _enumerate_pages():
    """Yield (notebook, section, title, page_id, last_modified) for every page in the cache."""
    cache = _load_cache()
    for nb_name, nb_data in cache.items():
        if nb_name.startswith('_'):
            continue
        for sec_name, sec_data in nb_data.get('sections', {}).items():
            for page in sec_data.get('pages', []):
                if not isinstance(page, dict) or not page.get('id'):
                    continue
                yield (nb_name, sec_name, page['title'], page['id'],
                       page.get('last_modified', ''))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_embeddings(force: bool = False, notebook_filter: set = None) -> dict:
    """Build or incrementally update embeddings.

    - Skips pages whose last_modified matches existing meta (unless force=True).
    - Skips pages whose HTML is not in the content cache (no API fallback here —
      run `refresh` or `read-page` to populate content first).
    - Writes embeddings.npz + embeddings_meta.json atomically at the end.

    Returns {'total': N, 'rebuilt': N, 'reused': N, 'skipped_no_content': N, 'elapsed': s}
    """
    import numpy as np
    import voyageai

    api_key = os.environ.get('VOYAGE_API_KEY')
    if not api_key:
        raise SystemExit('VOYAGE_API_KEY environment variable is not set.')

    vo = voyageai.Client(api_key=api_key)

    meta = _load_meta()
    # Invalidate everything if the model changed
    if meta.get('model') != MODEL:
        meta = {'model': MODEL, 'pages': {}}
        force = True

    existing_ids, existing_vecs = _load_vectors()
    if force:
        existing_ids, existing_vecs = [], {}

    # Decide what needs (re)embedding vs what can be carried forward
    to_embed = []    # list of (page_id, text, meta_dict)
    carry    = []    # list of (page_id, vec) to carry forward
    skipped_no_content = 0

    for nb, sec, title, pid, lm in _enumerate_pages():
        if notebook_filter and nb not in notebook_filter:
            # Still carry forward if present
            if pid in existing_vecs:
                carry.append((pid, existing_vecs[pid]))
            continue

        prev = meta['pages'].get(pid)
        if (prev and prev.get('last_modified') == lm and pid in existing_vecs and not force):
            carry.append((pid, existing_vecs[pid]))
            continue

        html = load_content_cache(pid, lm)
        if html is None:
            # No fresh cached HTML — carry forward old embedding if we have one
            if pid in existing_vecs:
                carry.append((pid, existing_vecs[pid]))
            skipped_no_content += 1
            continue

        text = _page_text(nb, sec, title, html)
        to_embed.append((pid, text, {
            'notebook': nb, 'section': sec, 'title': title,
            'last_modified': lm, 'text_len': len(text),
        }))

    print(f"Pages: {len(to_embed)} to embed, {len(carry)} reused, "
          f"{skipped_no_content} skipped (no cached HTML)", file=sys.stderr)

    if not to_embed:
        print("Nothing to embed.", file=sys.stderr)

    # ---------------------------------------------------------------------
    # Call Voyage in batches — respect per-batch token limit, not just count
    # ---------------------------------------------------------------------
    new_vecs: dict = {}
    new_page_meta: dict = {}
    t0 = time.time()

    i = 0
    total_tokens = 0
    while i < len(to_embed):
        # Build a batch up to BATCH_SIZE texts or MAX_BATCH_TOKENS tokens
        batch_end = i
        batch_token_est = 0
        while batch_end < len(to_embed) and batch_end - i < BATCH_SIZE:
            # Rough estimate: 1 token ≈ 4 chars
            est = max(1, len(to_embed[batch_end][1]) // 4)
            if batch_token_est + est > MAX_BATCH_TOKENS and batch_end > i:
                break
            batch_token_est += est
            batch_end += 1

        batch = to_embed[i:batch_end]
        texts = [t for _, t, _ in batch]

        try:
            resp = vo.embed(texts, model=MODEL, input_type='document', truncation=True)
        except Exception as e:
            # If the batch is too big, halve it and retry
            if len(batch) > 1 and ('token' in str(e).lower() or 'limit' in str(e).lower()):
                print(f"  ! batch of {len(batch)} rejected ({e}); splitting", file=sys.stderr)
                mid = len(batch) // 2
                resp1 = vo.embed([t for _, t, _ in batch[:mid]], model=MODEL,
                                 input_type='document', truncation=True)
                resp2 = vo.embed([t for _, t, _ in batch[mid:]], model=MODEL,
                                 input_type='document', truncation=True)
                embeddings = resp1.embeddings + resp2.embeddings
                total_tokens += getattr(resp1, 'total_tokens', 0) + getattr(resp2, 'total_tokens', 0)
            else:
                raise
        else:
            embeddings = resp.embeddings
            total_tokens += getattr(resp, 'total_tokens', 0)

        for (pid, _, pmeta), vec in zip(batch, embeddings):
            v = np.asarray(vec, dtype=np.float32)
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
            new_vecs[pid] = v
            new_page_meta[pid] = pmeta

        print(f"  [{batch_end}/{len(to_embed)}] embedded", file=sys.stderr)
        i = batch_end

    elapsed = time.time() - t0

    # ---------------------------------------------------------------------
    # Merge and persist
    # ---------------------------------------------------------------------
    final_vecs: dict = {pid: vec for pid, vec in carry}
    final_vecs.update(new_vecs)

    # Preserve meta for carried pages, update for new ones
    final_meta_pages = {
        pid: meta['pages'].get(pid, {}) for pid, _ in carry
    }
    final_meta_pages.update(new_page_meta)

    # Drop any entries for pages no longer in the cache
    valid_ids = {p[3] for p in _enumerate_pages()}
    final_vecs = {pid: v for pid, v in final_vecs.items() if pid in valid_ids}
    final_meta_pages = {pid: m for pid, m in final_meta_pages.items() if pid in valid_ids}

    ids = sorted(final_vecs.keys())
    if ids:
        vectors = np.stack([final_vecs[i] for i in ids]).astype(np.float32)
        np.savez(EMBEDDINGS_NPZ, ids=np.array(ids), vectors=vectors)

    EMBEDDINGS_META.write_text(json.dumps({
        'model':    MODEL,
        'dim':      EMBED_DIM,
        'built_at': datetime.now(timezone.utc).isoformat(),
        'pages':    final_meta_pages,
    }, indent=2))

    return {
        'total': len(ids),
        'rebuilt': len(new_vecs),
        'reused': len(carry),
        'skipped_no_content': skipped_no_content,
        'elapsed': round(elapsed, 1),
        'tokens': total_tokens,
    }


# ---------------------------------------------------------------------------
# Query-time lookup
# ---------------------------------------------------------------------------

_query_cache_mtime = 0.0
_query_ids = None
_query_vectors = None


def _load_for_query():
    """Load embeddings into memory once; reload only when .npz mtime changes."""
    import numpy as np
    global _query_cache_mtime, _query_ids, _query_vectors
    if not EMBEDDINGS_NPZ.exists():
        raise SystemExit(
            'No embeddings found. Run: python3 scripts/build_embeddings.py'
        )
    mtime = EMBEDDINGS_NPZ.stat().st_mtime
    if mtime > _query_cache_mtime:
        data = np.load(EMBEDDINGS_NPZ, allow_pickle=False)
        _query_ids     = data['ids'].tolist()
        _query_vectors = data['vectors']
        _query_cache_mtime = mtime
    return _query_ids, _query_vectors


def semantic_search(query: str, top_k: int = 10,
                    notebook: str = None) -> list:
    """Return top-K pages matching the query semantically.

    Each hit: {page_id, notebook, section, title, score, last_modified}.
    `notebook` filter restricts to a single notebook name (case-insensitive).
    """
    import numpy as np
    import voyageai

    api_key = os.environ.get('VOYAGE_API_KEY')
    if not api_key:
        raise SystemExit('VOYAGE_API_KEY environment variable is not set.')

    ids, vectors = _load_for_query()
    if not ids:
        return []

    meta = _load_meta()
    page_meta = meta.get('pages', {})

    vo = voyageai.Client(api_key=api_key)
    resp = vo.embed([query], model=MODEL, input_type='query', truncation=True)
    q = np.asarray(resp.embeddings[0], dtype=np.float32)
    n = np.linalg.norm(q)
    if n > 0:
        q /= n

    # Cosine since vectors are L2-normalized
    scores = vectors @ q

    # Optional notebook filter: mask out other-notebook scores
    if notebook:
        nb_lower = notebook.lower()
        for idx, pid in enumerate(ids):
            pm = page_meta.get(pid, {})
            if pm.get('notebook', '').lower() != nb_lower:
                scores[idx] = -1.0

    k = min(top_k, len(ids))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]

    hits = []
    for idx in top:
        pid = ids[idx]
        pm = page_meta.get(pid, {})
        hits.append({
            'page_id':       pid,
            'notebook':      pm.get('notebook', ''),
            'section':       pm.get('section', ''),
            'title':         pm.get('title', ''),
            'last_modified': pm.get('last_modified', ''),
            'score':         float(scores[idx]),
        })
    return hits
