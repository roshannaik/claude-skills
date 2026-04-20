#!python3
"""Adaptive, mechanical chunker for OneNote pages.

Produces a typed chunk list per page. Text chunks carry `embed_text` (the exact
string handed to the text embedder); media chunks carry `resource_id`/`mime`/
`filename` (bytes loaded at embed time).

Strategy (see chunked_embeddings_plan.md for rationale):
- Walk HTML elements in document order: h1..h4, p, ul/ol, table, hr, plus pseudo-
  heading detection on bold-standalone lines and date-block prefixes.
- Paragraph-pack with thresholds:
  PARA_OWN_CHUNK_MIN_SENT=3, PARA_OWN_CHUNK_MIN_CHARS=150. Short paragraphs are
  packed up to CHUNK_TARGET_CHARS (1500).
- Heading density awareness: a heading is a HARD chunk boundary only if its
  section accumulates >=HEADING_HARD_MIN_CHARS (500); otherwise the heading is
  a SOFT marker embedded inline in the active pack.
- Tables: small (<1.5K total) -> one chunk; otherwise row-group chunks with
  adaptive group size (derived from avg row body). Any cell >1K chars triggers
  intra-cell paragraph windowing with row-context re-prefix.
- Date-block detection: a paragraph starting with a date token starts a new
  "entry" that stays together under packing.
- Media chunks: one per resource (image/pdf/audio raw bytes) + image_ocr
  sibling when .ocr.txt >= OCR_MIN_CHARS. Video uses transcript-only as text.
- Page-summary chunk: one per page, whole body capped at SUMMARY_CHAR_CAP.
"""
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from onenote_cache import strip_html
from onenote_media import (
    parse_resources, load_resource, _find_byte_file,
    _ocr_path, _caption_path, _transcript_path,
    OCR_MIN_CHARS, CAPTION_MIN_CHARS,
)


# ---------------------------------------------------------------------------
# Tunables (see plan doc)
# ---------------------------------------------------------------------------

CHUNK_TARGET_CHARS         = 1500   # soft target per text chunk body
CHUNK_HARD_MAX_CHARS       = 2000   # never exceed
WINDOW_OVERLAP_CHARS       = 200    # sliding window overlap for oversized paragraphs
PARA_OWN_CHUNK_MIN_SENT    = 3      # >= this many sentences -> own chunk
PARA_OWN_CHUNK_MIN_CHARS   = 150    # ... AND >= this many non-ws chars
HEADING_HARD_MIN_CHARS     = 500    # heading becomes hard boundary if section >= this
SUMMARY_CHAR_CAP           = 5000   # page-summary body cap
TABLE_SMALL_CHAR_CAP       = 1500   # table treated as single chunk if body <= this
ROW_GROUP_BODY_TARGET      = 1000   # target body chars per row-group chunk
CELL_LARGE_CHAR_THRESHOLD  = 1000   # cell >= this triggers intra-cell windowing


# ---------------------------------------------------------------------------
# Regexes — top-level block extraction
# ---------------------------------------------------------------------------

_DIV_OPEN  = re.compile(r'<div\b[^>]*>',       re.I)
_DIV_CLOSE = re.compile(r'</div>',              re.I)
_H_TAG     = re.compile(r'<(h[1-4])\b[^>]*>(.*?)</\1>', re.I | re.S)
_P_TAG     = re.compile(r'<p\b[^>]*>(.*?)</p>',        re.I | re.S)
_TABLE_TAG = re.compile(r'<table\b[^>]*>(.*?)</table>',re.I | re.S)
_LIST_TAG  = re.compile(r'<(ul|ol)\b[^>]*>(.*?)</\1>', re.I | re.S)
_HR_TAG    = re.compile(r'<hr\b[^>]*/?>',              re.I)
_TR_TAG    = re.compile(r'<tr\b[^>]*>(.*?)</tr>',      re.I | re.S)
_TD_TAG    = re.compile(r'<t([dh])\b[^>]*>(.*?)</t\1>',re.I | re.S)
_LI_TAG    = re.compile(r'<li\b[^>]*>(.*?)</li>',      re.I | re.S)
_BOLD_ONLY = re.compile(r'^\s*<(b|strong|span[^>]*font-weight:\s*bold[^>]*)>(.+?)</\1>\s*$', re.I | re.S)

