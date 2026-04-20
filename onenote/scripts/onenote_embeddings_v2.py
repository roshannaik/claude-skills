#!python3
"""Phase-2 embeddings: chunked, multimodal, unified vector space.

Uses `gemini-embedding-2-preview` with `output_dimensionality=768`. Text chunks,
image bytes, PDF bytes, and audio bytes all land in the same vector space, so a
single text query matmul surfaces any modality.

Storage (prototype-isolated from v1):
    cache/embeddings_v2.npz         ids, vectors, kinds
    cache/embeddings_v2_meta.json   model, dim, pages, chunks

Incremental rebuild: a page whose last_modified matches is carried forward
(all its chunks). A page that changed has all its chunk rows dropped and is
re-chunked + re-embedded.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from onenote_cache import (
    _load_cache, REFS_DIR, load_content_cache, atomic_write, atomic_savez,
    iter_all_pages, pages_by_id,
    PAGE_SUBJECTS_JSON, SUBJECT_OVERRIDES,
)
from onenote_chunks import chunk_page, Chunk
from onenote_genai import get_client, with_retry


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MODEL               = 'gemini-embedding-2-preview'
EMBED_DIM           = 768
# gemini-embedding-2-preview returns exactly ONE embedding per call, even when
# given multiple contents (the model concatenates the list into a single
# document). So we call one chunk at a time.
INTER_CALL_SLEEP    = 0.3     # between embed calls to stay under RPM
MAX_RETRIES         = 6
CHECKPOINT_EVERY    = 50      # save partial state every N embed successes

EMBEDDINGS_NPZ      = REFS_DIR / 'embeddings_v2.npz'
EMBEDDINGS_META     = REFS_DIR / 'embeddings_v2_meta.json'

USER_SELF_LABEL     = 'self'   # label used for pages about the user


# ---------------------------------------------------------------------------
# Embed call with hard timeout + retry
# ---------------------------------------------------------------------------

_EMBED_CALL_TIMEOUT_SEC = 90   # hard timeout per embed_content call


def _get_client():
    # Kept for backward-compat with callers that may import this symbol.
    return get_client()


def _embed_call(client, contents: list, task_type: str):
    """embed_content with a hard per-call timeout so a stalled TCP doesn't
    hang the whole build. Runs the sync SDK call in a worker thread."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
    from google.genai import types

    def _do():
        resp = client.models.embed_content(
            model=MODEL,
            contents=contents,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBED_DIM,
            ),
        )
        return [e.values for e in resp.embeddings]

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_do)
        try:
            return fut.result(timeout=_EMBED_CALL_TIMEOUT_SEC)
        except FutTimeout:
            fut.cancel()
            raise TimeoutError(f'embed_content call exceeded {_EMBED_CALL_TIMEOUT_SEC}s')


def _embed_with_retry(client, contents: list, task_type: str):
    return with_retry(_embed_call, client, contents, task_type,
                       max_attempts=MAX_RETRIES, base_wait=4.0, max_wait=60.0,
                       label='embed')


# ---------------------------------------------------------------------------
# Content packaging per chunk
# ---------------------------------------------------------------------------

def _chunk_content(chunk: Chunk):
    """Return the content to hand to embed_content for one chunk.

    Text-like kinds return a single string. Media kinds return a single Part
    carrying raw bytes."""
    from google.genai import types
    from onenote_media import load_resource

    if chunk.kind in ('text', 'summary', 'image_ocr', 'image_caption',
                       'audio_transcript', 'video_transcript'):
        return chunk.embed_text

    # Raw media (image / pdf / audio)
    data, meta = load_resource(chunk.resource_id)
    if data is None:
        raise RuntimeError(f'Bytes not cached for resource {chunk.resource_id}')
    return types.Part.from_bytes(data=data, mime_type=chunk.mime)


