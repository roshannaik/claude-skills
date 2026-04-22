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
- Tables: small (<=TABLE_SMALL_CHAR_CAP) -> one chunk; otherwise one chunk per
  row, with nested tables serialized inline inside the parent row. A single
  row that exceeds CHUNK_HARD_MAX_CHARS is sliding-window split, with the
  row's label (first cell) prefixed to every window.
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
    _ocr_path, _caption_path, _transcript_path, _non_ws_len,
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


# ---------------------------------------------------------------------------
# Regexes — top-level block extraction
# ---------------------------------------------------------------------------

_DIV_OPEN  = re.compile(r'<div\b[^>]*>',       re.I)
_DIV_CLOSE = re.compile(r'</div>',              re.I)
_H_TAG     = re.compile(r'<(h[1-4])\b[^>]*>(.*?)</\1>', re.I | re.S)
_P_TAG     = re.compile(r'<p\b[^>]*>(.*?)</p>',        re.I | re.S)
_LIST_TAG  = re.compile(r'<(ul|ol)\b[^>]*>(.*?)</\1>', re.I | re.S)
_HR_TAG    = re.compile(r'<hr\b[^>]*/?>',              re.I)
_LI_TAG    = re.compile(r'<li\b[^>]*>(.*?)</li>',      re.I | re.S)
_BOLD_ONLY = re.compile(r'^\s*<(b|strong|span[^>]*font-weight:\s*bold[^>]*)>(.+?)</\1>\s*$', re.I | re.S)

# Balanced-tag scanning for table / tr / td — non-greedy regex can't handle
# nested same-tag structures (e.g. an outer <table> row containing a nested
# <table> in a cell). The scanner below tracks open/close depth so outer
# occurrences aren't truncated at the first inner closer.
_BAL_PATS: dict = {}

def _bal_pat(tag: str) -> tuple:
    pair = _BAL_PATS.get(tag)
    if pair is None:
        pair = (re.compile(rf'<{tag}\b[^>]*?(/?)>', re.I),
                re.compile(rf'</{tag}\s*>',         re.I))
        _BAL_PATS[tag] = pair
    return pair


def _balanced_tag_spans(html: str, tag: str):
    """Yield (open_tag_start, close_tag_end, inner_start, inner_end) for each
    top-level <tag>...</tag> block, respecting nested same-tag pairs."""
    open_pat, close_pat = _bal_pat(tag)
    pos = 0
    while True:
        m = open_pat.search(html, pos)
        if not m:
            return
        if m.group(1) == '/':  # self-closing; no body
            pos = m.end()
            continue
        open_start  = m.start()
        inner_start = m.end()
        depth = 1
        p = inner_start
        while depth > 0:
            nm = open_pat.search(html, p)
            cm = close_pat.search(html, p)
            if not cm:
                return  # malformed; give up
            if nm and nm.start() < cm.start() and nm.group(1) != '/':
                depth += 1
                p = nm.end()
            else:
                depth -= 1
                p = cm.end()
                if depth == 0:
                    yield (open_start, p, inner_start, cm.start())
        pos = p

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
    for open_start, close_end, inner_start, inner_end in _balanced_tag_spans(html, 'table'):
        hits.append((open_start, close_end, 'table', 'table',
                     html[inner_start:inner_end]))
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
#
# Policy: rows are atomic chunks. Small tables (whole body <= 1.5K) are one
# chunk; otherwise one chunk per row. Nested tables inside a cell are
# serialized inline so they travel with the parent row. Single pathologically-
# fat rows (>CHUNK_HARD_MAX_CHARS) are sliding-window split, but every window
# is prefixed with the row's first-cell content so its row label (typically a
# date or subject) rides along.

def _parse_table(table_html: str) -> tuple:
    """Return (rows, header_row) using balanced <tr>/<td> scanning.

    - Header row = first row where every cell is <th>.
    - Nested <table>s inside a cell are serialized as inline text so they
      stay with the parent row.
    - Each cell's text is cleaned / whitespace-collapsed.
    """
    rows = []
    header = None
    for _, _, tr_inner_start, tr_inner_end in _balanced_tag_spans(table_html, 'tr'):
        row_html = table_html[tr_inner_start:tr_inner_end]
        cells_raw = []  # list of (doc_pos, kind: 'd'|'h', cell_text)
        for kind in ('td', 'th'):
            for open_start, _close_end, inner_start, inner_end in _balanced_tag_spans(row_html, kind):
                cell_html = row_html[inner_start:inner_end]
                cells_raw.append((open_start, kind[-1], _serialize_cell_html(cell_html)))
        cells_raw.sort(key=lambda t: t[0])
        cells_text = [text for _, _, text in cells_raw]
        kinds      = [k    for _, k, _ in cells_raw]
        is_header_row = bool(cells_raw) and all(k == 'h' for k in kinds)
        if header is None and is_header_row:
            header = cells_text
        else:
            rows.append(cells_text)
    return rows, (header or [])