_SENT_END  = re.compile(r'[.!?]+(?=\s|$)')
_WS        = re.compile(r'\s+')
_DATE_HEAD = re.compile(
    r'^(?:'
    r'\d{4}-\d{2}-\d{2}'                                   # 2024-07-30
    r'|\d{1,2}/\d{1,2}(?:/\d{2,4})?'                       # 7/30 or 7/30/24
    r'|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'
      r'\s+\d{1,2}(?:st|nd|rd|th)?(?:[, ]+\d{2,4})?'        # Jul 30th 2024
    r'|\d{1,2}(?:st|nd|rd|th)?\s+'
      r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'  # 30 Jul 2024
    r')\b[\s:,-]*',
    re.I,
)


# ---------------------------------------------------------------------------
# Chunk dataclass
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    chunk_id:    str
    kind:        str                   # text|summary|image|image_ocr|pdf|audio|video_transcript
    page_id:     str
    embed_text:  str = ''              # for text-type chunks (passed to text embedder)
    resource_id: str = ''              # for media chunks (+image_ocr linkage)
    mime:        str = ''
    filename:    str = ''
    heading_path: list = field(default_factory=list)
    headings_covered: list = field(default_factory=list)   # populated when soft-packing
    char_count:  int = 0
    # arbitrary extra meta (e.g., source_resource_id for image_ocr)
    extra:       dict = field(default_factory=dict)

    def as_meta(self) -> dict:
        d = asdict(self)
        d.pop('embed_text', None)  # keep out of meta.json to save space
        return d


# ---------------------------------------------------------------------------
# Block extraction
# ---------------------------------------------------------------------------

def _extract_blocks(html: str) -> list:
    """Return top-level blocks in document order.

    Each block: {'kind': 'heading|para|table|list|hr', 'tag': 'h1|h2|..|p|table|ul|ol|hr',
                 'html': <inner html>, 'start': int}.

    Div wrappers (OneNote's absolute-positioning containers) are removed — their
    content becomes top-level for chunking purposes.
    """
    html = _DIV_OPEN.sub('', html)
    html = _DIV_CLOSE.sub('', html)

    hits = []
    for m in _H_TAG.finditer(html):
        hits.append((m.start(), m.end(), 'heading', m.group(1).lower(), m.group(2)))
    for m in _P_TAG.finditer(html):
        hits.append((m.start(), m.end(), 'para', 'p', m.group(1)))
    for m in _TABLE_TAG.finditer(html):
        hits.append((m.start(), m.end(), 'table', 'table', m.group(1)))
    for m in _LIST_TAG.finditer(html):
        hits.append((m.start(), m.end(), 'list', m.group(1).lower(), m.group(2)))
    for m in _HR_TAG.finditer(html):
        hits.append((m.start(), m.end(), 'hr', 'hr', ''))

    hits.sort()
    # Drop any match whose start falls within a previously-accepted range
    # (handles nested p-inside-td etc. — we only want top-level blocks).
    max_end = 0
    blocks = []
    for start, end, kind, tag, inner in hits:
        if start < max_end:
            continue
        blocks.append({'kind': kind, 'tag': tag, 'html': inner, 'start': start})
        max_end = end
    return blocks


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _clean(s: str) -> str:
    return _WS.sub(' ', strip_html(s)).strip()


def _non_ws_len(s: str) -> int:
    return len(re.sub(r'\s+', '', s or ''))


def _sentence_count(s: str) -> int:
    return len(_SENT_END.findall(s))


def _is_pseudo_heading(para_html: str) -> bool:
    """<p><b>Short bold line</b></p> with no other text -> treat as heading-like."""
    text = _clean(para_html)
    if not text or len(text) > 80:
        return False
    return bool(_BOLD_ONLY.match(para_html.strip()))


def _is_date_entry(para_html: str) -> bool:
    text = _clean(para_html)
    return bool(_DATE_HEAD.match(text))


def _headings_prefix(heading_path: list, headings_covered: list) -> str:
    lines = []
    if heading_path:
        lines.append('Heading path: ' + ' > '.join(heading_path))
    if headings_covered and len(headings_covered) > 1:
        lines.append('Headings covered: ' + '; '.join(headings_covered))
    return '\n'.join(lines)


def _build_routing_header(page_meta: dict,
                           heading_path: list,
                           headings_covered: list) -> str:
    parts = [
        f"Notebook: {page_meta.get('notebook', '')}",
        f"Section: {page_meta.get('section', '')}",
    ]
    ppath = page_meta.get('page_path') or page_meta.get('title', '')
    parts.append(f"Page: {ppath}")
    hp = _headings_prefix(heading_path, headings_covered)
    if hp:
        parts.append(hp)
    return '\n'.join(parts) + '\n\n'


