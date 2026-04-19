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
  sync.py [sync] [--force-embed] [--quiet] [--max-duration N]
  sync.py status                              report idle / running state
  sync.py unstick                             SIGTERM/SIGKILL a hung sync
"""
import argparse
import asyncio
import fcntl
import json
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from onenote_cache import (
    REFS_DIR, _content_path, _load_cache, atomic_write,
)

LOCK_FILE      = REFS_DIR / '.sync.lock'
HEARTBEAT_FILE = REFS_DIR / '.sync.heartbeat'
STATE_FILE     = REFS_DIR / '.sync.state.json'
LOG_FILE       = REFS_DIR / 'sync.log'

HEARTBEAT_INTERVAL  = 5.0
DEFAULT_MAX_SECONDS = 600


class SyncTimeout(Exception):
    """Raised by the SIGALRM handler when --max-duration is exceeded."""


def _alarm_handler(signum, frame):
    raise SyncTimeout(f'exceeded max-duration')


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

_current_step: str = 'starting'
_stop_heartbeat = threading.Event()


def _set_step(step: str) -> None:
    global _current_step
    _current_step = step
    _write_heartbeat()


def _write_heartbeat() -> None:
    atomic_write(HEARTBEAT_FILE, json.dumps({
        'pid':  os.getpid(),
        'step': _current_step,
        'ts':   datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }))


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

async def _sync_async(force_embed: bool) -> dict:
    from onenote_setup import make_graph_client, list_notebooks
    from onenote_api import refresh_notebook, find_pages_batch

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

    to_refresh = dirty + unknown
    before = _snapshot_pages(cache)

    if to_refresh:
        _set_step(f'refreshing {len(to_refresh)} notebook(s): {", ".join(to_refresh)}')

        async def _refresh_one(nb_name):
            try:
                return nb_name, await refresh_notebook(client, nb_name)
            except Exception as e:
                return nb_name, {'error': str(e)}

        await asyncio.gather(*[_refresh_one(n) for n in to_refresh])

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

    # Pre-fetch HTML for added + modified pages so embeddings can embed them
    fetched = failed = 0
    to_fetch_ids = added_ids | modified_ids
    if to_fetch_ids:
        _set_step(f'fetching {len(to_fetch_ids)} new/modified page(s)')
        specs = [{'notebook': after[pid][0],
                  'section':  after[pid][1],
                  'page':     after[pid][2]}
                 for pid in to_fetch_ids]
        results = await find_pages_batch(client, specs)
        for r in results:
            if 'error' in r:
                failed += 1
            else:
                fetched += 1

    # Incremental embeddings rebuild (also drops vectors for deleted page IDs)
    _set_step('building embeddings')
    from onenote_embeddings import build_embeddings
    embed_result = build_embeddings(force=force_embed)

    return {
        'notebooks_dirty':    len(dirty),
        'notebooks_unknown':  len(unknown),
        'pages_added':        len(added_ids),
        'pages_modified':     len(modified_ids),
        'pages_deleted':      len(deleted_ids),
        'pages_fetched':      fetched,
        'pages_fetch_failed': failed,
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

    _set_step('starting')
    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()

    # Deterministic self-kill: if the sync exceeds max_duration, SIGALRM fires
    # and SyncTimeout propagates out of asyncio.run like any other exception.
    if args.max_duration > 0:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(args.max_duration)

    t0 = time.perf_counter()
    state: dict = {'status': 'ok', 'started_at': started_at}
    result: dict | None = None

    try:
        result = asyncio.run(_sync_async(force_embed=args.force_embed))
        state.update({
            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'elapsed_sec': round(time.perf_counter() - t0, 1),
            'summary':     result,
        })
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
        signal.alarm(0)
        _stop_heartbeat.set()
        _clear_heartbeat()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        raise

    signal.alarm(0)
    atomic_write(STATE_FILE, json.dumps(state, indent=2))

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
            'embed_rebuilt': result['embeddings'].get('rebuilt', 0),
            'embed_reused':  result['embeddings'].get('reused',  0),
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

    nothing_changed = (result['pages_added'] + result['pages_modified']
                       + result['pages_deleted'] == 0)
    if args.quiet and nothing_changed:
        return 0

    print(
        f"sync done in {state['elapsed_sec']}s: "
        f"nb dirty={result['notebooks_dirty']}, "
        f"new={result['notebooks_unknown']}, "
        f"pages +{result['pages_added']} ~{result['pages_modified']} -{result['pages_deleted']}, "
        f"fetched={result['pages_fetched']} failed={result['pages_fetch_failed']}, "
        f"embed rebuilt={result['embeddings']['rebuilt']} reused={result['embeddings']['reused']}"
    )
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
        started = body.get('started_at', '?')
        elapsed_s = ''
        try:
            elapsed = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(started)).total_seconds()
            elapsed_s = f", elapsed {int(elapsed)}s"
        except Exception:
            pass
        beat_s = ''
        if hb:
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(hb.get('ts', ''))).total_seconds()
                beat_s = f", last beat {int(age)}s ago"
            except Exception:
                pass
        print(f"running  pid={pid}  step={step}{elapsed_s}{beat_s}")
        return 0

    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text())
            if st.get('status') == 'ok':
                s = st.get('summary', {})
                print(f"idle  last sync: {st.get('finished_at','?')}  "
                      f"({st.get('elapsed_sec','?')}s, "
                      f"pages +{s.get('pages_added','?')} "
                      f"~{s.get('pages_modified','?')} "
                      f"-{s.get('pages_deleted','?')}, "
                      f"embed rebuilt={s.get('embeddings',{}).get('rebuilt','?')})")
            else:
                print(f"idle  last sync FAILED @ {st.get('finished_at','?')}: "
                      f"{st.get('error','?')}")
            return 0
        except Exception:
            pass
    print("idle  (no prior sync recorded)")
    return 0


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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description='OneNote skill sync')
    sub = ap.add_subparsers(dest='cmd')

    ps = sub.add_parser('sync', help='sync now (default if no subcommand)')
    ps.add_argument('--force-embed', action='store_true',
                    help='force full embedding rebuild')
    ps.add_argument('--quiet', action='store_true',
                    help='print summary only when changes were applied')
    ps.add_argument('--max-duration', type=int, default=DEFAULT_MAX_SECONDS,
                    help=f'seconds before SIGALRM self-kill (default {DEFAULT_MAX_SECONDS}, 0 disables)')

    sub.add_parser('status',  help='report idle / running state')
    sub.add_parser('unstick', help='kill hung sync and clean up files')

    args = ap.parse_args()
    if args.cmd is None:
        args.cmd = 'sync'
        args.force_embed = False
        args.quiet = False
        args.max_duration = DEFAULT_MAX_SECONDS

    return {
        'sync':    cmd_sync,
        'status':  cmd_status,
        'unstick': cmd_unstick,
    }[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
