#!/usr/bin/env python3
"""Backup and restore the onenote skill cache to/from OneDrive.

Uses the same MS Graph auth as the rest of the skill (MS_CLIENT_ID +
~/.cache/ms_graph_token_cache.json). The archive lands at
OneDrive root:/onenote_cache.tar.gz; each backup overwrites the previous
one (OneDrive version history handles rollback).

Restore saves the pre-restore cache to a sibling `cache.previous/` (one
snapshot only — overwritten on each restore). `undo` reverts to it.

Commands:
  python3 cache_backup.py backup [--force]    # compress + upload (skips if same version)
  python3 cache_backup.py restore [--yes] [--force]  # download + replace (prompts unless --yes)
  python3 cache_backup.py undo    [--yes]     # revert cache/ to the pre-restore snapshot

Change detection / safety:
  Both sides carry a tiny version payload — `{full_ok_ms, partial_ms}` —
  where `full_ok_ms` is the millis of the last fully-clean sync and
  `partial_ms` is the millis of the last cache-mutating sync (clean or not,
  invariant: partial >= full_ok). sync.py writes the local file
  cache/.cache_version.json on each cache-mutating run; cache_backup stamps
  the same JSON into the OneDrive DriveItem's `description` field on upload.
  At backup/restore time we compare the pair and decide:
    - all four ms equal               → skip / nothing to do
    - remote.partial_ms <= local.full → ok, proceed (local subsumes remote)
    - local.full < remote.full        → warn, suggest restore first
    - local.full == remote.full but partials differ → warn, suggest local sync
    - remote.full <= local.full < remote.partial    → warn, suggest local sync
  Restore mirrors these (warns when local is ahead of remote).
  Use --force to override warnings.
"""
import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from onenote_cache import REFS_DIR

ONEDRIVE_FILENAME = 'onenote_cache.tar.gz'
GRAPH_ROOT        = 'https://graph.microsoft.com/v1.0'
UPLOAD_CHUNK      = 10 * 1024 * 1024   # 10 MB per PUT
PREVIOUS_DIR      = REFS_DIR.parent / f'{REFS_DIR.name}.previous'
VERSION_FILE      = REFS_DIR / '.cache_version.json'


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _token() -> str:
    from onenote_setup import get_access_token
    return get_access_token()


def _auth() -> dict:
    return {'Authorization': f'Bearer {_token()}'}