def _is_text_kind(chunk: Chunk) -> bool:
    return chunk.kind in ('text', 'summary', 'image_ocr', 'image_caption',
                          'audio_transcript', 'video_transcript')


# ---------------------------------------------------------------------------
# State load/save
# ---------------------------------------------------------------------------

def _load_meta() -> dict:
    if EMBEDDINGS_META.exists():
        return json.loads(EMBEDDINGS_META.read_text())
    return {'model': MODEL, 'dim': EMBED_DIM, 'pages': {}, 'chunks': {}}


def _load_vectors():
    import numpy as np
    if not EMBEDDINGS_NPZ.exists():
        return {}
    data = np.load(EMBEDDINGS_NPZ, allow_pickle=False)
    ids = data['ids'].tolist()
    vecs = data['vectors']
    return {cid: vecs[i] for i, cid in enumerate(ids)}


def _save_state(vectors: dict, kinds: dict, pages_meta: dict, chunks_meta: dict):
    import numpy as np
    if not vectors:
        print('Nothing to save.', file=sys.stderr)
        return
    ids = sorted(vectors.keys())
    vecs = np.stack([vectors[i] for i in ids]).astype(np.float32)
    kinds_arr = np.array([kinds[i] for i in ids])
    atomic_savez(EMBEDDINGS_NPZ, ids=np.array(ids), vectors=vecs, kinds=kinds_arr)
    atomic_write(EMBEDDINGS_META, json.dumps({
        'model':    MODEL,
        'dim':      EMBED_DIM,
        'built_at': datetime.now(timezone.utc).isoformat(),
        'pages':    pages_meta,
        'chunks':   chunks_meta,
    }, indent=2))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _resolve_page_ids(page_ids: list, pages_file: str) -> list:
    """Resolve a mix of page_id / 'Notebook / Section / Title' strings to page_ids."""
    idents = list(page_ids or [])
    if pages_file:
        lines = Path(pages_file).read_text().splitlines()
        idents.extend(l.strip() for l in lines if l.strip() and not l.startswith('#'))

    by_id = pages_by_id()
    by_path = {f'{nb.lower()}/{sec.lower()}/{p["title"].lower()}': pid
               for pid, (nb, sec, p) in by_id.items()}

    resolved = []
    missing = []
    for ident in idents:
        if '!' in ident and '/' not in ident.split('!', 1)[0]:
            if ident in by_id:
                resolved.append(ident)
            else:
                missing.append(ident)
            continue
        parts = [x.strip() for x in ident.split('/')]
        if len(parts) < 3:
            missing.append(ident); continue
        nb, sec = parts[0], parts[1]
        title = '/'.join(parts[2:])
        key = f'{nb.lower()}/{sec.lower()}/{title.lower()}'
        pid = by_path.get(key)
        if pid:
            resolved.append(pid)
        else:
            missing.append(ident)
    if missing:
        print(f'Warning: unresolved identifiers: {missing}', file=sys.stderr)
    return resolved


