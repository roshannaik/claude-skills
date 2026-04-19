#!python3
"""Gemini embeddings for OneNote pages.

Builds cache/embeddings.npz (ids + L2-normalized vectors) and
cache/embeddings_meta.json (model + per-page last_modified for incremental rebuild).

Query-time lookup is a single matmul — no vector DB, no ANN index.
1K pages × 1024-dim float32 = ~4MB total.

Requires GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable.

Uses gemini-embedding-001 with Matryoshka truncation to 1024 dims (default is 3072).
1024 is the sweet spot: still near-state-of-the-art quality per Google's published
MRL results, while keeping storage small and query math fast.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from onenote_cache import (
    _load_cache, REFS_DIR, strip_html, load_content_cache, atomic_write,
)


def _atomic_savez(path, **arrays) -> None:
    """numpy.savez with atomic tempfile + os.replace semantics.

    Keep the `.npz` suffix on tmp or numpy will append one itself, silently
    writing to a different filename than we pass to os.replace.
    """
    import numpy as np
    tmp = path.with_name(f'{path.name}.tmp.{os.getpid()}.npz')
    np.savez(tmp, **arrays)
    os.replace(tmp, path)

EMBEDDINGS_NPZ  = REFS_DIR / 'embeddings.npz'
EMBEDDINGS_META = REFS_DIR / 'embeddings_meta.json'

MODEL            = 'gemini-embedding-001'
EMBED_DIM        = 1024           # MRL-truncated from 3072
MAX_CHARS        = 32000          # per-page cap before sending to the API

# Gemini free tier for gemini-embedding-001 is ~5 RPM / 30K TPM.
# A batch of ~10 pages averages 15-25K tokens, safely under TPM.
# Sleeping 15s between batches keeps us under 5 RPM (one call per 12s).
BATCH_SIZE       = 10
INTER_BATCH_SLEEP = 15.0

TASK_DOCUMENT = 'RETRIEVAL_DOCUMENT'
TASK_QUERY    = 'RETRIEVAL_QUERY'


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


def _get_client():
    from google import genai
    api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        raise SystemExit('GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable is not set.')
    return genai.Client(api_key=api_key)


def _embed_call(client, texts: list, task_type: str):
    """Single Gemini embed_content call. Returns list of float lists."""
    from google.genai import types
    resp = client.models.embed_content(
        model=MODEL,
        contents=texts,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=EMBED_DIM,
        ),
    )
    return [e.values for e in resp.embeddings]


# ---------------------------------------------------------------------------
# Building embedding text per page
# ---------------------------------------------------------------------------

def _page_text(notebook: str, section: str, title: str, html: str) -> str:
    """Shape the text fed to the embedding model. Header adds routing signal
    (notebook/section) in case the body is sparse."""
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
    - Skips pages whose HTML is not in the content cache.
    - Writes embeddings.npz + embeddings_meta.json atomically at the end.

    Returns {'total': N, 'rebuilt': N, 'reused': N, 'skipped_no_content': N, 'elapsed': s}
    """
    import numpy as np

    client = _get_client()

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
            if pid in existing_vecs:
                carry.append((pid, existing_vecs[pid]))
            continue

        prev = meta['pages'].get(pid)
        if (prev and prev.get('last_modified') == lm and pid in existing_vecs and not force):
            carry.append((pid, existing_vecs[pid]))
            continue

        html = load_content_cache(pid, lm)
        if html is None:
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
    # Call Gemini in batches — fixed BATCH_SIZE, retry rate limits
    # ---------------------------------------------------------------------
    def _embed_with_retry(texts, max_attempts=8):
        for attempt in range(max_attempts):
            try:
                return _embed_call(client, texts, TASK_DOCUMENT)
            except Exception as e:
                msg = str(e)
                is_rate = ('rate' in msg.lower() or '429' in msg
                           or 'quota' in msg.lower() or 'RESOURCE_EXHAUSTED' in msg)
                if is_rate and attempt < max_attempts - 1:
                    wait = min(60, 4 * (2 ** attempt))
                    print(f"  ! rate limited; sleeping {wait}s (attempt {attempt+1}/{max_attempts})",
                          file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise

    new_vecs: dict = {}
    new_page_meta: dict = {}
    t0 = time.time()

    def _checkpoint():
        """Write a partial .npz + meta so a mid-run crash keeps progress."""
        merged_vecs = {pid: v for pid, v in carry}
        merged_vecs.update(new_vecs)
        merged_meta = {pid: meta['pages'].get(pid, {}) for pid, _ in carry}
        merged_meta.update(new_page_meta)
        valid = {p[3] for p in _enumerate_pages()}
        merged_vecs = {pid: v for pid, v in merged_vecs.items() if pid in valid}
        merged_meta = {pid: m for pid, m in merged_meta.items() if pid in valid}
        sids = sorted(merged_vecs.keys())
        if sids:
            vectors = np.stack([merged_vecs[i] for i in sids]).astype(np.float32)
            _atomic_savez(EMBEDDINGS_NPZ, ids=np.array(sids), vectors=vectors)
        atomic_write(EMBEDDINGS_META, json.dumps({
            'model':    MODEL,
            'dim':      EMBED_DIM,
            'built_at': datetime.now(timezone.utc).isoformat(),
            'pages':    merged_meta,
        }, indent=2))

    try:
        for i in range(0, len(to_embed), BATCH_SIZE):
            batch = to_embed[i:i + BATCH_SIZE]
            texts = [t for _, t, _ in batch]
            try:
                embeddings = _embed_with_retry(texts)
            except Exception as e:
                if len(batch) > 1 and ('payload' in str(e).lower() or 'too large' in str(e).lower()
                                       or 'INVALID_ARGUMENT' in str(e)):
                    print(f"  ! batch of {len(batch)} rejected ({e}); splitting", file=sys.stderr)
                    mid = len(batch) // 2
                    embeddings = _embed_with_retry([t for _, t, _ in batch[:mid]]) + \
                                 _embed_with_retry([t for _, t, _ in batch[mid:]])
                else:
                    raise

            for (pid, _, pmeta), vec in zip(batch, embeddings):
                v = np.asarray(vec, dtype=np.float32)
                n = np.linalg.norm(v)
                if not (n > 0) or not np.isfinite(v).all():
                    print(f"  ! skipping {pid}: non-finite embedding (norm={n})", file=sys.stderr)
                    continue
                v = v / n
                new_vecs[pid] = v
                new_page_meta[pid] = pmeta

            done_n = min(i + BATCH_SIZE, len(to_embed))
            print(f"  [{done_n}/{len(to_embed)}] embedded", file=sys.stderr)

            # Checkpoint every 50 pages so a later failure keeps progress
            if done_n % 50 == 0 or done_n == len(to_embed):
                _checkpoint()

            # Pace for RPM cap — skip on the final batch
            if done_n < len(to_embed):
                time.sleep(INTER_BATCH_SLEEP)
    except KeyboardInterrupt:
        print("\nInterrupted — checkpointing partial progress.", file=sys.stderr)
        _checkpoint()
        raise

    elapsed = time.time() - t0

    # ---------------------------------------------------------------------
    # Merge and persist
    # ---------------------------------------------------------------------
    final_vecs: dict = {pid: vec for pid, vec in carry}
    final_vecs.update(new_vecs)

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
        _atomic_savez(EMBEDDINGS_NPZ, ids=np.array(ids), vectors=vectors)

    atomic_write(EMBEDDINGS_META, json.dumps({
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

    ids, vectors = _load_for_query()
    if not ids:
        return []

    meta = _load_meta()
    page_meta = meta.get('pages', {})

    client = _get_client()

    # Retry on rate-limit, honoring the Retry-After hint in the error
    import re as _re
    last_err = None
    for attempt in range(5):
        try:
            vecs = _embed_call(client, [query], TASK_QUERY)
            break
        except Exception as e:
            msg = str(e)
            is_rate = ('429' in msg or 'RESOURCE_EXHAUSTED' in msg
                       or 'rate' in msg.lower() or 'quota' in msg.lower())
            if not is_rate or attempt == 4:
                raise
            # Parse the "retry in Xs" hint if present, else exponential backoff
            m = _re.search(r'retry in ([\d.]+)s', msg, _re.I)
            wait = min(60, float(m.group(1)) + 1 if m else 4 * (2 ** attempt))
            print(f"  ! rate limited; sleeping {wait:.0f}s (attempt {attempt+1}/5)",
                  file=sys.stderr)
            time.sleep(wait)
            last_err = e
    else:
        raise last_err

    q = np.asarray(vecs[0], dtype=np.float32)
    n = np.linalg.norm(q)
    if n > 0:
        q /= n

    # Cosine since vectors are L2-normalized; clamp any stale bad rows to 0
    vectors = np.nan_to_num(vectors, nan=0.0, posinf=0.0, neginf=0.0)
    scores = vectors @ q

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
