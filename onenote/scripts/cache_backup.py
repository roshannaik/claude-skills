#!/usr/bin/env python3
"""Backup and restore the onenote skill cache to/from OneDrive.

Uses the same MS Graph auth as the rest of the skill (MS_CLIENT_ID +
~/.cache/ms_graph_token_cache.json). The archive lands at
OneDrive root:/onenote_cache.tar.gz; each backup overwrites the previous
one (OneDrive version history handles rollback).

Commands:
  python3 cache_backup.py backup           # compress + upload
  python3 cache_backup.py restore [--yes]  # download + extract (prompts unless --yes)
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from onenote_cache import REFS_DIR

ONEDRIVE_FILENAME = 'onenote_cache.tar.gz'
GRAPH_ROOT        = 'https://graph.microsoft.com/v1.0'
UPLOAD_CHUNK      = 10 * 1024 * 1024   # 10 MB per PUT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _token() -> str:
    from onenote_setup import get_access_token
    return get_access_token()


def _auth() -> dict:
    return {'Authorization': f'Bearer {_token()}'}


def _compress(tmp_path: str) -> None:
    env = os.environ.copy()
    env['COPYFILE_DISABLE'] = '1'   # skip macOS extended-attribute sidecar files
    r = subprocess.run(
        ['tar', '-czf', tmp_path, '-C', str(REFS_DIR.parent), REFS_DIR.name],
        env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(f'tar failed (exit {r.returncode})')


def _upload(tmp_path: str) -> None:
    import httpx
    size = os.path.getsize(tmp_path)
    print(f'  {size / 1024 / 1024:.1f} MB to upload', flush=True)

    # Create a resumable upload session (required for files > 4 MB)
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
            pct    = 100 * uploaded / size
            rate   = uploaded / (time.perf_counter() - t0) / 1024 / 1024
            print(f'\r  {pct:.0f}%  '
                  f'{uploaded/1024/1024:.1f}/{size/1024/1024:.1f} MB  '
                  f'{rate:.1f} MB/s   ',
                  end='', flush=True)
    print(flush=True)


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
    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        print(f'Compressing {REFS_DIR} ...', flush=True)
        _compress(tmp_path)
        print(f'Uploading to OneDrive:{ONEDRIVE_FILENAME} ...', flush=True)
        _upload(tmp_path)
        print('Backup complete.', flush=True)
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
    if not args.yes:
        print(f'This will overwrite {REFS_DIR}. Continue? [y/N] ', end='', flush=True)
        if input().strip().lower() != 'y':
            print('Aborted.')
            return 0

    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        print(f'Downloading from OneDrive:{ONEDRIVE_FILENAME} ...', flush=True)
        _download(tmp_path)
        print(f'Extracting to {REFS_DIR.parent} ...', flush=True)
        r = subprocess.run(['tar', '-xzf', tmp_path, '-C', str(REFS_DIR.parent)])
        if r.returncode != 0:
            raise RuntimeError(f'tar extract failed (exit {r.returncode})')
        print('Restore complete.', flush=True)
        return 0
    except Exception as e:
        print(f'Restore failed: {e}', file=sys.stderr)
        return 1
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


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
    sub.add_parser('backup', help='Compress cache/ and upload to OneDrive')
    pr = sub.add_parser('restore', help='Download from OneDrive and extract to cache/')
    pr.add_argument('--yes', '-y', action='store_true',
                    help='Skip the overwrite confirmation prompt')

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 1

    return {'backup': cmd_backup, 'restore': cmd_restore}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
