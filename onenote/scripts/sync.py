#!python3
"""OneNote skill sync.

Detects changes on the server with a single list_notebooks call, then refreshes
only dirty notebooks. Prunes orphaned HTML + embedding vectors for deleted
pages. Pre-fetches content for new/modified pages so the embeddings rebuild
picks them up. After embeddings, GCs orphaned media bytes when any page was
modified or deleted (pure inserts can't drop resource references).

Concurrency: uses fcntl.flock on a lock file. The kernel releases the lock
when the process dies (crash, SIGKILL, OOM, reboot), so stale locks are
impossible — no manual cleanup ever needed. The lockfile body carries
{pid, started_at, hostname, max_duration_sec} so `status` / `unstick` can
identify the owning process even if the heartbeat was never written.

A separate heartbeat thread updates cache/.sync.heartbeat every 5s with the
current step name so `status` can report progress of a long-running sync.

SIGALRM fires after --max-duration (default 600s) as a deterministic
self-kill, so a sync can never wedge launchd indefinitely.

One JSONL row per run is appended to cache/sync.log for post-hoc auditing.

Subcommands:
  sync.py [sync] [--notebook NAME[/SECTION[/PAGE]]] [--force-embed] [--quiet]
                 [--max-duration N] [--max-changes N] [--force]
  sync.py status [-v|--verbose]               report idle / running state +
                                              cache size breakdown
  sync.py unstick                             SIGTERM/SIGKILL a hung sync
  sync.py gc [--dry-run]                      delete orphaned media files
"""
import argparse
import asyncio
import fcntl
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from onenote_cache import (
    REFS_DIR, _content_path, _load_cache, load_content_cache, atomic_write, _parse_lm,
)
from onenote_lock import duration_limit, DurationExceeded as SyncTimeout
from onenote_media import gc_media

LOCK_FILE      = REFS_DIR / '.sync.lock'
HEARTBEAT_FILE = REFS_DIR / '.sync.heartbeat'
STATE_FILE     = REFS_DIR / '.sync.state.json'
LAST_OK_FILE   = REFS_DIR / '.sync.last_ok.json'
VERSION_FILE   = REFS_DIR / '.cache_version.json'
LOG_FILE       = REFS_DIR / 'sync.log'

HEARTBEAT_INTERVAL  = 5.0
DEFAULT_MAX_SECONDS = 600
DEFAULT_MAX_CHANGES = 40    # abort if fetch or embed would touch more pages

# Column widths for the per-notebook refresh table (estimated from typical data;
# the notebook list rarely changes so dynamic sizing is not worth the complexity).
_NB_W = (3, 20, 18, 11, 6, 8, 6)  # #, name, pages/sect, date, size, changes, time
_NB_HEADERS = ('#', 'Notebook', 'Pages / Sections', 'Modified', 'Size', 'Changes', 'Time')


def _nb_hline(left='├', mid='┼', right='┤') -> str:
    return left + mid.join('─' * (w + 2) for w in _NB_W) + right


def _nb_row(num: str, name: str, pgs: str, date: str, size: str, chg: str, t: str) -> str:
    w = _NB_W
    return (f'│ {num:>{w[0]}} │ {name:<{w[1]}} │ {pgs:<{w[2]}} │ '
            f'{date:<{w[3]}} │ {size:>{w[4]}} │ {chg:<{w[5]}} │ {t:>{w[6]}} │')


class ThresholdExceeded(Exception):
    """Raised when a sync step would update more pages than --max-changes allows."""
    def __init__(self, step: str, count: int, limit: int):
        self.step  = step
        self.count = count
        self.limit = limit
        super().__init__(f'{step}: {count} pages exceeds threshold {limit}')


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_size(b: int, precision: int = 1) -> str:
    if b < 1024:
        return f'{b} B'
    n = float(b)
    for unit in ('KB', 'MB', 'GB', 'TB'):
        n /= 1024
        if n < 1024:
            return f'{n:.{precision}f} {unit}'
    return f'{n:.{precision}f} PB'


def _fmt_elapsed(sec) -> str:
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return str(sec)
    if sec < 60:
        return f'{sec}s'
    m, s = divmod(sec, 60)
    if m < 60:
        return f'{m}m {s}s'
    h, m = divmod(m, 60)
    return f'{h}h {m}m'


def _to_local(iso_ts: str) -> str:
    """UTC ISO timestamp → local-timezone formatted string. Pass-through on error."""
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.astimezone().strftime('%b %d %Y %I:%M:%S %p %Z')
                  .replace(' AM ', ' am ').replace(' PM ', ' pm '))
    except Exception:
        return iso_ts or '?'


def _normalize_error(s: str) -> str:
    """Collapse multi-line, whitespace-heavy error strings to a single line."""
    return ' '.join((s or '').split())


def _format_error(s: str) -> str:
    """Render a Graph/MSAL/generic error as `Error: <msg>, Code: <code>`.

    Parses the inner `message='...'` (preferred; carries the human-readable
    text from the MainError block) and the top-level `Code: NNN` (HTTP code).
    Falls back to whitespace-normalised raw string when neither pattern is
    present (non-ODataError exceptions).
    """
    s = _normalize_error(s)
    msg_match  = re.search(r"message='([^']+)'", s)
    code_match = re.search(r'\bCode:\s*(\d+)', s)
    parts = []
    if msg_match:
        parts.append(f"Error: {msg_match.group(1)}")
    if code_match:
        parts.append(f"Code: {code_match.group(1)}")
    return ', '.join(parts) if parts else s


def _notebook_html_size(nb_name: str) -> int:
    """Sum of cached .html sizes for all pages in this notebook."""
    cache = _load_cache()
    nb = cache.get(nb_name, {})
    total = 0
    for sec in nb.get('sections', {}).values():
        for p in sec.get('pages', []):
            if not isinstance(p, dict) or not p.get('id'):
                continue
            try:
                total += _content_path(p['id']).with_suffix('.html').stat().st_size
            except OSError:
                pass
    return total


def _state_is_clean(state: dict) -> bool:
    """A sync state is fully clean if status==ok AND no refresh/fetch errors."""
    if state.get('status') != 'ok':
        return False
    summary = state.get('summary', {}) or {}
    return not summary.get('refresh_errors') and not summary.get('fetch_errors')