def build_embeddings(page_ids: list = None, pages_file: str = None,
                     force: bool = False) -> dict:
    """(Re)build the v2 embedding store for a subset of pages.

    Args:
        page_ids / pages_file: subset of pages. If both None, ALL cached pages.
        force: ignore last_modified and re-embed every chunk.

    Returns summary dict.
    """
    import numpy as np

    client = _get_client()
    meta = _load_meta()
    # Model change → full rebuild
    if meta.get('model') != MODEL or meta.get('dim') != EMBED_DIM:
        print(f'  model/dim changed ({meta.get("model")}, {meta.get("dim")}) → '
              f'({MODEL}, {EMBED_DIM}); wiping v2 state', file=sys.stderr)
        meta = {'model': MODEL, 'dim': EMBED_DIM, 'pages': {}, 'chunks': {}}
        force = True

    existing_vecs = {} if force else _load_vectors()

    # Build a single {pid: (nb, sec, page)} index so both the full-corpus
    # target list and the per-pid metadata lookup are O(1). Previously the
    # per-pid lookup did a full notebook/section/page scan per target pid,
    # which was O(P×N) for P targets across N pages.
    pid_index = pages_by_id()

    if page_ids or pages_file:
        target_pids = _resolve_page_ids(page_ids, pages_file)
    else:
        target_pids = list(pid_index.keys())
    if not target_pids:
        return {'error': 'no pages resolved'}

    all_chunks = []            # list of (chunk, page_id, page_meta_dict)
    pages_to_rebuild = {}

    for pid in target_pids:
        row = pid_index.get(pid)
        if not row:
            continue
        nb, sec, p = row
        pid_lm = p.get('last_modified', '')
        prev = meta['pages'].get(pid, {})

        if (not force
                and prev.get('last_modified') == pid_lm
                and all(cid in existing_vecs for cid in prev.get('chunk_ids', []))):
            # Carry-forward path: nothing to do for this page
            continue

        html = load_content_cache(pid, pid_lm)
        if html is None:
            print(f'  ! no cached HTML for {nb}/{sec}/{p["title"]} — skipping', file=sys.stderr)
            continue

        page_meta = {'notebook': nb, 'section': sec, 'title': p['title']}
        chunks = chunk_page(pid, html, page_meta)
        pages_to_rebuild[pid] = {
            'notebook': nb, 'section': sec, 'title': p['title'],
            'last_modified': pid_lm,
            'chunk_ids': [c.chunk_id for c in chunks],
        }
        for c in chunks:
            all_chunks.append((c, pid, page_meta))

    # Chunks we need to embed (skip those already in existing_vecs on non-force)
    need_embed = [(c, pid, pm) for c, pid, pm in all_chunks
                  if force or c.chunk_id not in existing_vecs]

    carry_count = len(all_chunks) - len(need_embed)
    print(f'Pages targeted: {len(target_pids)}; '
          f'{len(pages_to_rebuild)} to rebuild; '
          f'{len(all_chunks)} total chunks; '
          f'{len(need_embed)} to embed, {carry_count} reusable',
          file=sys.stderr)

    # ---- Embed ----
    new_vecs  = {}
    new_kinds = {}
    new_chunks_meta = {}
    successes_since_ckpt = 0

    t0 = time.time()

    def _merge_and_save():
        """Snapshot current progress to disk. Safe to call multiple times."""
        merged_vecs  = dict(existing_vecs) if not force else {}
        merged_kinds = {cid: (meta.get('chunks', {}).get(cid, {}).get('kind') or '')
                        for cid in existing_vecs} if not force else {}
        merged_vecs.update(new_vecs)
        merged_kinds.update(new_kinds)

        merged_chunks_meta = {} if force else dict(meta.get('chunks', {}))
        merged_chunks_meta.update(new_chunks_meta)

        merged_pages_meta = {} if force else dict(meta.get('pages', {}))
        merged_pages_meta.update(pages_to_rebuild)

        # Filter to live chunk_ids only
        live = set()
        for p in merged_pages_meta.values():
            live.update(p.get('chunk_ids', []))
        if live:
            merged_vecs = {k: v for k, v in merged_vecs.items() if k in live}
            merged_kinds = {k: v for k, v in merged_kinds.items() if k in live}
            merged_chunks_meta = {k: v for k, v in merged_chunks_meta.items() if k in live}
        _save_state(merged_vecs, merged_kinds, merged_pages_meta, merged_chunks_meta)

    # gemini-embedding-2-preview supports only one item per call, so we walk
    # all chunks sequentially (text first, media after — just for readability
    # of the progress log).
    text_chunks  = [(c, pid, pm) for c, pid, pm in need_embed if _is_text_kind(c)]
    media_chunks = [(c, pid, pm) for c, pid, pm in need_embed if not _is_text_kind(c)]

    def _embed_and_store(c, content, label, idx, total):
        nonlocal successes_since_ckpt
        try:
            vec_list = _embed_with_retry(client, [content], 'RETRIEVAL_DOCUMENT')
        except Exception as e:
            print(f'  ! {label} failed {c.chunk_id}: {e}', file=sys.stderr)
            return
        if not vec_list:
            print(f'  ! {label} no embedding for {c.chunk_id}', file=sys.stderr)
            return
        arr = np.asarray(vec_list[0], dtype=np.float32)
        n = np.linalg.norm(arr)
        if n == 0 or not np.isfinite(arr).all():
            print(f'  ! {label} bad vector for {c.chunk_id}', file=sys.stderr)
            return
        new_vecs[c.chunk_id] = (arr / n)
        new_kinds[c.chunk_id] = c.kind
        new_chunks_meta[c.chunk_id] = c.as_meta()
        successes_since_ckpt += 1
        if idx % 20 == 0 or idx == total:
            print(f'  {label} [{idx}/{total}] {c.kind:17s} {c.chunk_id[-24:]}',
                  file=sys.stderr)

    try:
        for i, (c, pid, pm) in enumerate(text_chunks, 1):
            _embed_and_store(c, c.embed_text, 'text ', i, len(text_chunks))
            if successes_since_ckpt >= CHECKPOINT_EVERY:
                _merge_and_save()
                successes_since_ckpt = 0
                print(f'  [checkpoint] saved partial state after {len(new_vecs)} new chunks',
                      file=sys.stderr)
            if i < len(text_chunks) or media_chunks:
                time.sleep(INTER_CALL_SLEEP)

        for i, (c, pid, pm) in enumerate(media_chunks, 1):
            try:
                content = _chunk_content(c)
            except Exception as e:
                print(f'  ! media prep failed {c.chunk_id}: {e}', file=sys.stderr)
                continue
            _embed_and_store(c, content, 'media', i, len(media_chunks))
            if successes_since_ckpt >= CHECKPOINT_EVERY:
                _merge_and_save()
                successes_since_ckpt = 0
                print(f'  [checkpoint] saved partial state after {len(new_vecs)} new chunks',
                      file=sys.stderr)
            if i < len(media_chunks):
                time.sleep(INTER_CALL_SLEEP)
    except KeyboardInterrupt:
        print('\nInterrupted — saving partial progress.', file=sys.stderr)
        _merge_and_save()
        raise

    elapsed = time.time() - t0

    _merge_and_save()

    # Re-read back for accurate reporting
    final_vecs = _load_vectors()
    return {
        'pages_targeted':   len(target_pids),
        'pages_rebuilt':    len(pages_to_rebuild),
        'chunks_total':     len(final_vecs),
        'chunks_embedded':  len(new_vecs),
        'chunks_reused':    carry_count,
        'elapsed_sec':      round(elapsed, 1),
    }


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