def _serialize_cell_html(cell_html: str) -> str:
    """Cell content → flat text. Nested <table>s are expanded inline so the
    nested rows stay with the parent row's chunk."""
    out  = []
    last = 0
    for open_start, close_end, inner_start, inner_end in _balanced_tag_spans(cell_html, 'table'):
        out.append(cell_html[last:open_start])
        nested_rows, nested_header = _parse_table(cell_html[inner_start:inner_end])
        if nested_rows:
            out.append(f'\n{_serialize_rows_block(nested_header, nested_rows)}\n')
        last = close_end
    out.append(cell_html[last:])
    return _clean(''.join(out))


def _serialize_row(header: list, row: list) -> str:
    """Single-row serialization: 'Col1: val1 | Col2: val2 | ...'."""
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
    """Markdown-ish table for a group of rows, used when emitting multi-row
    chunks (small-table path) or nested tables inline inside a cell."""
    if header:
        cols = '| ' + ' | '.join(header) + ' |'
        sep  = '|' + '|'.join(['---'] * len(header)) + '|'
        body = '\n'.join('| ' + ' | '.join(r + [''] * (len(header) - len(r))) + ' |'
                         for r in rows)
        return f'{cols}\n{sep}\n{body}'
    return '\n'.join(_serialize_row([], r) for r in rows)


def _row_label(row: list) -> str:
    """First non-empty cell, treated as the row's anchor (date / key / name).
    Used to re-prefix window-split chunks so the anchor isn't lost."""
    for cell in row:
        txt = cell.strip()
        if txt:
            return txt[:120]
    return ''


def _chunk_table(page_id: str, table_html: str,
                 page_meta: dict, heading_path: list,
                 seq_start: int) -> tuple:
    """Chunk a single table. Returns (chunks, next_seq).

    Small tables emit one chunk for the whole table. Larger tables emit one
    chunk PER ROW — nested content inside cells is serialized inline so the
    row is self-contained (date labels in cell 0 travel with the row).
    """
    rows, header = _parse_table(table_html)
    if not rows:
        return [], seq_start

    full_body = _serialize_rows_block(header, rows)
    chunks = []
    seq = seq_start

    if len(full_body) <= TABLE_SMALL_CHAR_CAP:
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

    # Row-atomic: one chunk per row.
    routing     = _build_routing_header(page_meta, heading_path, [])
    body_header = _header_reprefix(header)
    for row in rows:
        row_body = _serialize_row(header, row)
        if not row_body.strip():
            continue

        if len(row_body) <= CHUNK_HARD_MAX_CHARS:
            text = routing + body_header + row_body
            chunks.append(Chunk(
                chunk_id   = f'{page_id}#t{seq:04d}',
                kind       = 'text',
                page_id    = page_id,
                embed_text = text,
                heading_path = list(heading_path),
                char_count = len(text),
                extra      = {'source': 'table_row'},
            ))
            seq += 1
            continue

        # Pathologically fat single row: sliding-window split, but every
        # window carries the row label (first non-empty cell) so the anchor
        # (e.g., date) rides with every piece.
        label = _row_label(row)
        for part_n, slice_text in enumerate(_split_oversized_paragraph(row_body), 1):
            label_line = f"[Row: {label}  part {part_n}]\n" if label else \
                         f"[part {part_n}]\n"
            text = routing + body_header + label_line + slice_text
            chunks.append(Chunk(
                chunk_id   = f'{page_id}#t{seq:04d}',
                kind       = 'text',
                page_id    = page_id,
                embed_text = text,
                heading_path = list(heading_path),
                char_count = len(text),
                extra      = {'source': 'table_row_window',
                              'row_label': label, 'part': part_n},
            ))
            seq += 1

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
    # Bytes absent (`byte_path is None`) just means no raw-media chunk gets
    # emitted for that resource — we still emit OCR/caption/transcript
    # siblings below if those derived text files exist on disk.
    for ref in parse_resources(html):
        rid = ref['resource_id']
        byte_path = _find_byte_file(rid)
        mime = ref['mime']
        kind = ref['kind']

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