# ---------------------------------------------------------------------------
# Table handling
# ---------------------------------------------------------------------------

def _parse_table(table_html: str) -> tuple:
    """Return (rows, header_row). rows/header_row are lists of cell strings.

    Heuristic: if the first row is all <th> or contains 'bold' styling markers,
    treat it as header; otherwise no header."""
    rows = []
    header = None
    for m in _TR_TAG.finditer(table_html):
        cells_raw = [(c_kind, c_html) for c_kind, c_html in _TD_TAG.findall(m.group(1))]
        cell_texts = [_clean(h) for _, h in cells_raw]
        is_header_row = all(k == 'h' for k, _ in cells_raw) and cells_raw
        if header is None and is_header_row:
            header = cell_texts
        else:
            rows.append(cell_texts)
    # If no explicit header row detected but the first row looks like labels
    # (short cells, no numbers), call it header. Keep it simple: require
    # explicit <th> for now.
    return rows, (header or [])


def _serialize_row(header: list, row: list) -> str:
    """Markdown-ish single-row serialization: 'Col1: val1 | Col2: val2 | ...'."""
    if header:
        pairs = [f"{h}: {v}" for h, v in zip(header, row) if v]
    else:
        pairs = [v for v in row if v]
    return ' | '.join(pairs)


def _header_reprefix(header: list) -> str:
    if not header:
        return ''
    return 'Columns: ' + ' | '.join(header) + '\n'


def _serialize_rows_block(header: list, rows: list) -> str:
    """Full markdown table for a group of rows."""
    if header:
        cols = '| ' + ' | '.join(header) + ' |'
        sep  = '|' + '|'.join(['---'] * len(header)) + '|'
        body = '\n'.join('| ' + ' | '.join(r + [''] * (len(header) - len(r))) + ' |'
                         for r in rows)
        return f'{cols}\n{sep}\n{body}'
    return '\n'.join(_serialize_row([], r) for r in rows)


def _chunk_table(page_id: str, table_html: str,
                 page_meta: dict, heading_path: list,
                 seq_start: int) -> tuple:
    """Chunk a single table. Returns (chunks, next_seq)."""
    rows, header = _parse_table(table_html)
    if not rows:
        return [], seq_start

    full_body = _serialize_rows_block(header, rows)
    full_len  = len(full_body)
    chunks    = []
    seq       = seq_start

    # Small-table short-circuit
    if full_len <= TABLE_SMALL_CHAR_CAP:
        routing = _build_routing_header(page_meta, heading_path, [])
        text = routing + full_body
        chunks.append(Chunk(
            chunk_id   = f'{page_id}#t{seq:04d}',
            kind       = 'text',
            page_id    = page_id,
            embed_text = text,
            heading_path = list(heading_path),
            char_count = len(text),
            extra      = {'source': 'table', 'rows': len(rows)},
        ))
        return chunks, seq + 1

    # Detect oversized cells triggering intra-cell windowing
    def _has_big_cell(r):
        return any(len(c) > CELL_LARGE_CHAR_THRESHOLD for c in r)

    # Group rows by adaptive size (rows fit until body target is hit)
    i = 0
    while i < len(rows):
        if _has_big_cell(rows[i]):
            # emit intra-cell-windowed chunks for this row alone
            chunks_row, seq = _chunk_big_row(page_id, rows[i], header,
                                             page_meta, heading_path, seq)
            chunks.extend(chunks_row)
            i += 1
            continue

        group = []
        body_len = 0
        while i < len(rows) and not _has_big_cell(rows[i]):
            row_str = _serialize_row(header, rows[i])
            # +1 for newline between rows
            if group and body_len + 1 + len(row_str) > ROW_GROUP_BODY_TARGET:
                break
            group.append(rows[i])
            body_len += (1 if group else 0) + len(row_str)
            i += 1
            if body_len >= ROW_GROUP_BODY_TARGET:
                break
        if not group:
            break

        routing = _build_routing_header(page_meta, heading_path, [])
        body = _header_reprefix(header) + _serialize_rows_block(header, group)
        text = routing + body
        chunks.append(Chunk(
            chunk_id   = f'{page_id}#t{seq:04d}',
            kind       = 'text',
            page_id    = page_id,
            embed_text = text,
            heading_path = list(heading_path),
            char_count = len(text),
            extra      = {'source': 'table_row_group', 'rows': len(group)},
        ))
        seq += 1

    return chunks, seq