_query_state = {'mtime': 0.0, 'ids': None, 'kinds': None, 'vectors': None, 'meta': None}


def _load_for_query():
    import numpy as np
    global _query_state
    if not EMBEDDINGS_NPZ.exists():
        raise SystemExit('No v2 embeddings found. Run: build_embeddings.py --v2')
    mtime = EMBEDDINGS_NPZ.stat().st_mtime
    if mtime > _query_state['mtime']:
        data = np.load(EMBEDDINGS_NPZ, allow_pickle=False)
        _query_state['ids']     = data['ids'].tolist()
        _query_state['kinds']   = data['kinds'].tolist() if 'kinds' in data.files else None
        # Sanitize once at load so per-query matmul never pays for it.
        _query_state['vectors'] = np.nan_to_num(
            data['vectors'], nan=0.0, posinf=0.0, neginf=0.0)
        _query_state['meta']    = _load_meta()
        _query_state['mtime']   = mtime
        _query_state['subjects'] = _load_subjects()
    return _query_state


def _load_subjects() -> dict:
    """Return {page_id: subject_label}, merging classifier output with user overrides."""
    subjects = {}
    if PAGE_SUBJECTS_JSON.exists():
        subjects.update(json.loads(PAGE_SUBJECTS_JSON.read_text()))
    if SUBJECT_OVERRIDES.exists():
        subjects.update(json.loads(SUBJECT_OVERRIDES.read_text()))
    return subjects