def _cache_content_changed(result: dict | None) -> bool:
    """True iff the sync actually mutated cache content.

    Excludes `pages_fetched` — Graph's `lastModifiedDateTime` can flutter,
    causing a refetch of unchanged content. Only counts real content changes
    (added/modified/deleted pages) and embedding rebuilds.
    """
    if not result:
        return False
    emb = result.get('embeddings') or {}
    return (
        (result.get('pages_added',    0) or 0)
        + (result.get('pages_modified', 0) or 0)
        + (result.get('pages_deleted',  0) or 0)
        + (emb.get('pages_rebuilt',  emb.get('rebuilt', 0)) or 0)
    ) >= 1


def _update_cache_version(state: dict, *, clean: bool) -> None:
    """Advance the version timestamps after a cache-mutating sync.

    `partial_ms` advances on any content change; `full_ok_ms` only when the
    sync was fully clean. Fields not bumped are preserved from the prior file.
    `full_ok_ms` is always present (0 until the first clean sync) so the file
    is valid to readers and the invariant partial_ms >= full_ok_ms holds.
    """
    finished_iso = state.get('finished_at')
    if not finished_iso:
        return
    now_dt  = datetime.fromisoformat(finished_iso.replace('Z', '+00:00'))
    now_ms  = int(now_dt.timestamp() * 1000)
    now_loc = now_dt.astimezone().strftime('%a, %b %-d, %Y at %-I:%M %p %Z')

    cur: dict = {}
    if VERSION_FILE.exists():
        try:
            cur = json.loads(VERSION_FILE.read_text())
        except Exception:
            cur = {}

    cur.setdefault('full_ok_ms', 0)
    cur.setdefault('full_ok_local', '(never)')
    cur['partial_ms']    = now_ms
    cur['partial_local'] = now_loc
    if clean:
        cur['full_ok_ms']    = now_ms
        cur['full_ok_local'] = now_loc

    atomic_write(VERSION_FILE, json.dumps(cur, indent=2))


def _nothing_changed(result: dict) -> bool:
    if not result:
        return False
    emb = result.get('embeddings', {}) or {}
    return ((result.get('pages_added', 0) or 0)
            + (result.get('pages_modified', 0) or 0)
            + (result.get('pages_deleted', 0) or 0)
            + (result.get('pages_fetched', 0) or 0)
            + (result.get('pages_fetch_failed', 0) or 0)
            + (emb.get('pages_rebuilt', emb.get('rebuilt', 0)) or 0)
            + (emb.get('chunks_embedded', 0) or 0)) == 0


def _print_change_block(result: dict, indent: str = '  ') -> None:
    """Print the notebooks/pages/fetch/embed block (shared by sync + status).

    Zero rows are suppressed to keep the log compact. The `notebooks` line
    always prints because seeing "N total" confirms the run scope.
    """
    emb = result.get('embeddings', {}) or {}
    pages_rebuilt   = emb.get('pages_rebuilt',   emb.get('rebuilt', 0))
    chunks_embedded = emb.get('chunks_embedded', 0)
    chunks_reused   = emb.get('chunks_reused',   emb.get('reused', 0))

    nb_total  = result.get('notebooks_refreshed', 0)
    nb_dirty  = result.get('notebooks_dirty', 0)
    nb_new    = result.get('notebooks_unknown', 0)
    nb_failed = result.get('notebooks_failed', 0)
    nb_parts = [f"{nb_total} total"]
    if nb_dirty:  nb_parts.append(f"{nb_dirty} dirty")
    if nb_new:    nb_parts.append(f"{nb_new} new")
    if nb_failed: nb_parts.append(f"{nb_failed} failed")
    print(f"{indent}notebooks  {', '.join(nb_parts)}")

    p_added = result.get('pages_added', 0)
    p_mod   = result.get('pages_modified', 0)
    p_del   = result.get('pages_deleted', 0)
    p_parts = []
    if p_added: p_parts.append(f"+{p_added} added")
    if p_mod:   p_parts.append(f"~{p_mod} modified")
    if p_del:   p_parts.append(f"-{p_del} deleted")
    if p_parts:
        print(f"{indent}pages      {', '.join(p_parts)}")

    f_ok   = result.get('pages_fetched', 0)
    f_fail = result.get('pages_fetch_failed', 0)
    if f_ok or f_fail:
        f_parts = []
        if f_ok:   f_parts.append(f"{f_ok} ok")
        if f_fail: f_parts.append(f"{f_fail} failed")
        print(f"{indent}fetch      {', '.join(f_parts)}")

    if pages_rebuilt or chunks_embedded:
        emb_parts = []
        if pages_rebuilt:
            emb_parts.append(f"{pages_rebuilt} page{'' if pages_rebuilt == 1 else 's'} rebuilt")
        if chunks_embedded:
            emb_parts.append(f"{chunks_embedded} chunks embedded")
        if chunks_reused:
            emb_parts.append(f"{chunks_reused} reused")
        print(f"{indent}embed      {', '.join(emb_parts)}")

    gc = result.get('gc') or {}
    if gc.get('deleted'):
        print(f"{indent}gc         {gc['deleted']} file{'' if gc['deleted'] == 1 else 's'} deleted, "
              f"{_fmt_size(gc['reclaimed_bytes'])} reclaimed")

    sweep = result.get('sweep') or {}
    sweep_parts = []
    if sweep.get('content'):  sweep_parts.append(f"{sweep['content']} content")
    if sweep.get('rendered'): sweep_parts.append(f"{sweep['rendered']} rendered")
    if sweep.get('subjects'): sweep_parts.append(f"{sweep['subjects']} subjects")
    if sweep_parts:
        print(f"{indent}sweep      {', '.join(sweep_parts)} orphan(s)")
    refresh_errors = result.get('refresh_errors', {}) or {}
    fetch_errors   = result.get('fetch_errors', {}) or {}
    if refresh_errors or fetch_errors:
        print(f"{indent}errors:")
        for nb, err in refresh_errors.items():
            print(f"{indent}  [refresh] {nb} — {_format_error(err)}")
        if fetch_errors:
            # Group by formatted error — rate-limit cascades produce many pages
            # with identical errors; ungrouped output would be unreadable.
            from collections import defaultdict
            grouped = defaultdict(list)
            for label, err in fetch_errors.items():
                grouped[_format_error(err)].append(label)
            for err_msg, labels in grouped.items():
                if len(labels) == 1:
                    print(f"{indent}  [fetch]   {labels[0]} — {err_msg}")
                else:
                    print(f"{indent}  [fetch]   {err_msg} ({len(labels)} pages):")
                    for label in labels[:5]:
                        print(f"{indent}              {label}")
                    if len(labels) > 5:
                        print(f"{indent}              ... and {len(labels) - 5} more")