def _chunk_big_row(page_id: str, row: list, header: list,
                   page_meta: dict, heading_path: list,
                   seq_start: int) -> tuple:
    """Intra-cell windowing: one row with at least one cell >1K chars.

    Each sub-chunk carries:
      - column header re-prefix
      - compact one-line summary of the row's small cells
      - the big cell's content split into ~1.5K windows (with overlap)
    """
    seq = seq_start
    chunks = []

    # Identify the big cells; small cells form the compact row context
    small_parts = []
    big_cells   = []  # (col_idx, content)
    for idx, cell in enumerate(row):
        col = header[idx] if idx < len(header) else f'col{idx+1}'
        if len(cell) > CELL_LARGE_CHAR_THRESHOLD:
            big_cells.append((idx, col, cell))
        elif cell:
            small_parts.append(f"{col}: {cell}")
    row_context = ' | '.join(small_parts)

    # Split each big cell using sliding window
    for idx, col, cell in big_cells:
        start = 0
        part_n = 0
        while start < len(cell):
            end = min(start + CHUNK_TARGET_CHARS, len(cell))
            slice_text = cell[start:end]
            part_n += 1
            routing = _build_routing_header(page_meta, heading_path, [])
            body = (_header_reprefix(header)
                    + (f"[Row context] {row_context}\n" if row_context else '')
                    + f"[Column: {col}  part {part_n}]\n"
                    + slice_text)
            text = routing + body
            chunks.append(Chunk(
                chunk_id   = f'{page_id}#t{seq:04d}',
                kind       = 'text',
                page_id    = page_id,
                embed_text = text,
                heading_path = list(heading_path),
                char_count = len(text),
                extra      = {'source': 'table_cell_window', 'column': col, 'part': part_n},
            ))
            seq += 1
            if end >= len(cell):
                break
            start = end - WINDOW_OVERLAP_CHARS

    return chunks, seq


# ---------------------------------------------------------------------------
# Text-block packing
# ---------------------------------------------------------------------------

def _split_oversized_paragraph(text: str) -> list:
    """Sliding-window split with overlap."""
    out = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_TARGET_CHARS, len(text))
        out.append(text[start:end])
        if end >= len(text):
            break
        start = end - WINDOW_OVERLAP_CHARS
    return out


class _Packer:
    """Accumulates short elements into a current pack, emits on overflow or flush."""

    def __init__(self, page_id, page_meta):
        self.page_id    = page_id
        self.page_meta  = page_meta
        self.parts      = []        # list of strings (with inline soft headings)
        self.headings_in_pack = []  # headings seen while packing (soft markers)
        self.heading_path = []      # hard-anchored path (last hard heading)
        self.chunks     = []
        self.seq        = 0

    @property
    def pack_len(self):
        return sum(len(p) for p in self.parts) + max(0, len(self.parts) - 1) * 2

    def add(self, text: str):
        if not text.strip():
            return
        # flush if this addition would overflow
        if self.pack_len and self.pack_len + len(text) + 2 > CHUNK_HARD_MAX_CHARS:
            self.flush()
        self.parts.append(text)

    def add_own_chunk(self, text: str, source: str = 'paragraph'):
        self.flush()
        routing = _build_routing_header(self.page_meta, self.heading_path, [])
        full = routing + text
        self.chunks.append(Chunk(
            chunk_id   = f'{self.page_id}#t{self.seq:04d}',
            kind       = 'text',
            page_id    = self.page_id,
            embed_text = full,
            heading_path = list(self.heading_path),
            char_count = len(full),
            extra      = {'source': source},
        ))
        self.seq += 1

    def add_soft_heading(self, heading_text: str, level: str):
        """Insert a heading inline in the current pack as a soft marker."""
        marker = f'\n{"#" * int(level[1])} {heading_text}\n'
        self.parts.append(marker)
        self.headings_in_pack.append(heading_text)

    def set_hard_heading(self, heading_text: str, level: str):
        """Close any active pack; start a new pack anchored to this heading."""
        self.flush()
        # Replace heading_path's entry at this level (simple: only track h1/h2)
        lvl = int(level[1])
        # Truncate path to lvl-1, then append
        self.heading_path = self.heading_path[:lvl-1] + [heading_text]

    def flush(self):
        if not self.parts:
            return
        body = '\n\n'.join(p for p in self.parts if p.strip())
        routing = _build_routing_header(
            self.page_meta, self.heading_path, self.headings_in_pack)
        full = routing + body
        self.chunks.append(Chunk(
            chunk_id   = f'{self.page_id}#t{self.seq:04d}',
            kind       = 'text',
            page_id    = self.page_id,
            embed_text = full,
            heading_path = list(self.heading_path),
            headings_covered = list(self.headings_in_pack),
            char_count = len(full),
            extra      = {'source': 'paragraph_pack'},
        ))
        self.seq += 1
        self.parts = []
        self.headings_in_pack = []


