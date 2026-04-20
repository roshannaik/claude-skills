#!python3
"""Fetch + cache OneNote page resources (images, PDFs, audio, video).

Cache layout (cache/page_resources/):
    <safe_resource_id>.<ext>           # raw bytes (png/jpg/pdf/mp4/...)
    <safe_resource_id>.meta.json       # {mime, filename, kind, size, fetched_at, page_ids:[...]}
    <safe_resource_id>.ocr.txt         # images, when OCR returns >=30 non-ws chars
    <safe_resource_id>.transcript.txt  # audio/video transcripts

Heavy imports (msgraph, google-genai) are deferred to the functions that use
them so cache-only callers stay light.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from onenote_cache import REFS_DIR, atomic_write


PAGE_RESOURCES_DIR = REFS_DIR / 'page_resources'
PAGE_RESOURCES_DIR.mkdir(parents=True, exist_ok=True)

# MIME -> (kind, extension)
MIME_MAP = {
    'image/png':   ('image', 'png'),
    'image/jpeg':  ('image', 'jpg'),
    'image/tiff':  ('image', 'tiff'),
    'image/gif':   ('image', 'gif'),
    'image/webp':  ('image', 'webp'),
    'application/pdf': ('pdf', 'pdf'),
    'video/mp4':   ('video', 'mp4'),
    'video/quicktime': ('video', 'mov'),
    'audio/mpeg':  ('audio', 'mp3'),
    'audio/mp4':   ('audio', 'm4a'),
    'audio/wav':   ('audio', 'wav'),
    'audio/x-wav': ('audio', 'wav'),
}

OCR_MIN_CHARS = 30

# Suffixes that are derived artifacts, not raw bytes — skip when hunting
# for the cached byte file by glob.
_DERIVED_SUFFIXES = ('.meta.json', '.ocr.txt', '.caption.txt', '.transcript.txt')


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def safe_resource_id(rid: str) -> str:
    return rid.replace('!', '_').replace('/', '_')


def _byte_path(rid: str, ext: str) -> Path:
    return PAGE_RESOURCES_DIR / f'{safe_resource_id(rid)}.{ext}'


def _meta_path(rid: str) -> Path:
    return PAGE_RESOURCES_DIR / f'{safe_resource_id(rid)}.meta.json'


def _ocr_path(rid: str) -> Path:
    return PAGE_RESOURCES_DIR / f'{safe_resource_id(rid)}.ocr.txt'


def _caption_path(rid: str) -> Path:
    return PAGE_RESOURCES_DIR / f'{safe_resource_id(rid)}.caption.txt'


def _transcript_path(rid: str) -> Path:
    return PAGE_RESOURCES_DIR / f'{safe_resource_id(rid)}.transcript.txt'


def _find_byte_file(rid: str) -> Path:
    """Return the raw-bytes path for a resource if cached, else None. Matches
    any extension — we key the lookup on the safe-id prefix."""
    base = safe_resource_id(rid)
    for p in PAGE_RESOURCES_DIR.glob(f'{base}.*'):
        name = p.name
        if any(name.endswith(s) for s in _DERIVED_SUFFIXES):
            continue
        return p
    return None


# ---------------------------------------------------------------------------
# HTML parsing — extract <img> and <object> resource refs
# ---------------------------------------------------------------------------

_IMG_RE = re.compile(
    r'<img\b[^>]*?\bsrc="(?P<src>[^"]+?/onenote/resources/(?P<rid>[^/"]+)/\$value)"'
    r'(?P<attrs>[^>]*?)/?>',
    re.IGNORECASE,
)

_OBJECT_RE = re.compile(
    r'<object\b(?P<attrs>[^>]*?)'
    r'\bdata="(?P<src>[^"]+?/onenote/resources/(?P<rid>[^/"]+)/\$value)"',
    re.IGNORECASE,
)

_ATTR_RE = re.compile(r'\b(?P<k>[a-zA-Z\-]+)="(?P<v>[^"]*)"')


def _parse_attrs(blob: str) -> dict:
    return {m.group('k').lower(): m.group('v') for m in _ATTR_RE.finditer(blob or '')}


def parse_resources(html: str) -> list[dict]:
    """Return [{resource_id, url, kind, mime, filename}, ...] for all media refs.

    Deduplicates by resource_id within a single page, preserving first-seen order.
    """
    refs = []
    seen = set()

    for m in _IMG_RE.finditer(html or ''):
        rid = m.group('rid')
        if rid in seen:
            continue
        seen.add(rid)
        attrs = _parse_attrs(m.group('attrs'))
        mime = attrs.get('data-src-type') or 'image/png'
        kind, ext = MIME_MAP.get(mime, ('image', 'bin'))
        refs.append({
            'resource_id': rid,
            'url':         m.group('src'),
            'kind':        kind,
            'mime':        mime,
            'filename':    f'{safe_resource_id(rid)[:32]}.{ext}',
        })

    for m in _OBJECT_RE.finditer(html or ''):
        rid = m.group('rid')
        if rid in seen:
            continue
        seen.add(rid)
        attrs = _parse_attrs(m.group('attrs'))
        mime = attrs.get('type') or 'application/octet-stream'
        kind, _ext = MIME_MAP.get(mime, ('other', None))
        refs.append({
            'resource_id': rid,
            'url':         m.group('src'),
            'kind':        kind,
            'mime':        mime,
            'filename':    attrs.get('data-attachment') or safe_resource_id(rid)[:32],
        })

    return refs


# ---------------------------------------------------------------------------
# Meta helpers
# ---------------------------------------------------------------------------

def _load_meta(rid: str) -> dict:
    p = _meta_path(rid)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_meta(rid: str, meta: dict) -> None:
    atomic_write(_meta_path(rid), json.dumps(meta, indent=2))


def is_cached(rid: str) -> bool:
    return _find_byte_file(rid) is not None


def load_resource(rid: str) -> tuple:
    """Returns (bytes, meta_dict) or (None, None) if not cached."""
    p = _find_byte_file(rid)
    if not p:
        return None, None
    return p.read_bytes(), _load_meta(rid)


def load_ocr(rid: str) -> str:
    p = _ocr_path(rid)
    return p.read_text() if p.exists() else ''


def load_caption(rid: str) -> str:
    p = _caption_path(rid)
    return p.read_text() if p.exists() else ''


def load_transcript(rid: str) -> str:
    p = _transcript_path(rid)
    return p.read_text() if p.exists() else ''


# ---------------------------------------------------------------------------
# Graph fetch
# ---------------------------------------------------------------------------

async def fetch_resource(client, resource_id: str) -> bytes:
    """Download raw bytes for a OneNote resource."""
    return await client.me.onenote.resources.by_onenote_resource_id(resource_id).content.get()


async def download_resources_for_page(client, page_id: str, html: str) -> list[dict]:
    """Parse + fetch + cache all resources referenced by a page. Idempotent.

    Returns one dict per parsed ref with status fields:
        {resource_id, kind, mime, filename, cached, fetched, size_bytes, [error]}
    """
    refs = parse_resources(html)
    results = []

    for ref in refs:
        rid = ref['resource_id']
        byte_path = _find_byte_file(rid)

        if byte_path:
            meta = _load_meta(rid)
            page_ids = meta.setdefault('page_ids', [])
            if page_id not in page_ids:
                page_ids.append(page_id)
                _save_meta(rid, meta)
            results.append({**ref, 'cached': True, 'fetched': False,
                            'size_bytes': byte_path.stat().st_size})
            continue

        try:
            data = await fetch_resource(client, rid)
        except Exception as e:
            results.append({**ref, 'cached': False, 'fetched': False, 'error': str(e)})
            continue

        if not data:
            results.append({**ref, 'cached': False, 'fetched': False,
                            'error': 'empty response'})
            continue

        _, ext = MIME_MAP.get(ref['mime'], (ref['kind'], 'bin'))
        out_path = _byte_path(rid, ext)
        atomic_write(out_path, data, binary=True)

        _save_meta(rid, {
            'resource_id': rid,
            'kind':        ref['kind'],
            'mime':        ref['mime'],
            'filename':    ref['filename'],
            'orig_url':    ref['url'],
            'size_bytes':  len(data),
            'fetched_at':  datetime.now(timezone.utc).isoformat(),
            'page_ids':    [page_id],
        })
        results.append({**ref, 'cached': True, 'fetched': True, 'size_bytes': len(data)})

    return results


# ---------------------------------------------------------------------------
# Derived artifacts — OCR for images, transcription for audio/video
# ---------------------------------------------------------------------------

OCR_MODEL       = 'gemini-2.5-flash'
OCR_PROMPT      = (
    "Extract ALL visible text from this image verbatim. Preserve line breaks and "
    "layout as best you can. Do not summarize or describe. If there is no text, "
    "respond with the single word: NONE."
)
CAPTION_MODEL   = 'gemini-2.5-flash'
CAPTION_PROMPT  = (
    "Describe this image in 2-4 sentences focused on identifying what it depicts: "
    "the subject, key elements, setting, and any visible context. Be factual and "
    "specific. Avoid artistic commentary. If the image is completely blank or "
    "unintelligible, respond with the single word: NONE."
)
TRANSCRIBE_MODEL  = 'gemini-2.5-flash'
TRANSCRIBE_PROMPT = (
    "Produce a verbatim transcript of all speech in this audio/video. Preserve "
    "speaker turns if distinguishable. Do not summarize. If there is no speech, "
    "respond with the single word: NONE."
)
CAPTION_MIN_CHARS = 20          # minimum meaningful caption length


def _get_genai_client():
    from onenote_genai import get_client
    return get_client()


def _non_ws_len(s: str) -> int:
    return len(re.sub(r'\s+', '', s or ''))


def _gemini_generate_from_bytes(client, model: str, prompt: str,
                                 data: bytes, mime: str) -> str:
    from google.genai import types
    from onenote_genai import with_retry
    resp = with_retry(
        client.models.generate_content,
        model=model,
        contents=[types.Part.from_bytes(data=data, mime_type=mime), prompt],
        max_attempts=5, base_wait=2.0, max_wait=30.0, label='media-gen')
    return (resp.text or '').strip()


def ocr_image(rid: str, genai_client=None, force: bool = False) -> dict:
    """OCR a cached image resource. Writes <safe_id>.ocr.txt if the extracted
    text has >=OCR_MIN_CHARS non-whitespace characters. Idempotent unless force=True.

    Returns {resource_id, status, chars, path?, error?}:
        status ∈ {'skipped_not_cached','skipped_not_image','exists','written','empty','error'}
    """
    data, meta = load_resource(rid)
    if data is None:
        return {'resource_id': rid, 'status': 'skipped_not_cached'}
    if (meta.get('kind') or '') != 'image':
        return {'resource_id': rid, 'status': 'skipped_not_image'}
    p = _ocr_path(rid)
    if p.exists() and not force:
        return {'resource_id': rid, 'status': 'exists',
                'chars': _non_ws_len(p.read_text())}
    try:
        if genai_client is None:
            genai_client = _get_genai_client()
        text = _gemini_generate_from_bytes(
            genai_client, OCR_MODEL, OCR_PROMPT, data, meta.get('mime') or 'image/png')
    except Exception as e:
        return {'resource_id': rid, 'status': 'error', 'error': str(e)}

    # Always write the file (even if empty / "NONE") so re-runs don't re-call
    # the API. Embedding pipeline gates chunk emission on text length.
    if text.strip().upper() == 'NONE':
        text = ''
    chars = _non_ws_len(text)
    atomic_write(p, text)
    status = 'written' if chars >= OCR_MIN_CHARS else 'empty'
    return {'resource_id': rid, 'status': status, 'chars': chars, 'path': str(p)}


def caption_image(rid: str, genai_client=None, force: bool = False) -> dict:
    """Scene-caption a cached image. Writes <safe_id>.caption.txt.

    Intended as a fallback for images where OCR returned empty — captures
    scene semantics (diagram of X, photograph of Y, chart showing Z) so pure-
    visual content can still be found by descriptive text queries.

    Idempotent unless force=True. Returns {resource_id, status, chars, ...}.
    """
    data, meta = load_resource(rid)
    if data is None:
        return {'resource_id': rid, 'status': 'skipped_not_cached'}
    if (meta.get('kind') or '') != 'image':
        return {'resource_id': rid, 'status': 'skipped_not_image'}
    p = _caption_path(rid)
    if p.exists() and not force:
        return {'resource_id': rid, 'status': 'exists',
                'chars': _non_ws_len(p.read_text())}
    try:
        if genai_client is None:
            genai_client = _get_genai_client()
        text = _gemini_generate_from_bytes(
            genai_client, CAPTION_MODEL, CAPTION_PROMPT,
            data, meta.get('mime') or 'image/png')
    except Exception as e:
        return {'resource_id': rid, 'status': 'error', 'error': str(e)}

    if text.strip().upper() == 'NONE':
        text = ''
    chars = _non_ws_len(text)
    atomic_write(p, text)
    status = 'written' if chars >= CAPTION_MIN_CHARS else 'empty'
    return {'resource_id': rid, 'status': status, 'chars': chars, 'path': str(p)}


def transcribe_resource(rid: str, genai_client=None, force: bool = False) -> dict:
    """Transcribe a cached audio or video resource. Writes <safe_id>.transcript.txt.
    Idempotent unless force=True.

    Returns {resource_id, status, chars, path?, error?}.
    """
    data, meta = load_resource(rid)
    if data is None:
        return {'resource_id': rid, 'status': 'skipped_not_cached'}
    kind = meta.get('kind') or ''
    if kind not in ('audio', 'video'):
        return {'resource_id': rid, 'status': 'skipped_not_av'}
    p = _transcript_path(rid)
    if p.exists() and not force:
        return {'resource_id': rid, 'status': 'exists',
                'chars': _non_ws_len(p.read_text())}
    try:
        if genai_client is None:
            genai_client = _get_genai_client()
        text = _gemini_generate_from_bytes(
            genai_client, TRANSCRIBE_MODEL, TRANSCRIBE_PROMPT,
            data, meta.get('mime') or 'application/octet-stream')
    except Exception as e:
        return {'resource_id': rid, 'status': 'error', 'error': str(e)}

    if text.strip().upper() == 'NONE':
        text = ''
    chars = _non_ws_len(text)
    atomic_write(p, text)
    status = 'written' if chars >= OCR_MIN_CHARS else 'empty'
    return {'resource_id': rid, 'status': status, 'chars': chars, 'path': str(p)}


def process_derived_artifacts(resource_ids: list, force: bool = False) -> list:
    """Run OCR on each cached image and transcription on each audio/video.

    For images where OCR returns < OCR_MIN_CHARS, additionally run a scene
    caption pass — gives pure-visual images a text sibling so they surface
    for descriptive queries (e.g., "photo of tooth decay").

    Skips resources that are not cached locally. Returns a list of result
    dicts, one per call (may be 1-2 per image depending on OCR outcome).
    """
    if not resource_ids:
        return []
    genai_client = None  # lazy — only constructed if actually needed
    out = []
    for rid in resource_ids:
        _, meta = load_resource(rid)
        if meta is None:
            out.append({'resource_id': rid, 'status': 'skipped_not_cached'})
            continue
        kind = meta.get('kind') or ''
        if kind == 'image':
            if genai_client is None:
                genai_client = _get_genai_client()
            ocr_result = ocr_image(rid, genai_client=genai_client, force=force)
            out.append(ocr_result)
            # Run scene-caption fallback when OCR is empty or returned few chars
            if ocr_result.get('status') in ('empty', 'exists') \
                    and ocr_result.get('chars', 0) < OCR_MIN_CHARS:
                out.append(caption_image(rid, genai_client=genai_client, force=force))
        elif kind in ('audio', 'video'):
            if genai_client is None:
                genai_client = _get_genai_client()
            out.append(transcribe_resource(rid, genai_client=genai_client, force=force))
        else:
            out.append({'resource_id': rid, 'status': 'skipped_no_handler', 'kind': kind})
    return out


# ---------------------------------------------------------------------------
# HTML hydration — rewrite Graph resource URLs to local file:// URIs
# ---------------------------------------------------------------------------

PAGE_RENDERED_DIR = REFS_DIR / 'page_rendered'


def render_hydrated_html(page_id: str, html: str) -> tuple:
    """Rewrite <img>/<object> Graph URLs in `html` to local file:// URIs for
    resources that are cached under cache/page_resources/.

    Returns (hydrated_html, summary_dict) where summary lists which resources
    were rewritten vs missing. Missing resources are left with their original
    (broken) URL so the reader can still see there was something there.
    """
    rewritten = []
    missing = []
    for ref in parse_resources(html):
        rid = ref['resource_id']
        byte_path = _find_byte_file(rid)
        if byte_path is None:
            missing.append({'resource_id': rid, 'kind': ref['kind'],
                            'filename': ref['filename']})
            continue
        html = html.replace(ref['url'], byte_path.as_uri())
        rewritten.append({'resource_id': rid, 'kind': ref['kind'],
                          'filename': ref['filename'],
                          'local_path': str(byte_path)})
    return html, {'page_id': page_id, 'rewritten': rewritten, 'missing': missing}


def save_hydrated_html(page_id: str, html: str, out_path=None):
    """Render + write to cache/page_rendered/<safe_page_id>.html (or out_path).

    Returns (path, summary_dict).
    """
    hydrated, summary = render_hydrated_html(page_id, html)
    if out_path is None:
        PAGE_RENDERED_DIR.mkdir(parents=True, exist_ok=True)
        out_path = PAGE_RENDERED_DIR / f'{safe_resource_id(page_id)}.html'
    atomic_write(out_path, hydrated)
    summary['output_path'] = str(out_path)
    return out_path, summary


# ---------------------------------------------------------------------------
# Garbage collection
# ---------------------------------------------------------------------------

def gc_media(dry_run: bool = False) -> dict:
    """Delete raw resource bytes that are no longer referenced by any cached
    page HTML. Preserves .meta.json, .ocr.txt, .transcript.txt always.

    Returns {deleted: [paths], kept: N, orphaned_bytes: int}.
    """
    from onenote_cache import _content_path, load_content_cache, iter_all_pages

    # Build set of currently-referenced resource IDs across all cached pages.
    # If load_content_cache is stale but an HTML file exists on disk, still use
    # it — gc shouldn't miss references due to a meta mismatch.
    referenced = set()
    for _nb, _sec, _title, pid, lm in iter_all_pages():
        html = load_content_cache(pid, lm)
        if html is None:
            path = _content_path(pid)
            try:
                html = path.read_text()
            except OSError:
                continue
        for ref in parse_resources(html):
            referenced.add(ref['resource_id'])

    # Walk cache/page_resources/ and find raw-byte files whose safe-id prefix
    # doesn't map to any referenced resource_id.
    referenced_safe = {safe_resource_id(r) for r in referenced}
    deleted = []
    kept = 0
    orphaned_bytes = 0

    for p in sorted(PAGE_RESOURCES_DIR.iterdir()):
        if not p.is_file():
            continue
        if any(p.name.endswith(s) for s in _DERIVED_SUFFIXES):
            continue
        safe = p.name.rsplit('.', 1)[0]  # strip trailing ext
        if safe in referenced_safe:
            kept += 1
            continue
        size = p.stat().st_size
        orphaned_bytes += size
        if not dry_run:
            p.unlink()
        deleted.append({'path': str(p), 'size_bytes': size})

    return {'deleted': deleted, 'kept': kept,
            'orphaned_bytes': orphaned_bytes, 'dry_run': dry_run}