# ---------------------------------------------------------------------------
# Step timing + progress (module-level, updated by _set_step / _set_progress)
# ---------------------------------------------------------------------------

_step_t0: float          = 0.0       # perf_counter() when current step started
_step_started_at_iso: str = ''        # UTC ISO timestamp for the same moment
_step_progress: dict      = {}        # {'done': int, 'total': int} or empty


def _append_log(row: dict) -> None:
    """Append one JSONL row to sync.log. Best-effort — never raises."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(row, separators=(',', ':')) + '\n')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

_current_step: str = ''   # empty = no active step (initial or finalized)
_stop_heartbeat = threading.Event()


def _now_hms() -> str:
    """Local-time `HH:MM:SS` for log-line prefixes."""
    return datetime.now().strftime('%H:%M:%S')


def _set_step(step: str | None) -> None:
    """Transition to a new step, or finalize the current one.

    Prints `[HH:MM:SS]   done (Xs)` for the prior step (if any) then
    `[HH:MM:SS] <Step> ...` for the new one. Pass None or '' to close out
    the last step without starting a new one (e.g. at end of sync).
    """
    global _current_step, _step_t0, _step_started_at_iso, _step_progress
    now_perf = time.perf_counter()
    if _current_step:
        print(f'[{_now_hms()}]   done ({now_perf - _step_t0:.1f}s)', flush=True)
    _current_step = step or ''
    _step_t0 = now_perf
    _step_progress = {}
    if step:
        _step_started_at_iso = datetime.now(timezone.utc).isoformat(timespec='seconds')
        capitalized = step[0].upper() + step[1:]
        print(f'[{_now_hms()}] {capitalized} ...', flush=True)
        _write_heartbeat()


def _set_progress(done: int, total: int) -> None:
    global _step_progress
    _step_progress = {'done': done, 'total': total}


def _write_heartbeat() -> None:
    payload: dict = {
        'pid':             os.getpid(),
        'step':            _current_step,
        'step_started_at': _step_started_at_iso,
        'ts':              datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    if _step_progress:
        payload['progress'] = _step_progress
    atomic_write(HEARTBEAT_FILE, json.dumps(payload))


def _heartbeat_loop() -> None:
    while not _stop_heartbeat.wait(HEARTBEAT_INTERVAL):
        _write_heartbeat()


def _clear_heartbeat() -> None:
    try:
        HEARTBEAT_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Page snapshot (before / after refresh)
# ---------------------------------------------------------------------------

def _sweep_orphans(valid_pids: set) -> dict:
    """Remove per-page artifacts for pages no longer in cache. Self-healing:
    catches orphans from interrupted prior runs, page moves that re-key the
    page_id, or legacy state from before delta-based pruning existed.

    Covers: page_content/*.html + *.meta, page_rendered/*.html,
    page_subjects.json. Resource/media orphans are handled by gc_media.
    embeddings_meta.json is self-healed inside build_embeddings via its own
    intersection with the current cache.
    """
    valid_safes = {pid.replace('!', '_').replace('/', '_') for pid in valid_pids}
    counts = {'content': 0, 'rendered': 0, 'subjects': 0}

    pc = REFS_DIR / 'page_content'
    if pc.exists():
        for p in pc.iterdir():
            if not p.is_file():
                continue
            safe = p.name.rsplit('.', 1)[0]
            if safe in valid_safes:
                continue
            try:
                p.unlink()
                counts['content'] += 1
            except FileNotFoundError:
                pass

    rd = REFS_DIR / 'page_rendered'
    if rd.exists():
        for p in rd.glob('*.html'):
            safe = p.name.rsplit('.', 1)[0]
            if safe in valid_safes:
                continue
            try:
                p.unlink()
                counts['rendered'] += 1
            except FileNotFoundError:
                pass

    subj_path = REFS_DIR / 'page_subjects.json'
    if subj_path.exists():
        try:
            subjects = json.loads(subj_path.read_text())
            cleaned = {pid: lbl for pid, lbl in subjects.items() if pid in valid_pids}
            removed = len(subjects) - len(cleaned)
            if removed:
                atomic_write(subj_path, json.dumps(cleaned, indent=2))
                counts['subjects'] = removed
        except Exception:
            pass

    return counts


def _snapshot_pages(cache: dict) -> dict:
    """Return {page_id: (notebook, section, title, last_modified)} for every cached page."""
    out = {}
    for nb_name, nb in cache.items():
        if nb_name.startswith('_'):
            continue
        for sec_name, sec in nb.get('sections', {}).items():
            for p in sec.get('pages', []):
                if not isinstance(p, dict) or not p.get('id'):
                    continue
                out[p['id']] = (
                    nb_name, sec_name,
                    p.get('title', ''),
                    p.get('last_modified', ''),
                )
    return out


# ---------------------------------------------------------------------------
# Main sync flow
# ---------------------------------------------------------------------------

async def _sync_async(force_embed: bool, verbose: bool = False,
                      max_changes: int = 0, force: bool = False,
                      notebook_name: str | None = None,
                      section_name: str | None = None,
                      page_title: str | None = None,
                      concurrency: int = 5) -> dict:
    from onenote_setup import make_graph_client, list_notebooks
    from onenote_api import refresh_notebook, fetch_pages_by_id

    client = make_graph_client()

    _set_step('listing notebooks')  # capitalization handled by _set_step
    fresh_nbs = await list_notebooks(client)

    cache = _load_cache()
    # "unknown" = brand-new notebooks not yet in cache (independent of LM).
    # We deliberately do NOT compute notebook-dirty from notebook-level
    # last_modified — Graph's notebook lastModifiedDateTime is broken: it is
    # frozen at creation for older notebooks (e.g. Health = 2013) and never
    # updates when pages are edited. Always derive notebook-dirty from the
    # page-level before/after diff post-refresh (see dirty_notebooks below).
    unknown = [nb['name'] for nb in fresh_nbs
               if not cache.get(nb['name']) or not cache[nb['name']].get('id')]
    unknown_set = set(unknown)

    # Filter to single notebook if specified
    if notebook_name:
        matching = [nb for nb in fresh_nbs if nb['name'] == notebook_name]
        if not matching:
            raise ValueError(f"Notebook '{notebook_name}' not found")
        fresh_nbs = matching

    # Always refresh all notebooks (see note above re: broken notebook LM).
    to_refresh = [nb['name'] for nb in fresh_nbs]
    before = _snapshot_pages(cache)

    n_nb = len(to_refresh)
    # Single semaphore caps total concurrent Graph API calls (list_sections +
    # list_pages combined) across all notebooks. list_sections must finish
    # before list_pages can start for a given notebook, so a shared limit
    # naturally interleaves both call types without over-requesting.
    _set_step(f'Fetching section & page metadata using Graph API — {n_nb} notebook{"" if n_nb == 1 else "s"} [concurrency={concurrency}]')
    graph_sem = asyncio.Semaphore(concurrency)

    async def _refresh_one(nb_name):
        t0 = time.perf_counter()
        try:
            return nb_name, await refresh_notebook(client, nb_name,
                                                   graph_sem=graph_sem)
        except Exception as e:
            return nb_name, {'error': str(e), 'elapsed_sec': time.perf_counter() - t0}

    # Run all refreshes concurrently; print a table row per notebook as each
    # finishes (completion order) so a timeout mid-step still shows progress.
    # Per-notebook diff is computed here using the page_lm map returned by
    # refresh_notebook, avoiding a second cache load.
    refresh_results_dict: dict = {}
    done_nb = 0
    tasks = {asyncio.ensure_future(_refresh_one(n)): n for n in to_refresh}
    print(_nb_hline('┌', '┬', '┐'))
    print(_nb_row(*_NB_HEADERS))
    print(_nb_hline())
    for fut in asyncio.as_completed(tasks):
        nb_name, info = await fut
        nb_elapsed = info.get('elapsed_sec', 0.0)
        refresh_results_dict[nb_name] = info
        done_nb += 1
        t_str = f'{nb_elapsed:.1f}s'
        if 'error' in info:
            print(_nb_row(f'{done_nb}.', nb_name, 'error', info['error'][:11], '', '', t_str), flush=True)
        else:
            nb_before  = {pid: v for pid, v in before.items() if v[0] == nb_name}
            after_lm   = info.get('page_lm', {})
            added_n    = len(set(after_lm) - set(nb_before))
            deleted_n  = len(set(nb_before) - set(after_lm))
            modified_n = len({pid for pid in set(after_lm) & set(nb_before)
                              if after_lm[pid] != nb_before[pid][3]})
            ch = []
            if added_n:    ch.append(f'+{added_n}')
            if modified_n: ch.append(f'~{modified_n}')
            if deleted_n:  ch.append(f'-{deleted_n}')
            change_str = ', '.join(ch)
            emoji    = ' ✨' if nb_name in unknown_set else (' ⚡' if ch else '')
            lm_dt    = _parse_lm(info.get('last_modified', ''))
            date_str = lm_dt.strftime('%d %b %Y') if lm_dt else '?'
            size_str = _fmt_size(_notebook_html_size(nb_name), precision=0)
            pgs_str  = f'{str(info.get("pages","?")):>3} pgs / {str(info.get("sections","?")):>2} sect'
            print(_nb_row(f'{done_nb}.', nb_name + emoji, pgs_str,
                          date_str, size_str, change_str, t_str), flush=True)
    print(_nb_hline('└', '┴', '┘'))

    refresh_results = list(refresh_results_dict.items())
    refresh_errors = {nb: info['error'] for nb, info in refresh_results if 'error' in info}

    cache = _load_cache()
    after = _snapshot_pages(cache)

    # full_after_ids spans all notebooks; used by _sweep_orphans so it doesn't
    # treat other notebooks' pages as orphans when --notebook narrows the run.
    full_after_ids = set(after)

    # Narrow both snapshots to the requested scope (notebook / section / page).
    if notebook_name:
        before = {pid: v for pid, v in before.items() if v[0] == notebook_name}
        after  = {pid: v for pid, v in after.items()  if v[0] == notebook_name}
    if section_name:
        before = {pid: v for pid, v in before.items() if v[1] == section_name}
        after  = {pid: v for pid, v in after.items()  if v[1] == section_name}
    if page_title:
        before = {pid: v for pid, v in before.items() if v[2] == page_title}
        after  = {pid: v for pid, v in after.items()  if v[2] == page_title}

    before_ids, after_ids = set(before), set(after)
    deleted_ids  = before_ids - after_ids
    added_ids    = after_ids  - before_ids
    modified_ids = {
        pid for pid in (before_ids & after_ids)
        if before[pid][3] != after[pid][3]
    }

    # Pages whose HTML is missing/stale — need fetching regardless of API diff.
    missing_html_ids = {
        pid for pid in after_ids
        if load_content_cache(pid, after[pid][3]) is None
    }

    # Notebook is "dirty" if any of its pages were added, modified, or have
    # missing HTML (self-heal fills). Notebook-level Graph last_modified is
    # unreliable (see comment near unknown_set computation above).
    dirty_notebooks = {after[pid][0] for pid in (modified_ids | added_ids | missing_html_ids)}
    dirty_set = dirty_notebooks - unknown_set  # ✨ for new notebooks wins over ⚡


    # Sweep orphans across all per-page artifacts (content html/meta,
    # rendered html, subject classifications). Self-healing — handles both
    # `deleted_ids` from this run and any historic orphans (interrupted
    # prior syncs, page moves that changed page_id, etc.).
    _set_step('sweeping orphan artifacts')
    sweep_counts = _sweep_orphans(full_after_ids)

    fetched = failed = 0
    fetch_errors: dict = {}
    to_fetch_ids = added_ids | modified_ids | missing_html_ids

    # --force means "ignore timestamps at the given scope" — re-fetch all
    # pages in scope regardless of last_modified. Recovers pages whose Graph
    # lastModifiedDateTime is frozen at creation and never advances.
    if force:
        to_fetch_ids |= after_ids

    if not force and max_changes > 0 and len(to_fetch_ids) > max_changes:
        raise ThresholdExceeded('fetch', len(to_fetch_ids), max_changes)

    if to_fetch_ids:
        n_fetch = len(to_fetch_ids)
        _set_step(f'fetching {n_fetch} new/modified page{"" if n_fetch == 1 else "s"}')
        _set_progress(0, n_fetch)
        done_count = 0

        def _on_page_done(item, result):
            nonlocal done_count
            done_count += 1
            _set_progress(done_count, n_fetch)
            if verbose:
                if 'error' in result:
                    suffix = f'  [error: {result["error"][:50]}]'
                else:
                    size = _fmt_size(result.get('html_bytes', 0))
                    ms   = result.get('elapsed_ms', 0)
                    src  = 'cache' if result.get('from_cache') else f'{ms}ms'
                    suffix = f'  [{size}, {src}]'
                print(f'             {done_count}. {item["label"]}{suffix}', flush=True)

        # Fetch by page_id directly. find_page's title-based lookup is
        # ambiguous when a section has duplicate titles (legal in OneNote) —
        # one page would never get fetched.
        items = [{'page_id': pid,
                  'last_modified': after[pid][3],
                  'label': f'{after[pid][0]} / {after[pid][1]} / {after[pid][2]}'}
                 for pid in to_fetch_ids]
        results = await fetch_pages_by_id(client, items, on_progress=_on_page_done,
                                          force_refetch=force, concurrency=concurrency)
        label_by_id = {it['page_id']: it['label'] for it in items}
        for r in results:
            if 'error' in r:
                failed += 1
                fetch_errors[label_by_id.get(r['id'], r['id'])] = r['error']
            else:
                fetched += 1

    # Incremental embeddings rebuild (also drops vectors for deleted page IDs)
    _set_step('building embeddings')
    from onenote_embeddings import build_embeddings

    def _on_page_embedded(nb, sec, title, n_chunks, page_num, total_pages):
        print(f'  [page {page_num}/{total_pages}] {nb} / {sec} / {title} — {n_chunks} chunks', flush=True)

    embed_result = build_embeddings(
        page_ids=list(after_ids) if (notebook_name or section_name or page_title) else None,
        force=force_embed or force, deleted_page_ids=deleted_ids,
        on_page_embedded=_on_page_embedded if verbose else None,
        max_rebuilds=0 if (force or force_embed) else max_changes,
    )
    if embed_result.get('aborted'):
        raise ThresholdExceeded('embed', embed_result['pages_to_rebuild'], max_changes)

    # GC orphaned media only when pages were modified or deleted — pure inserts
    # can't drop resource references. Cheap (single HTML walk + dir scan), so
    # we don't gate it on a heavier heuristic.
    gc_result = None
    if modified_ids or deleted_ids:
        _set_step('garbage collecting media')
        r = gc_media(dry_run=False)
        gc_result = {'deleted': len(r['deleted']),
                     'reclaimed_bytes': r['orphaned_bytes']}

    _set_step(None)  # finalize the last step's "done (Xs)" line

    return {
        'notebooks_refreshed': len(to_refresh),
        'notebooks_dirty':    len(dirty_set),
        'notebooks_unknown':  len(unknown),
        'notebooks_failed':   len(refresh_errors),
        'pages_added':        len(added_ids),
        'pages_modified':     len(modified_ids),
        'pages_deleted':      len(deleted_ids),
        'pages_fetched':      fetched,
        'pages_fetch_failed': failed,
        'refresh_errors':     refresh_errors,
        'fetch_errors':       fetch_errors,
        'embeddings':         embed_result,
        'gc':                 gc_result,
        'sweep':              sweep_counts,
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_sync(args) -> int:
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('another sync is already running; exiting', file=sys.stderr)
        lock_fd.close()
        return 2

    started_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    # Write identification info into the lockfile body. This is the
    # authoritative pid source — heartbeat is best-effort and may be missing
    # if the process died before the heartbeat thread wrote once.
    lock_fd.write(json.dumps({
        'pid':              os.getpid(),
        'started_at':       started_at,
        'hostname':         socket.gethostname(),
        'max_duration_sec': args.max_duration,
    }) + '\n')
    lock_fd.flush()
    os.fsync(lock_fd.fileno())

    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()

    t0 = time.perf_counter()
    state: dict = {'status': 'ok', 'started_at': started_at}
    result: dict | None = None

    print(f'[{_now_hms()}] Sync start — {datetime.now().astimezone().strftime("%a %b %d %Y %Z")}', flush=True)

    try:
        with duration_limit(args.max_duration, 'sync'):
            nb_arg = getattr(args, 'notebook', None) or ''
            nb_parts = [p.strip() for p in nb_arg.split('/', 2)] if nb_arg else []
            result = asyncio.run(_sync_async(
                force_embed=args.force_embed, verbose=args.verbose,
                max_changes=args.max_changes, force=args.force,
                notebook_name=nb_parts[0] if len(nb_parts) >= 1 else None,
                section_name=nb_parts[1] if len(nb_parts) >= 2 else None,
                page_title=nb_parts[2]    if len(nb_parts) >= 3 else None,
                concurrency=args.concurrency,
            ))
        state.update({
            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'elapsed_sec': round(time.perf_counter() - t0, 1),
            'summary':     result,
        })
    except ThresholdExceeded as e:
        state = {
            'status':      'aborted',
            'started_at':  started_at,
            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'elapsed_sec': round(time.perf_counter() - t0, 1),
            'error':       f'ThresholdExceeded: {e}',
        }
    except SyncTimeout as e:
        state = {
            'status':      'timeout',
            'started_at':  started_at,
            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'elapsed_sec': round(time.perf_counter() - t0, 1),
            'error':       f'SyncTimeout: {e} (step: {_current_step})',
        }
    except BaseException as e:
        state = {
            'status':      'failed',
            'started_at':  started_at,
            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'elapsed_sec': round(time.perf_counter() - t0, 1),
            'error':       f'{type(e).__name__}: {e} (step: {_current_step})',
        }
        atomic_write(STATE_FILE, json.dumps(state, indent=2))
        _append_log(state)
        _stop_heartbeat.set()
        _clear_heartbeat()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        raise

    atomic_write(STATE_FILE, json.dumps(state, indent=2))
    if _state_is_clean(state):
        atomic_write(LAST_OK_FILE, json.dumps(state, indent=2))

    # Advance the cache-version timestamps iff this run actually changed
    # cache content. Partial advances on any change; full_ok only on clean.
    if _cache_content_changed(result):
        _update_cache_version(state, clean=_state_is_clean(state))

    # One-line JSONL row — flat keys for easy grep/jq.
    log_row = {
        'ts':          state['finished_at'],
        'status':      state['status'],
        'elapsed_sec': state['elapsed_sec'],
    }
    if result is not None:
        log_row.update({
            'nb_dirty':      result['notebooks_dirty'],
            'nb_new':        result['notebooks_unknown'],
            'pages_added':   result['pages_added'],
            'pages_mod':     result['pages_modified'],
            'pages_del':     result['pages_deleted'],
            'fetched':       result['pages_fetched'],
            'fetch_failed':  result['pages_fetch_failed'],
            'embed_rebuilt': result['embeddings'].get('pages_rebuilt', result['embeddings'].get('rebuilt', 0)),
            'embed_reused':  result['embeddings'].get('chunks_reused', result['embeddings'].get('reused', 0)),
        })
        gc = result.get('gc')
        if gc is not None:
            log_row['gc_deleted'] = gc['deleted']
            log_row['gc_bytes']   = gc['reclaimed_bytes']
        sweep = result.get('sweep') or {}
        if any(sweep.values()):
            log_row['sweep_content']  = sweep.get('content', 0)
            log_row['sweep_rendered'] = sweep.get('rendered', 0)
            log_row['sweep_subjects'] = sweep.get('subjects', 0)
    if 'error' in state:
        log_row['error'] = state['error']
    _append_log(log_row)

    _stop_heartbeat.set()
    hb_thread.join(timeout=1)
    _clear_heartbeat()

    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()

    if state['status'] == 'timeout':
        print(f"sync TIMED OUT after {state['elapsed_sec']}s: {state['error']}",
              file=sys.stderr)
        return 3

    if state['status'] == 'aborted':
        print(f"sync ABORTED after {state['elapsed_sec']}s: {state['error']}",
              file=sys.stderr)
        print(f"  re-run with --force or raise --max-changes <N> to proceed",
              file=sys.stderr)
        return 4

    nothing_changed = _nothing_changed(result)
    has_errors = bool(result.get('refresh_errors') or result.get('fetch_errors'))
    if args.quiet and nothing_changed and not has_errors:
        return 0

    elapsed = _fmt_elapsed(state['elapsed_sec'])
    if nothing_changed and not has_errors:
        print(f"[{_now_hms()}] Sync done in {elapsed} — no changes ({result['notebooks_refreshed']} notebooks)")
    else:
        # Inline summary on the trailer: e.g. "— 1 added, 2 modified".
        p_added = result.get('pages_added', 0)
        p_mod   = result.get('pages_modified', 0)
        p_del   = result.get('pages_deleted', 0)
        change_parts = []
        if p_added: change_parts.append(f"{p_added} added")
        if p_mod:   change_parts.append(f"{p_mod} modified")
        if p_del:   change_parts.append(f"{p_del} deleted")
        tail = f" — {', '.join(change_parts)}" if change_parts else ""
        print(f"[{_now_hms()}] Sync done in {elapsed}{tail}")
        _print_change_block(result)
    return 0


def _read_lockfile_body() -> dict:
    """Read {pid, started_at, hostname, max_duration_sec} from the lockfile body.
    Returns {} on any error — caller should treat missing body as 'unknown'."""
    try:
        return json.loads(LOCK_FILE.read_text())
    except Exception:
        return {}


def _read_heartbeat() -> dict:
    try:
        return json.loads(HEARTBEAT_FILE.read_text())
    except Exception:
        return {}


def cmd_status(args) -> int:
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    # Probe whether a sync is running without holding the lock.
    # `os.O_RDONLY` avoids truncating the lockfile body on an idle check.
    try:
        probe_fd = os.open(LOCK_FILE, os.O_RDONLY)
    except FileNotFoundError:
        probe_fd = None

    running = False
    if probe_fd is not None:
        try:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe_fd, fcntl.LOCK_UN)
        except BlockingIOError:
            running = True
        os.close(probe_fd)

    if running:
        body = _read_lockfile_body()
        hb   = _read_heartbeat()
        pid  = body.get('pid') or hb.get('pid', '?')
        step = hb.get('step', 'starting')
        started = body.get('started_at', '')
        now = datetime.now(timezone.utc)

        print(f"[Current Status: running] pid {pid}")
        if started:
            print(f"  started  {_to_local(started)}")
            try:
                elapsed = (now - datetime.fromisoformat(started)).total_seconds()
                print(f"  elapsed  {_fmt_elapsed(elapsed)}")
            except Exception:
                pass

        step_line = f"  step     {step}"
        try:
            step_elapsed = (now - datetime.fromisoformat(hb['step_started_at'])).total_seconds()
            step_line += f" — {_fmt_elapsed(step_elapsed)}"
            prog = hb.get('progress', {})
            if prog:
                step_line += f", {prog['done']}/{prog['total']}"
        except Exception:
            pass
        print(step_line)

        if hb:
            try:
                age = (now - datetime.fromisoformat(hb.get('ts', ''))).total_seconds()
                print(f"  beat     {int(age)}s ago")
            except Exception:
                pass
        _print_cache_sizes(verbose=args.verbose)
        return 0

    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text())
            status = st.get('status', '?')
            label = {'ok': 'OK', 'timeout': 'TIMED OUT',
                     'aborted': 'ABORTED', 'failed': 'FAILED'}.get(status, status.upper())
            finished = st.get('finished_at', '')
            elapsed  = _fmt_elapsed(st.get('elapsed_sec', '?'))
            print("[Current Status: idle]")
            print(f"[Last Sync: {label}] {_to_local(finished)} ({elapsed})")

            if status == 'ok':
                s = st.get('summary', {}) or {}
                if _nothing_changed(s) and not s.get('refresh_errors') and not s.get('fetch_errors'):
                    print(f"  no changes ({s.get('notebooks_refreshed','?')} notebooks)")
                else:
                    _print_change_block(s)
            else:
                print(f"  error   {st.get('error','?')}")

            # When the latest sync isn't fully clean, surface when the last
            # fully-clean one happened so it's clear how stale the cache is.
            if not _state_is_clean(st) and LAST_OK_FILE.exists():
                try:
                    ok = json.loads(LAST_OK_FILE.read_text())
                    ok_finished = _to_local(ok.get('finished_at', ''))
                    ok_elapsed  = _fmt_elapsed(ok.get('elapsed_sec', '?'))
                    print(f"\n[Last Clean Sync: OK] {ok_finished} ({ok_elapsed})")
                    ok_summary = ok.get('summary', {}) or {}
                    if _nothing_changed(ok_summary):
                        print(f"  no changes ({ok_summary.get('notebooks_refreshed','?')} notebooks)")
                    else:
                        _print_change_block(ok_summary)  # clean by definition → no errors
                except Exception:
                    pass
            _print_cache_sizes(verbose=args.verbose)
            return 0
        except Exception:
            pass
    print("[Current Status: idle]\nno prior sync recorded")
    _print_cache_sizes(verbose=args.verbose)
    return 0


def _print_cache_sizes(verbose: bool = False) -> None:
    """Print per-region size breakdown of the local cache (only if verbose)."""
    if not verbose:
        return

    def _scan(paths) -> tuple[int, int]:
        size = total = 0
        for p in paths:
            try:
                if p.is_file():
                    size += p.stat().st_size
                    total += 1
            except OSError:
                pass
        return size, total

    pc_files = list((REFS_DIR / 'page_content').glob('*')) \
        if (REFS_DIR / 'page_content').exists() else []
    pr_files = list((REFS_DIR / 'page_resources').glob('*')) \
        if (REFS_DIR / 'page_resources').exists() else []
    pr_files = [p for p in pr_files if p.is_file()]

    derived_suf = ('.ocr.txt', '.caption.txt', '.transcript.txt')
    derived  = [p for p in pr_files if p.name.endswith(derived_suf)]
    pr_meta  = [p for p in pr_files if p.name.endswith('.meta.json')]
    raw_set  = set(derived) | set(pr_meta)
    raw      = [p for p in pr_files if p not in raw_set]

    rendered = list((REFS_DIR / 'page_rendered').glob('*')) \
        if (REFS_DIR / 'page_rendered').exists() else []
    embeddings = [p for p in (REFS_DIR / 'embeddings.npz',
                              REFS_DIR / 'embeddings_meta.json') if p.exists()]
    index = [p for p in (REFS_DIR / 'onenote_cache.json',
                         REFS_DIR / 'page_index.txt',
                         REFS_DIR / 'page_subjects.json',
                         REFS_DIR / 'subject_overrides.json') if p.exists()]
    sync_state = list(REFS_DIR.glob('.sync.*'))
    if (REFS_DIR / 'sync.log').exists():
        sync_state.append(REFS_DIR / 'sync.log')

    sections = [
        ('html pages',      pc_files),
        ('media (raw)',     raw),
        ('media (derived)', derived),
        ('media (meta)',    pr_meta),
        ('page rendered',   rendered),
        ('embeddings',      embeddings),
        ('index',           index),
        ('sync state',      sync_state),
    ]

    print(f"\n[Cache: {REFS_DIR}]")
    total_bytes = total_files = 0
    # Calculate sizes and sort by size descending
    sizes_and_labels = []
    for label, paths in sections:
        size, n = _scan(paths)
        total_bytes += size
        total_files += n
        sizes_and_labels.append((size, n, label))

    sizes_and_labels.sort(key=lambda x: x[0], reverse=True)

    for size, n, label in sizes_and_labels:
        print(f"  {label:<17} {_fmt_size(size):>10}   ({n:>5} files)")
    print(f"  {'─' * 17} {'─' * 10}   {'─' * 13}")
    print(f"  {'total':<17} {_fmt_size(total_bytes):>10}   ({total_files:>5} files)")

    _print_media_footprint()


def _print_media_footprint() -> None:
    """Print media footprint breakdown by notebook."""
    from collections import defaultdict

    page_sizes = defaultdict(int)
    cache_dir = REFS_DIR / 'page_resources'
    if not cache_dir.exists():
        return

    # Compute the set of safe_rids whose raw bytes exist on disk. A "raw"
    # file is anything that doesn't match a derived suffix. Defensive: lets
    # the footprint reflect actual disk usage even before gc cleans up
    # orphan metas left by older gc behavior.
    derived_suf = ('.meta.json', '.ocr.txt', '.caption.txt', '.transcript.txt')
    raw_safe_rids = {p.name.rsplit('.', 1)[0]
                     for p in cache_dir.iterdir()
                     if p.is_file() and not any(p.name.endswith(s) for s in derived_suf)}

    for meta_file in cache_dir.glob("*.meta.json"):
        safe_rid = meta_file.name[:-len('.meta.json')]
        if safe_rid not in raw_safe_rids:
            continue
        try:
            meta = json.loads(meta_file.read_text())
            size_bytes = meta.get("size_bytes", 0)
            for page_id in meta.get("page_ids", []):
                page_sizes[page_id] += size_bytes
        except Exception:
            pass

    if not page_sizes:
        return

    # Load page_index to map page_ids to notebook/section/title
    page_index = {}
    index_file = REFS_DIR / 'page_index.txt'
    if index_file.exists():
        try:
            with open(index_file) as f:
                for line in f:
                    parts = line.strip().split('\t')
                    if len(parts) == 4:
                        title, notebook, section, page_id = parts
                        page_index[page_id] = {
                            "title": title,
                            "notebook": notebook,
                            "section": section
                        }
        except Exception:
            pass

    # Sort by size
    sorted_pages = sorted(page_sizes.items(), key=lambda x: x[1], reverse=True)

    if not sorted_pages:
        return

    print(f"\n[Media Footprint by Notebook: top 10 pages]")
    print(f"| {'Notebook':<18} | {'Section / Page':<50} | {'Size':<14} |")
    print(f"|{'-' * 20}|{'-' * 52}|{'-' * 16}|")

    shown = 0
    for idx, (page_id, total_size) in enumerate(sorted_pages):
        if shown >= 10:
            break
        info = page_index.get(page_id)
        if not info:
            continue
        nb = info.get("notebook", "")[:18]
        sec = info.get("section", "")
        title = info.get("title", "")
        section_page = f"{sec} / {title}"[:50]

        size_str = _fmt_size(total_size)
        emoji = " 🔥" if shown < 2 else ""
        print(f"| {nb:<18} | {section_page:<50} | {size_str:>10}{emoji:<4} |")
        shown += 1

    total_media = sum(s for _, s in sorted_pages)
    print(f"|{'-' * 20}|{'-' * 52}|{'-' * 16}|")
    print(f"| {'TOTAL':<18} | {'':<50} | {_fmt_size(total_media):>10}     |")


def cmd_unstick(args) -> int:
    body = _read_lockfile_body()
    hb   = _read_heartbeat()
    # Prefer the lockfile body (written synchronously right after flock) over
    # the heartbeat (which may not have fired yet if the sync died quickly).
    pid = body.get('pid') or hb.get('pid')

    if pid:
        step = hb.get('step', 'unknown')
        print(f'sending SIGTERM to pid {pid} (step: {step})', file=sys.stderr)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            print(f'  pid {pid} already gone', file=sys.stderr)
        else:
            for _ in range(50):  # up to 5s
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                print(f'  still alive after 5s; SIGKILL {pid}', file=sys.stderr)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    else:
        print('no pid in lockfile or heartbeat; not killing anything',
              file=sys.stderr)

    # Only remove the heartbeat — leaving the lockfile in place avoids the
    # unlink+flock race where a concurrent sync's lock becomes detached
    # from the filename.
    try:
        HEARTBEAT_FILE.unlink()
    except FileNotFoundError:
        pass
    print('cleared heartbeat', file=sys.stderr)
    return 0


def cmd_gc(args) -> int:
    """Garbage collect orphaned media files no longer referenced by any page."""
    result = gc_media(dry_run=args.dry_run)
    suffix = ' (DRY RUN)' if result['dry_run'] else ''
    print(f"\ngc-media{suffix}: {len(result['deleted'])} orphaned file(s), "
          f"{result['kept']} kept, "
          f"{result['orphaned_bytes'] / (1024 * 1024):.1f} MB reclaimable")
    for d in result['deleted'][:10]:
        action = 'would delete' if result['dry_run'] else 'deleted'
        print(f"  {action}: {Path(d['path']).name}  ({d['size_bytes'] / (1024*1024):.1f} MB)")
    if len(result['deleted']) > 10:
        print(f"  ... and {len(result['deleted']) - 10} more")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description='OneNote skill sync')
    # `cmd` defaults to 'sync' so bare `sync.py` works; subparser sets defaults
    # for sync-only flags so we don't need a per-flag fallback when no
    # subcommand is given.
    ap.set_defaults(cmd='sync', force_embed=False, quiet=False, verbose=True,
                    max_duration=DEFAULT_MAX_SECONDS,
                    max_changes=DEFAULT_MAX_CHANGES, force=False, concurrency=5)
    sub = ap.add_subparsers(dest='cmd')

    ps = sub.add_parser('sync', help='sync now (default if no subcommand)')
    ps.add_argument('--notebook', type=str, default=None,
                    help='restrict sync scope: Notebook  or  Notebook/Section  or  '
                         'Notebook/Section/Page  (use / as separator).')
    ps.add_argument('--force-embed', action='store_true',
                    help='force full embedding rebuild')
    ps.add_argument('--quiet', action='store_true',
                    help='print summary only when changes were applied')
    ps.add_argument('--max-duration', type=int, default=DEFAULT_MAX_SECONDS,
                    help=f'seconds before SIGALRM self-kill (default {DEFAULT_MAX_SECONDS}, 0 disables)')
    ps.add_argument('--max-changes', type=int, default=DEFAULT_MAX_CHANGES,
                    help=f'abort if fetch or embed would touch more pages than this '
                         f'(default {DEFAULT_MAX_CHANGES}, 0 disables). '
                         f'Guards against runaway rebuilds from Graph last_modified flutter.')
    ps.add_argument('--force', '-f', action='store_true',
                    help='ignore last_modified timestamps — re-fetch HTML and rebuild '
                         'embeddings for all pages in scope regardless of whether '
                         'Graph reports them as changed. Also bypasses --max-changes.')
    ps.add_argument('--concurrency', type=int, default=5, metavar='N',
                    help='max concurrent Graph API calls for both metadata refresh and '
                         'page content fetch (default 5). Raise to speed up large syncs; '
                         'lower if you are hitting 429 rate limits.')

    status_parser = sub.add_parser('status',  help='report idle / running state, plus cache sizes')
    status_parser.add_argument('-v', '--verbose', action='store_true',
                              help='show media footprint by notebook')
    sub.add_parser('unstick', help='kill hung sync and clean up files')

    gc_parser = sub.add_parser('gc', help='garbage collect orphaned media files')
    gc_parser.add_argument('--dry-run', action='store_true',
                          help='report what would be deleted without deleting')

    args = ap.parse_args()
    if not args.cmd:
        args.cmd = 'sync'
    return {
        'sync':    cmd_sync,
        'status':  cmd_status,
        'unstick': cmd_unstick,
        'gc':      cmd_gc,
    }[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