# ---------------------------------------------------------------------------
# Top-level chunker
# ---------------------------------------------------------------------------

def chunk_page(page_id: str, html: str, page_meta: dict) -> list:
    """Produce a list of Chunk objects for one page.

    page_meta needs: notebook, section, title. Optional: page_path, parent_page_id.
    Returns chunks in document order: text chunks, then a summary chunk, then
    media chunks (image raw, image_ocr sibling, pdf, audio, video_transcript).
    """
    page_meta = dict(page_meta)  # local copy

    blocks = _extract_blocks(html)

    # ---- Heading-density pass: decide for each heading whether it's hard or
    # ---- soft based on how much content follows before the next heading.
    heading_is_hard = {}
    for i, b in enumerate(blocks):
        if b['kind'] != 'heading':
            continue
        # Accumulate chars in following non-heading blocks until next heading
        acc = 0
        for nb in blocks[i+1:]:
            if nb['kind'] == 'heading':
                break
            if nb['kind'] == 'table':
                # count full body length (clean rows), not html
                rows, header = _parse_table(nb['html'])
                acc += sum(sum(len(c) for c in r) for r in rows)
            elif nb['kind'] in ('para', 'list'):
                acc += len(_clean(nb['html']))
        heading_is_hard[i] = (acc >= HEADING_HARD_MIN_CHARS)

    # ---- Walk blocks and drive the packer ----
    packer = _Packer(page_id, page_meta)

    for i, b in enumerate(blocks):
        if b['kind'] == 'heading':
            heading_text = _clean(b['html'])
            if not heading_text:
                continue
            if heading_is_hard.get(i, False):
                packer.set_hard_heading(heading_text, b['tag'])
            else:
                packer.add_soft_heading(heading_text, b['tag'])
            continue

        if b['kind'] == 'hr':
            packer.flush()
            continue

        if b['kind'] == 'table':
            packer.flush()
            ch, _ = _chunk_table(page_id, b['html'], page_meta, packer.heading_path,
                                 packer.seq)
            packer.chunks.extend(ch)
            packer.seq += len(ch)
            continue

        if b['kind'] == 'list':
            # Each top-level <li> as a paragraph-like item
            for m in _LI_TAG.finditer(b['html']):
                txt = _clean(m.group(1))
                if txt:
                    packer.add('- ' + txt)
            continue

        if b['kind'] == 'para':
            text = _clean(b['html'])
            if not text:
                continue

            # Pseudo-heading: bold-only short <p> -> treat as soft heading
            if _is_pseudo_heading(b['html']):
                packer.add_soft_heading(text, 'h2')
                continue

            # Long / multi-sentence paragraph -> own chunk
            if (_non_ws_len(text) >= PARA_OWN_CHUNK_MIN_CHARS
                    and _sentence_count(text) >= PARA_OWN_CHUNK_MIN_SENT):
                if len(text) > CHUNK_HARD_MAX_CHARS:
                    for slice_ in _split_oversized_paragraph(text):
                        packer.add_own_chunk(slice_, source='paragraph_window')
                else:
                    packer.add_own_chunk(text, source='paragraph')
                continue

            # Short paragraph: check for date-entry prefix (journal marker).
            # We don't treat this specially structurally, but tag it.
            packer.add(text)

    packer.flush()

    # ---- Page-summary chunk ----
    body_plain = _clean(html)
    if body_plain:
        summary_text = body_plain[:SUMMARY_CHAR_CAP]
        routing = _build_routing_header(page_meta, [], [])
        full = routing + summary_text
        packer.chunks.append(Chunk(
            chunk_id   = f'{page_id}#summary',
            kind       = 'summary',
            page_id    = page_id,
            embed_text = full,
            char_count = len(full),
            extra      = {'truncated_at': SUMMARY_CHAR_CAP},
        ))

    # ---- Media chunks ----
    for ref in parse_resources(html):
        rid = ref['resource_id']
        byte_path = _find_byte_file(rid)
        mime = ref['mime']
        kind = ref['kind']
        if byte_path is None:
            # Bytes not cached — skip, but emit OCR sibling if present
            pass

        if kind == 'video':
            # Video: transcript-only text chunk. Skip raw multimodal embedding.
            tx_p = _transcript_path(rid)
            if tx_p.exists():
                tx_text = tx_p.read_text().strip()
                if _non_ws_len(tx_text) >= OCR_MIN_CHARS:
                    routing = _build_routing_header(page_meta, [], [])
                    full = (routing
                            + f'[Video transcript: {ref["filename"]}]\n\n'
                            + tx_text)
                    packer.chunks.append(Chunk(
                        chunk_id   = f'{page_id}#media/{rid}#transcript',
                        kind       = 'video_transcript',
                        page_id    = page_id,
                        embed_text = full,
                        resource_id = rid,
                        mime       = mime,
                        filename   = ref['filename'],
                        char_count = len(full),
                        extra      = {'source_resource_id': rid},
                    ))
            continue

        # Raw media chunk (image/pdf/audio) — bytes loaded at embed time
        if byte_path is not None:
            packer.chunks.append(Chunk(
                chunk_id   = f'{page_id}#media/{rid}',
                kind       = kind,   # 'image' | 'pdf' | 'audio'
                page_id    = page_id,
                resource_id = rid,
                mime       = mime,
                filename   = ref['filename'],
                char_count = byte_path.stat().st_size,
            ))

        # OCR + caption siblings for images
        if kind == 'image':
            ocr_p = _ocr_path(rid)
            ocr_emitted = False
            if ocr_p.exists():
                ocr_text = ocr_p.read_text().strip()
                if _non_ws_len(ocr_text) >= OCR_MIN_CHARS:
                    routing = _build_routing_header(page_meta, [], [])
                    full = (routing
                            + f'[Text in image: {ref["filename"]}]\n\n'
                            + ocr_text)
                    packer.chunks.append(Chunk(
                        chunk_id   = f'{page_id}#media/{rid}#ocr',
                        kind       = 'image_ocr',
                        page_id    = page_id,
                        embed_text = full,
                        resource_id = rid,
                        mime       = mime,
                        filename   = ref['filename'],
                        char_count = len(full),
                        extra      = {'source_resource_id': rid},
                    ))
                    ocr_emitted = True

            # Scene-caption sibling: helps queries about what's IN the image when
            # the image has no legible text. Emitted whenever a caption exists and
            # reaches CAPTION_MIN_CHARS; OK to coexist with OCR (rare case).
            cap_p = _caption_path(rid)
            if cap_p.exists():
                cap_text = cap_p.read_text().strip()
                if _non_ws_len(cap_text) >= CAPTION_MIN_CHARS:
                    routing = _build_routing_header(page_meta, [], [])
                    full = (routing
                            + f'[Image caption: {ref["filename"]}]\n\n'
                            + cap_text)
                    packer.chunks.append(Chunk(
                        chunk_id   = f'{page_id}#media/{rid}#caption',
                        kind       = 'image_caption',
                        page_id    = page_id,
                        embed_text = full,
                        resource_id = rid,
                        mime       = mime,
                        filename   = ref['filename'],
                        char_count = len(full),
                        extra      = {'source_resource_id': rid,
                                      'ocr_also_emitted': ocr_emitted},
                    ))

        # Audio transcript sibling (if present)
        if kind == 'audio':
            tx_p = _transcript_path(rid)
            if tx_p.exists():
                tx_text = tx_p.read_text().strip()
                if _non_ws_len(tx_text) >= OCR_MIN_CHARS:
                    routing = _build_routing_header(page_meta, [], [])
                    full = (routing
                            + f'[Audio transcript: {ref["filename"]}]\n\n'
                            + tx_text)
                    packer.chunks.append(Chunk(
                        chunk_id   = f'{page_id}#media/{rid}#transcript',
                        kind       = 'audio_transcript',
                        page_id    = page_id,
                        embed_text = full,
                        resource_id = rid,
                        mime       = mime,
                        filename   = ref['filename'],
                        char_count = len(full),
                        extra      = {'source_resource_id': rid},
                    ))

    return packer.chunks
