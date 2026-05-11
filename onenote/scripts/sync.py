#!python3
"""OneNote skill sync.

Detects changes on the server with a single list_notebooks call, then refreshes
only dirty notebooks. Prunes orphaned HTML + embedding vectors for deleted
pages. Pre-fetches content for new/modified pages so the embeddings rebuild
picks them up.

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
  sync.py [sync] [--force-embed] [--quiet] [--silent] [--max-duration N]
                 [--max-changes N] [--force]
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
    REFS_DIR, _content_path, _load_cache, load_content_cache, atomic_write,
)
from onenote_lock import duration_limit, DurationExceeded as SyncTimeout
from onenote_media import gc_media

LOCK_FILE      = REFS_DIR / '.sync.lock'
HEARTBEAT_FILE = REFS_DIR / '.sync.heartbeat'
STATE_FILE     = REFS_DIR / '.sync.state.json'
LAST_OK_FILE   = REFS_DIR / '.sync.last_ok.json'
LOG_FILE       = REFS_DIR / 'sync.log'

HEARTBEAT_INTERVAL  = 5.0
DEFAULT_MAX_SECONDS = 600
DEFAULT_MAX_CHANGES = 20    # abort if fetch or embed would touch more pages


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

def _fmt_size(b: int) -> str:
    if b < 1024:
        return f'{b} B'
    n = float(b)
    for unit in ('KB', 'MB', 'GB', 'TB'):
        n /= 1024
        if n < 1024:
            return f'{n:.1f} {unit}'
    return f'{n:.1f} PB'


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
    """Print the notebooks/pages/fetch/embed block (shared by sync + status)."""
    emb = result.get('embeddings', {}) or {}
    pages_rebuilt   = emb.get('pages_rebuilt',   emb.get('rebuilt', 0))
    chunks_embedded = emb.get('chunks_embedded', 0)
    chunks_reused   = emb.get('chunks_reused',   emb.get('reused', 0))
    nb_failed = result.get('notebooks_failed', 0)
    nb_line = (f"{result.get('notebooks_refreshed','?')} total, "
               f"{result.get('notebooks_dirty','?')} dirty, "
               f"{result.get('notebooks_unknown','?')} new")
    if nb_failed:
        nb_line += f", {nb_failed} failed"
    print(f"{indent}notebooks  {nb_line}")
    print(f"{indent}pages      +{result.get('pages_added','?')} added, "
          f"~{result.get('pages_modified','?')} modified, -{result.get('pages_deleted','?')} deleted")
    print(f"{indent}fetch      {result.get('pages_fetched','?')} ok, "
          f"{result.get('pages_fetch_failed','?')} failed")
    print(f"{indent}embed      {pages_rebuilt} pages rebuilt, "
          f"{chunks_embedded} chunks embedded, {chunks_reused} reused")
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


def _set_step(step: str | None) -> None:
    """Transition to a new step, or finalize the current one.

    Prints `done (Xs)` for the prior step (if any) then `<step> ...` for
    the new one. Pass None or '' to close out the last step without
    starting a new one (e.g. at end of sync).
    """
    global _current_step, _step_t0, _step_started_at_iso, _step_progress
    now_perf = time.perf_counter()
    if _current_step:
        print(f'  done ({now_perf - _step_t0:.1f}s)', flush=True)
    _current_step = step or ''
    _step_t0 = now_perf
    _step_progress = {}
    if step:
        _step_started_at_iso = datetime.now(timezone.utc).isoformat(timespec='seconds')
        print(f'{step} ...', flush=True)
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
                      max_changes: int = 0, force: bool = False) -> dict:
    from onenote_setup import make_graph_client, list_notebooks
    from onenote_api import refresh_notebook, fetch_pages_by_id

    client = make_graph_client()

    _set_step('listing notebooks')
    fresh_nbs = await list_notebooks(client)

    cache = _load_cache()
    dirty, unknown = [], []
    for nb in fresh_nbs:
        cached = cache.get(nb['name'])
        if not cached or not cached.get('id'):
            unknown.append(nb['name'])
        elif cached.get('last_modified', '') != nb['last_modified']:
            dirty.append(nb['name'])

    # Always refresh all notebooks: notebook-level last_modified is frozen at
    # creation date for older notebooks (e.g. Health = 2013) and is never
    # updated when pages are edited. Page-level last_modified (compared in
    # modified_ids below) is the reliable change signal.
    to_refresh = [nb['name'] for nb in fresh_nbs]
    before = _snapshot_pages(cache)

    _set_step(f'refreshing {len(to_refresh)} notebook(s)')
    refresh_done = [0]

    async def _refresh_one(nb_name):
        try:
            result = nb_name, await refresh_notebook(client, nb_name)
        except Exception as e:
            result = nb_name, {'error': str(e)}
        refresh_done[0] += 1
        if verbose:
            info = result[1]
            if 'error' in info:
                suffix = f'  [{_format_error(info["error"])}]'
            else:
                suffix = (f' — {info.get("pages", "?")} pages, '
                          f'{info.get("sections", "?")} sections, '
                          f'{_fmt_size(_notebook_html_size(nb_name))} html')
            print(f'  [{refresh_done[0]}/{len(to_refresh)}] {nb_name}{suffix}', flush=True)
        return result

    refresh_results = await asyncio.gather(*[_refresh_one(n) for n in to_refresh])
    refresh_errors = {nb: info['error'] for nb, info in refresh_results if 'error' in info}

    cache = _load_cache()
    after = _snapshot_pages(cache)

    before_ids, after_ids = set(before), set(after)
    deleted_ids  = before_ids - after_ids
    added_ids    = after_ids  - before_ids
    modified_ids = {
        pid for pid in (before_ids & after_ids)
        if before[pid][3] != after[pid][3]
    }

    # Prune orphaned HTML + .meta for deleted pages
    if deleted_ids:
        _set_step(f'pruning {len(deleted_ids)} deleted page(s)')
        for pid in deleted_ids:
            p = _content_path(pid)
            for suffix in ('.html', '.meta'):
                f = p.with_suffix(suffix)
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass

    # Pre-fetch HTML for added + modified pages so embeddings can embed them.
    # Also include pages whose HTML is not loadable: file missing OR .meta
    # doesn't match last_modified (e.g. a prior sync timed out mid-fetch, or
    # the meta was left stale after a section rename).
    missing_html_ids = {
        pid for pid in after_ids
        if load_content_cache(pid, after[pid][3]) is None
    }
    fetched = failed = 0
    fetch_errors: dict = {}
    to_fetch_ids = added_ids | modified_ids | missing_html_ids

    if not force and max_changes > 0 and len(to_fetch_ids) > max_changes:
        raise ThresholdExceeded('fetch', len(to_fetch_ids), max_changes)

    if to_fetch_ids:
        _set_step(f'fetching {len(to_fetch_ids)} new/modified page(s)')
        total_fetch = len(to_fetch_ids)
        _set_progress(0, total_fetch)
        done_count = 0

        def _on_page_done(item, result):
            nonlocal done_count
            done_count += 1
            _set_progress(done_count, total_fetch)
            if verbose:
                suffix = f'  [error: {result["error"][:50]}]' if 'error' in result else ''
                print(f'  [{done_count}/{total_fetch}] {item["label"]}{suffix}', flush=True)

        # Fetch by page_id directly. find_page's title-based lookup is
        # ambiguous when a section has duplicate titles (legal in OneNote) —
        # one page would never get fetched.
        items = [{'page_id': pid,
                  'last_modified': after[pid][3],
                  'label': f'{after[pid][0]} / {after[pid][1]} / {after[pid][2]}'}
                 for pid in to_fetch_ids]
        results = await fetch_pages_by_id(client, items, on_progress=_on_page_done)
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
        force=force_embed, deleted_page_ids=deleted_ids,
        on_page_embedded=_on_page_embedded if verbose else None,
        max_rebuilds=0 if (force or force_embed) else max_changes,
    )
    if embed_result.get('aborted'):
        raise ThresholdExceeded('embed', embed_result['pages_to_rebuild'], max_changes)

    _set_step(None)  # finalize the last step's "done (Xs)" line

    return {
        'notebooks_refreshed': len(to_refresh),
        'notebooks_dirty':    len(dirty),
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

    try:
        with duration_limit(args.max_duration, 'sync'):
            result = asyncio.run(_sync_async(
                force_embed=args.force_embed, verbose=args.verbose,
                max_changes=args.max_changes, force=args.force,
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
        print(f"sync done in {elapsed} — no changes ({result['notebooks_refreshed']} notebooks)")
    else:
        print(f"sync done in {elapsed}")
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

    for meta_file in cache_dir.glob("*.meta.json"):
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
                    max_changes=DEFAULT_MAX_CHANGES, force=False)
    sub = ap.add_subparsers(dest='cmd')

    ps = sub.add_parser('sync', help='sync now (default if no subcommand)')
    ps.add_argument('--force-embed', action='store_true',
                    help='force full embedding rebuild')
    ps.add_argument('--quiet', action='store_true',
                    help='print summary only when changes were applied')
    ps.add_argument('--silent', '-s', action='store_false', dest='verbose',
                    help='suppress per-page progress (summary line only)')
    ps.add_argument('--max-duration', type=int, default=DEFAULT_MAX_SECONDS,
                    help=f'seconds before SIGALRM self-kill (default {DEFAULT_MAX_SECONDS}, 0 disables)')
    ps.add_argument('--max-changes', type=int, default=DEFAULT_MAX_CHANGES,
                    help=f'abort if fetch or embed would touch more pages than this '
                         f'(default {DEFAULT_MAX_CHANGES}, 0 disables). '
                         f'Guards against runaway rebuilds from Graph last_modified flutter.')
    ps.add_argument('--force', '-f', action='store_true',
                    help='bypass --max-changes threshold')

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