def _get_remote_info() -> dict | None:
    """Return DriveItem metadata for the existing backup, or None if absent."""
    import httpx
    url = f'{GRAPH_ROOT}/me/drive/root:/{ONEDRIVE_FILENAME}'
    r = httpx.get(url, headers=_auth(), timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _set_description(item_id: str, description: str) -> None:
    """PATCH the DriveItem's description field."""
    import httpx
    url = f'{GRAPH_ROOT}/me/drive/items/{item_id}'
    r = httpx.patch(
        url,
        headers={**_auth(), 'Content-Type': 'application/json'},
        json={'description': description},
        timeout=30,
    )
    r.raise_for_status()


def _get_root_folder_url() -> str | None:
    """Return the OneDrive web URL of the folder the backup lives in (root)."""
    import httpx
    try:
        r = httpx.get(f'{GRAPH_ROOT}/me/drive/root',
                      headers=_auth(), timeout=30)
        r.raise_for_status()
        return r.json().get('webUrl')
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_age_secs(secs: float) -> str:
    def plural(n, unit):
        return f'{n} {unit}{"s" if n != 1 else ""} ago'
    if secs < 60:    return plural(int(secs), 'second')
    if secs < 3600:  return plural(int(secs / 60), 'minute')
    if secs < 86400: return plural(int(secs / 3600), 'hour')
    return plural(int(secs / 86400), 'day')


def _fmt_age(iso_ts: str) -> str:
    dt = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
    return _fmt_age_secs((datetime.now(timezone.utc) - dt).total_seconds())


def _fmt_dt(dt: datetime) -> str:
    # Normalize to aware-local so %Z resolves to the local TZ abbreviation
    # (naive datetimes — e.g. from datetime.fromtimestamp() with no tz —
    # would otherwise leave %Z empty).
    if dt.tzinfo is None:
        dt = dt.astimezone()
    # %-d / %-I are platform-extensions (Linux/macOS) for unpadded numerics
    return dt.strftime('%a, %b %-d, %Y at %-I:%M %p %Z')


def _fmt_local(iso_ts: str) -> str:
    """Human-readable local time, e.g. 'Sun, May 10, 2026 at 2:23 PM'."""
    return _fmt_dt(datetime.fromisoformat(iso_ts.replace('Z', '+00:00')).astimezone())


def _fmt_mtime(mtime: float) -> str:
    return _fmt_dt(datetime.fromtimestamp(mtime))


def _fmt_ms(ms: int) -> str:
    """Format epoch millis as human-readable local time."""
    return _fmt_dt(datetime.fromtimestamp(ms / 1000))


def _fmt_delta(new: int, old: int) -> str:
    d = new - old
    sign = '+' if d >= 0 else '-'
    pct  = (d / old * 100) if old else 0.0
    return f'Δ {sign}{abs(d)/1024/1024:.1f} MB ({sign}{abs(pct):.1f}%)'


def _dir_stats(d: Path) -> dict | None:
    """Total bytes on disk and freshness mtime for a cache-style directory."""
    if not d.exists():
        return None
    total = 0
    for p in d.rglob('*'):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    sync_json = d / 'onenote_cache.json'
    mtime = sync_json.stat().st_mtime if sync_json.exists() else d.stat().st_mtime
    return {'size': total, 'mtime': mtime}


def _print_dir_stats(label: str, d: Path) -> None:
    s = _dir_stats(d)
    if s is None:
        print(f'{label}: (not present)')
        return
    age = _fmt_age_secs(time.time() - s['mtime'])
    print(f'{label}: {s["size"]/1024/1024:.1f} MB, '
          f'last touched {_fmt_mtime(s["mtime"])} ({age})')


# ---------------------------------------------------------------------------
# Version (cache_version.json) + comparison rule engine
# ---------------------------------------------------------------------------

def _load_local_version() -> dict | None:
    """Read cache/.cache_version.json. Returns None if missing/invalid."""
    if not VERSION_FILE.exists():
        return None
    try:
        v = json.loads(VERSION_FILE.read_text())
    except Exception:
        return None
    if not (isinstance(v.get('full_ok_ms'), int) and isinstance(v.get('partial_ms'), int)):
        return None
    return v


def _parse_remote_version(item: dict | None) -> dict | None:
    """Pull version JSON out of the DriveItem description. OneDrive HTML-encodes
    specials (e.g. ':' → '&#58;'); unescape before parsing."""
    if not item:
        return None
    desc = html.unescape(item.get('description') or '').strip()
    if not desc:
        return None
    try:
        v = json.loads(desc)
    except Exception:
        return None
    if not (isinstance(v.get('full_ok_ms'), int) and isinstance(v.get('partial_ms'), int)):
        return None
    return v


def _remote_version_payload(ver: dict) -> str:
    """JSON to stamp into the DriveItem.description. Machine-readable only —
    drops the `_local` strings since they're TZ-specific to the writer."""
    return json.dumps({
        'full_ok_ms': ver['full_ok_ms'],
        'partial_ms': ver['partial_ms'],
    })


def _print_version(label: str, ver: dict | None) -> None:
    if ver is None:
        print(f'{label}: (no version info — pre-feature cache or never synced)')
        return
    p = _fmt_ms(ver['partial_ms'])
    if ver['full_ok_ms'] == 0:
        print(f'{label}: no clean baseline yet; partial updates through {p}')
    elif ver['full_ok_ms'] == ver['partial_ms']:
        print(f'{label}: clean as of {_fmt_ms(ver["full_ok_ms"])}')
    else:
        print(f'{label}: clean baseline {_fmt_ms(ver["full_ok_ms"])}; '
              f'partial updates through {p}')


# Verdicts
SKIP    = 'skip'
OK      = 'ok'
WARN    = 'warn'
UNKNOWN = 'unknown'

_SYNC_HINT = 'Run a clean local sync (python3 scripts/sync.py) before backing up, or use --force.'


def _compare_for_backup(local: dict | None, remote: dict | None) -> tuple[str, str]:
    """Apply rules (see module docstring). Returns (verdict, message).

    Evaluation order is significant: warn cases must be checked BEFORE the
    'local subsumes remote' rule, otherwise the boundary case rp==lf (with
    differing partials at same full) would silently fire OK.
    """
    if local is None and remote is None:
        return UNKNOWN, 'No version info on either side — proceeding without comparison.'
    if local is None:
        return UNKNOWN, 'Local cache has no version stamp (pre-feature?) — proceeding.'
    if remote is None:
        return UNKNOWN, 'Remote backup has no version stamp — proceeding (will stamp on upload).'

    lf, lp = local['full_ok_ms'], local['partial_ms']
    rf, rp = remote['full_ok_ms'], remote['partial_ms']

    # 1. All four equal → skip.
    if lf == rf and lp == rp:
        return SKIP, 'Local and remote are at the same version — nothing to upload.'
    # 2. Local's clean baseline is older than remote's → restore first.
    if lf < rf:
        return WARN, ('Remote has a newer clean version than local. '
                      'Restore + re-sync before pushing, or use --force.')
    # 3. Same clean baseline but partials differ (lp != rp implied by step 1).
    if lf == rf:
        return WARN, f'Same clean baseline as remote but partial timestamps differ. {_SYNC_HINT}'
    # Now lf > rf.
    # 4. Local's clean baseline sits inside remote's history → remote has
    #    partial updates past local. Sync first.
    if lf < rp:
        return WARN, f'Remote has partial updates past your last clean sync. {_SYNC_HINT}'
    # 5. lf > rf and lf >= rp → local strictly subsumes remote. Safe.
    return OK, ''


def _compare_for_restore(local: dict | None, remote: dict | None) -> tuple[str, str]:
    """Restore mirrors backup with local/remote swapped in the rules."""
    if remote is None:
        return UNKNOWN, 'Remote backup has no version stamp — proceeding without comparison.'
    if local is None:
        # Nothing to lose locally. Restore freely.
        return OK, ''

    lf, lp = local['full_ok_ms'], local['partial_ms']
    rf, rp = remote['full_ok_ms'], remote['partial_ms']

    if lf == rf and lp == rp:
        return SKIP, 'Local cache already matches the remote backup — nothing to restore.'
    # 2. Remote's clean baseline is older than local's → restoring would discard
    #    newer local clean state.
    if rf < lf:
        return WARN, ('Your local cache has a newer clean version than the remote backup. '
                      'Restoring would discard newer local state. Back up local first, or use --force.')
    # 3. Same clean baseline but partials differ.
    if rf == lf:
        return WARN, ('Same clean baseline as remote but partial timestamps differ. '
                      'Back up local changes first, or use --force.')
    # Now rf > lf.
    # 4. Remote's clean baseline sits inside local's partial history → local has
    #    partial changes past remote's baseline.
    if rf < lp:
        return WARN, ('Local has partial changes past the remote backup\'s clean baseline. '
                      'Back up local changes first, or use --force.')
    # 5. rf > lf and rf >= lp → remote strictly subsumes local. Safe.
    return OK, ''


# ---------------------------------------------------------------------------
# tar / upload / download
# ---------------------------------------------------------------------------

def _compress(tmp_path: str) -> None:
    env = os.environ.copy()
    env['COPYFILE_DISABLE'] = '1'   # skip macOS extended-attribute sidecar files
    r = subprocess.run(
        ['tar', '-czf', tmp_path, '-C', str(REFS_DIR.parent), REFS_DIR.name],
        env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(f'tar failed (exit {r.returncode})')


def _upload(tmp_path: str) -> dict:
    """Upload tmp_path to OneDrive. Returns the resulting DriveItem dict."""
    import httpx
    size = os.path.getsize(tmp_path)

    # Create a resumable upload session (required for files > 4 MB).
    # Note: personal OneDrive rejects extra item props like `description` in
    # this call — set them via a separate PATCH after upload completes.
    session_url = f'{GRAPH_ROOT}/me/drive/root:/{ONEDRIVE_FILENAME}:/createUploadSession'
    resp = httpx.post(
        session_url,
        headers=_auth(),
        json={'item': {'@microsoft.graph.conflictBehavior': 'replace'}},
        timeout=30,
    )
    resp.raise_for_status()
    upload_url = resp.json()['uploadUrl']

    uploaded = 0
    t0 = time.perf_counter()
    final: httpx.Response | None = None
    with open(tmp_path, 'rb') as f:
        while uploaded < size:
            chunk = f.read(UPLOAD_CHUNK)
            end = uploaded + len(chunk) - 1
            r = httpx.put(
                upload_url,
                content=chunk,
                headers={
                    'Content-Range':  f'bytes {uploaded}-{end}/{size}',
                    'Content-Length': str(len(chunk)),
                    'Content-Type':   'application/octet-stream',
                },
                timeout=120,
            )
            if r.status_code not in (200, 201, 202):
                raise RuntimeError(f'Upload chunk failed {r.status_code}: {r.text[:200]}')
            uploaded += len(chunk)
            final = r
            pct    = 100 * uploaded / size
            rate   = uploaded / (time.perf_counter() - t0) / 1024 / 1024
            print(f'\r  {pct:.0f}%  '
                  f'{uploaded/1024/1024:.1f}/{size/1024/1024:.1f} MB  '
                  f'{rate:.1f} MB/s   ',
                  end='', flush=True)
    print(flush=True)
    if final is not None and final.status_code in (200, 201):
        return final.json()
    return {}


def _download(tmp_path: str) -> None:
    import httpx
    url = f'{GRAPH_ROOT}/me/drive/root:/{ONEDRIVE_FILENAME}:/content'
    t0 = time.perf_counter()
    with httpx.stream('GET', url, headers=_auth(), follow_redirects=True,
                      timeout=300) as r:
        r.raise_for_status()
        total      = int(r.headers.get('content-length', 0))
        downloaded = 0
        with open(tmp_path, 'wb') as f:
            for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct  = 100 * downloaded / total
                    rate = downloaded / (time.perf_counter() - t0) / 1024 / 1024
                    print(f'\r  {pct:.0f}%  '
                          f'{downloaded/1024/1024:.1f}/{total/1024/1024:.1f} MB  '
                          f'{rate:.1f} MB/s   ',
                          end='', flush=True)
    print(flush=True)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_backup(args) -> int:
    try:
        prev = _get_remote_info()
    except Exception as e:
        print(f'  (could not query previous backup: {e})', flush=True)
        prev = None

    local_ver  = _load_local_version()
    remote_ver = _parse_remote_version(prev)

    if prev:
        print(f'Previous backup: {prev["size"]/1024/1024:.1f} MB, '
              f'{_fmt_local(prev["lastModifiedDateTime"])} '
              f'({_fmt_age(prev["lastModifiedDateTime"])})')
        _print_version('  Remote version', remote_ver)
    else:
        print('No previous backup found on OneDrive.')
    _print_version('  Local version ', local_ver)

    verdict, msg = _compare_for_backup(local_ver, remote_ver)
    if verdict == SKIP:
        if args.force:
            print(f'{msg} Uploading anyway (--force).')
        else:
            print(msg)
            return 0
    elif verdict == WARN:
        if args.force:
            print(f'Warning: {msg}\n  Continuing anyway (--force).')
        else:
            print(f'Warning: {msg}', file=sys.stderr)
            return 1
    elif verdict == UNKNOWN:
        print(msg)
    # OK falls through silently.

    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        print(f'Compressing {REFS_DIR} ...', flush=True)
        t0 = time.perf_counter()
        _compress(tmp_path)
        new_size = os.path.getsize(tmp_path)
        elapsed  = time.perf_counter() - t0
        if prev:
            print(f'  New archive: {new_size/1024/1024:.1f} MB, '
                  f'compressed in {elapsed:.1f}s  '
                  f'({_fmt_delta(new_size, prev["size"])})')
        else:
            print(f'  New archive: {new_size/1024/1024:.1f} MB, '
                  f'compressed in {elapsed:.1f}s')

        print(f'Uploading to OneDrive:{ONEDRIVE_FILENAME} ...', flush=True)
        item = _upload(tmp_path)
        # Stamp the version onto the DriveItem so the next backup can compare.
        # Best-effort: a PATCH failure doesn't undo the successful upload.
        if item.get('id') and local_ver is not None:
            try:
                _set_description(item['id'], _remote_version_payload(local_ver))
            except Exception as e:
                print(f'  (warning: could not stamp version on remote: {e})')
        print('Backup complete.')
        folder_url = _get_root_folder_url()
        if folder_url:
            print(f'  OneDrive folder: {folder_url}')
        return 0
    except Exception as e:
        print(f'Backup failed: {e}', file=sys.stderr)
        return 1
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def cmd_restore(args) -> int:
    # 1. Fetch remote metadata
    try:
        info = _get_remote_info()
    except Exception as e:
        print(f'Could not query backup metadata: {e}', file=sys.stderr)
        return 1
    if not info:
        print(f'No backup found at OneDrive:{ONEDRIVE_FILENAME}', file=sys.stderr)
        return 1

    local_ver  = _load_local_version()
    remote_ver = _parse_remote_version(info)

    # 2. Stats
    _print_dir_stats('Current cache', REFS_DIR)
    _print_version('  Local version ', local_ver)
    print(f'Remote backup: {info["size"]/1024/1024:.1f} MB compressed, '
          f'backed up {_fmt_local(info["lastModifiedDateTime"])} '
          f'({_fmt_age(info["lastModifiedDateTime"])})')
    _print_version('  Remote version', remote_ver)
    if PREVIOUS_DIR.exists():
        prev = _dir_stats(PREVIOUS_DIR)
        prev_age = _fmt_age_secs(time.time() - prev['mtime'])
        print(f'Existing undo snapshot: {prev["size"]/1024/1024:.1f} MB, '
              f'from {_fmt_mtime(prev["mtime"])} ({prev_age}) '
              f'— will be replaced')

    # 3. Apply rule engine
    verdict, msg = _compare_for_restore(local_ver, remote_ver)
    if verdict == SKIP:
        if args.force:
            print(f'{msg} Restoring anyway (--force).')
        else:
            print(msg)
            return 0
    elif verdict == WARN:
        if args.force:
            print(f'Warning: {msg}\n  Continuing anyway (--force).')
        else:
            print(f'Warning: {msg}', file=sys.stderr)
            return 1
    elif verdict == UNKNOWN:
        print(msg)
    # OK falls through silently.

    # 4. Confirm
    if not args.yes:
        print(f'Snapshot current cache as {PREVIOUS_DIR.name}/ and overwrite '
              f'with remote backup? [y/N] ', end='', flush=True)
        if input().strip().lower() != 'y':
            print('Aborted.')
            return 0

    # 5. Download to temp (before any destructive local action)
    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
        tmp_path = tmp.name
    snapshot_made = False
    try:
        print(f'Downloading from OneDrive:{ONEDRIVE_FILENAME} ...', flush=True)
        _download(tmp_path)

        # 6. Snapshot current cache via rename
        if REFS_DIR.exists():
            if PREVIOUS_DIR.exists():
                shutil.rmtree(PREVIOUS_DIR)
            REFS_DIR.rename(PREVIOUS_DIR)
            snapshot_made = True
            print(f'Snapshotted current cache → {PREVIOUS_DIR.name}/')

        # 7. Extract new archive
        print(f'Extracting to {REFS_DIR.parent} ...', flush=True)
        r = subprocess.run(['tar', '-xzf', tmp_path, '-C', str(REFS_DIR.parent)])
        if r.returncode != 0:
            raise RuntimeError(f'tar extract failed (exit {r.returncode})')

        print('Restore complete.')
        if snapshot_made:
            print(f'  To revert: python3 {Path(__file__).name} undo')
        return 0
    except Exception as e:
        print(f'Restore failed: {e}', file=sys.stderr)
        if snapshot_made:
            print(f'  Previous cache is preserved at {PREVIOUS_DIR}.',
                  file=sys.stderr)
            print(f'  Recover with: python3 {Path(__file__).name} undo',
                  file=sys.stderr)
        return 1
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def cmd_undo(args) -> int:
    if not PREVIOUS_DIR.exists():
        print(f'No undo snapshot found at {PREVIOUS_DIR}.', file=sys.stderr)
        print('Nothing to revert to.', file=sys.stderr)
        return 1

    _print_dir_stats('Current cache ', REFS_DIR)
    _print_dir_stats('Undo snapshot ', PREVIOUS_DIR)

    if not args.yes:
        print(f'Discard current cache and revert to snapshot? [y/N] ',
              end='', flush=True)
        if input().strip().lower() != 'y':
            print('Aborted.')
            return 0

    try:
        if REFS_DIR.exists():
            shutil.rmtree(REFS_DIR)
        PREVIOUS_DIR.rename(REFS_DIR)
        print(f'Reverted. (Snapshot consumed — re-run restore to fetch the '
              f'remote backup again.)')
        return 0
    except Exception as e:
        print(f'Undo failed: {e}', file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description='Backup/restore onenote cache to OneDrive',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Requires MS_CLIENT_ID env var and a cached auth token '
               '(run onenote_setup.py first).',
    )
    sub = ap.add_subparsers(dest='cmd')
    pb = sub.add_parser('backup',
                        help='Compress cache/ and upload to OneDrive '
                             '(skips if same version, warns on divergence)')
    pb.add_argument('--force', '-f', action='store_true',
                    help='Upload even on skip/warn verdicts')
    pr = sub.add_parser('restore',
                        help='Download from OneDrive, snapshot current, '
                             'then replace cache/')
    pr.add_argument('--yes', '-y', action='store_true',
                    help='Skip the overwrite confirmation prompt')
    pr.add_argument('--force', '-f', action='store_true',
                    help='Restore even on skip/warn verdicts (e.g. local is newer)')
    pu = sub.add_parser('undo',
                        help='Revert cache/ to the snapshot saved by the last '
                             'restore')
    pu.add_argument('--yes', '-y', action='store_true',
                    help='Skip the revert confirmation prompt')

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 1

    return {
        'backup':  cmd_backup,
        'restore': cmd_restore,
        'undo':    cmd_undo,
    }[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