_FIRST_PERSON_RE = re.compile(
    r"\b(?:my|mine|me|i|i'm|i've|i'll|i'd)\b", re.IGNORECASE)


def _known_subjects(subjects: dict) -> set:
    """Unique non-trivial subject labels from the classifier output."""
    return {s for s in subjects.values() if s not in (USER_SELF_LABEL, 'general') and s}


def _detect_subjects_in_query(query: str, known: set) -> set:
    """Return a set of subject labels referenced by the query text.

    First-person pronouns → {USER_SELF_LABEL}.
    A known person name appearing as a word (or as <name>'s) → add that name.
    Returns empty set if no subject cues are found.
    """
    detected = set()
    if _FIRST_PERSON_RE.search(query):
        detected.add(USER_SELF_LABEL)
    q_lower = query.lower()
    for name in known:
        # case-insensitive whole-word / possessive match
        pat = re.compile(rf"\b{re.escape(name.lower())}(?:'s)?\b")
        if pat.search(q_lower):
            detected.add(name)
    return detected




def semantic_search(query: str, top_k_pages: int = 10,
                    max_n_per_page: int = 3, notebook: str = None,
                    subject: list = None, no_subject_filter: bool = False,
                    include_general: bool = False) -> list:
    """Top-K-pages with max-N per page. Returns list of per-page hit lists.

    Subject-aware filtering:
      - If no_subject_filter=True, no subject filtering.
      - Else if `subject` is an explicit list (e.g. ['self','Dad']), filter to it.
      - Else auto-detect subjects from the query text; if any are detected,
        restrict results to those subjects.
      - Default is STRICT (person only). Pass include_general=True to also
        admit subject='general' pages alongside the detected subjects.

    The caller decides whether to include general info — the script does not
    call any LLM for this decision.

    Each returned element:
        {'page_id', 'notebook', 'section', 'title', 'subject',
         'chunks': [...], ...}
    """
    import numpy as np

    state = _load_for_query()
    ids, kinds, vectors, meta = (state['ids'], state['kinds'],
                                  state['vectors'], state['meta'])
    subjects_map = state.get('subjects', {})
    if not ids:
        return []

    client = _get_client()
    q_vec = _embed_with_retry(client, [query], 'RETRIEVAL_QUERY')[0]
    q = np.asarray(q_vec, dtype=np.float32)
    n = np.linalg.norm(q)
    if n > 0:
        q /= n

    # vectors are already NaN-sanitized at load (see _load_for_query)
    scores = vectors @ q

    chunks_meta = meta.get('chunks', {})
    pages_meta  = meta.get('pages', {})

    if notebook:
        nb_lower = notebook.lower()
        for idx, cid in enumerate(ids):
            pid = chunks_meta.get(cid, {}).get('page_id', '')
            pm = pages_meta.get(pid, {})
            if pm.get('notebook', '').lower() != nb_lower:
                scores[idx] = -1.0

    # ---- Subject filter ----
    # Resolve the allowed subject set:
    #   - Explicit --subject list → use it (with 'self'/'me' mapped to USER_SELF_LABEL).
    #   - no_subject_filter → no filter.
    #   - Otherwise auto-detect from query.
    allowed_subjects = None
    detected = set()
    if no_subject_filter:
        allowed_subjects = None
    elif subject:
        norm = {(USER_SELF_LABEL if s.lower() in ('self','me','i','roshan') else s)
                for s in subject}
        if 'all' in {s.lower() for s in subject}:
            allowed_subjects = None
        else:
            allowed_subjects = norm
    else:
        known = _known_subjects(subjects_map)
        detected = _detect_subjects_in_query(query, known)
        if detected:
            allowed_subjects = set(detected)

    if allowed_subjects is not None:
        effective_allowed = set(allowed_subjects)
        if include_general:
            effective_allowed.add('general')
        for idx, cid in enumerate(ids):
            pid = chunks_meta.get(cid, {}).get('page_id', '')
            subj = subjects_map.get(pid, 'general')
            if subj not in effective_allowed:
                scores[idx] = -1.0

    # Walk descending scores, dedupe by page w/ max-N. Use argpartition to
    # avoid sorting all N chunks when we only need a small top slice; take a
    # generous candidate pool (top_k × max_n × fanout) to survive filtering.
    candidate_pool = min(len(scores), max(200, top_k_pages * max_n_per_page * 5))
    part = np.argpartition(-scores, candidate_pool - 1)[:candidate_pool]
    order = part[np.argsort(-scores[part])]
    per_page_count = {}
    per_page_hits = {}
    for idx in order:
        s = float(scores[idx])
        if s <= -0.5:
            break
        cid = ids[idx]
        cm  = chunks_meta.get(cid, {})
        pid = cm.get('page_id', '')
        if not pid:
            continue
        if per_page_count.get(pid, 0) >= max_n_per_page:
            continue
        snippet = _chunk_snippet(cm)
        entry = {
            'chunk_id': cid,
            'kind':     cm.get('kind', ''),
            'score':    s,
            'snippet':  snippet,
            'resource_id': cm.get('resource_id', ''),
            'filename':    cm.get('filename', ''),
            'heading_path': cm.get('heading_path', []),
        }
        per_page_hits.setdefault(pid, []).append(entry)
        per_page_count[pid] = per_page_count.get(pid, 0) + 1
        if len(per_page_hits) >= top_k_pages and all(
                c >= max_n_per_page for c in per_page_count.values()):
            break

    # Sort pages by best chunk score desc
    results = []
    for pid in sorted(per_page_hits, key=lambda p: -per_page_hits[p][0]['score']):
        pm = pages_meta.get(pid, {})
        results.append({
            'page_id':  pid,
            'notebook': pm.get('notebook', ''),
            'section':  pm.get('section', ''),
            'title':    pm.get('title', ''),
            'subject':  subjects_map.get(pid, 'general'),
            'chunks':   per_page_hits[pid],
        })
        if len(results) >= top_k_pages:
            break

    if results and detected:
        results[0]['_detected_subjects'] = sorted(detected)
    if results and allowed_subjects is not None:
        results[0]['_include_general'] = bool(include_general)
    return results


def _chunk_snippet(chunk_meta: dict) -> str:
    """Summarize a chunk for CLI display (no embed_text in meta.json, so this
    is kind-aware)."""
    kind = chunk_meta.get('kind', '')
    if kind in ('image', 'pdf', 'audio'):
        return f'[{kind} {chunk_meta.get("filename", "")}]'
    if kind == 'image_ocr':
        return f'[OCR text in image {chunk_meta.get("filename", "")}]'
    if kind == 'image_caption':
        return f'[scene caption of image {chunk_meta.get("filename", "")}]'
    if kind in ('video_transcript', 'audio_transcript'):
        return f'[{kind} {chunk_meta.get("filename", "")}]'
    if kind == 'summary':
        return '[page summary]'
    # plain text chunk
    src = chunk_meta.get('extra', {}).get('source', 'text')
    if src == 'table_row_group':
        return f'[table rows ({chunk_meta.get("extra",{}).get("rows",0)})]'
    if src == 'table':
        return '[table]'
    if src == 'table_cell_window':
        col = chunk_meta.get('extra', {}).get('column', '')
        return f'[table cell: {col}]'
    if src == 'paragraph_window':
        return '[paragraph (windowed)]'
    return '[text]'
