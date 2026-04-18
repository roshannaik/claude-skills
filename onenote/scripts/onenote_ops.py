#!/usr/bin/env python3
"""OneNote CLI entry point + UNIX-socket daemon.

The heavy lifting now lives in sibling modules:
  - onenote_cache   — JSON cache, page index, content cache, update helpers
  - onenote_api     — Graph API read ops (get_notebooks, find_page, refresh_*)
  - onenote_write   — update_page, create_page, container helpers
  - onenote_search  — title / content grep, routing index

This file keeps: daemon (removed in Phase 2), prepopulate (removed in Phase 2),
and the argparse CLI dispatch. All names below are re-exported for backward
compat with inline-Python usage (`from onenote_ops import find_page, ...`).
"""
import asyncio, sys, os, json, time, argparse, warnings, signal
warnings.filterwarnings('ignore', category=Warning, module='urllib3')
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ---------------------------------------------------------------------------
# Re-exports for backward compatibility
# ---------------------------------------------------------------------------

from onenote_cache import (  # noqa: F401
    REFS_DIR, CACHE_JSON, PAGE_INDEX, PAGE_CONTENT_DIR,
    _load_cache, _save_cache, _rebuild_page_index,
    _load_page_index,
    lookup_notebook, lookup_section, lookup_page,
    _content_path, load_content_cache, save_content_cache,
    update_sections_cache, update_pages_cache,
    strip_html,
)
from onenote_search import (  # noqa: F401
    SUMMARIES_DIR, search_pages, search_content, _build_compact_index,
)
from onenote_api import (  # noqa: F401
    get_notebooks, get_sections, get_pages, refresh_notebook,
    find_page, find_pages_batch, refresh_all_notebooks,
)
from onenote_write import (  # noqa: F401
    update_page, create_page,
    get_container_html, set_container_html,
    _patch_page_content, _CONTAINER_RE, _get_body,
)

DAEMON_SOCK  = Path('/tmp/onenote_ops.sock')
DAEMON_PID   = Path('/tmp/onenote_ops.pid')
IDLE_TIMEOUT = 6 * 3600

# ---------------------------------------------------------------------------
# Background page-ID pre-population (removed in Phase 2)
# ---------------------------------------------------------------------------

PREPOP_CONCURRENCY = 8
_PREPOP_LOG        = Path('/tmp/onenote_prepop.log')
_PREPOP_STATUS     = Path('/tmp/onenote_prepop_status.json')
_prepop_cancel     = False


async def _fetch_section_with_retry(client, nb_name: str, sec_name: str,
                                     sec_id: str, cached_mod: str,
                                     max_retries: int = 4) -> tuple:
    """Fetch + cache pages for one section. Returns (page_count, status_str)."""
    from onenote_setup import list_pages, get_section_modified
    for attempt in range(max_retries):
        try:
            pages = await list_pages(client, sec_id)
            try:
                sec_mod = await get_section_modified(client, sec_id)
            except Exception:
                sec_mod = cached_mod
            update_pages_cache(nb_name, sec_name, pages, section_modified=sec_mod)
            return len(pages), 'ok'
        except Exception as e:
            msg = str(e)
            if '429' in msg or 'throttl' in msg.lower() or 'TooManyRequests' in msg:
                wait = min(2 * (2 ** attempt), 60)
                await asyncio.sleep(wait)
            else:
                return 0, f'err:{msg[:80]}'
    return 0, 'throttled'


async def prepopulate_page_ids(client=None, concurrency: int = PREPOP_CONCURRENCY,
                                log_file: Path = None) -> dict:
    """Pre-populate page IDs for all sections in the local cache."""
    global _prepop_cancel
    _prepop_cancel = False

    from onenote_setup import make_graph_client
    if client is None:
        client = make_graph_client()

    cache = _load_cache()
    work, skip_count = [], 0
    for nb_name, nb_data in cache.items():
        if nb_name.startswith('_'):
            continue
        for sec_name, sec_data in nb_data.get('sections', {}).items():
            sec_id = sec_data.get('id')
            if not sec_id:
                continue
            pages = sec_data.get('pages', [])
            if pages and all(isinstance(p, dict) and p.get('id') for p in pages):
                skip_count += 1
                continue
            work.append((nb_name, sec_name, sec_id, sec_data.get('last_modified', '')))

    total, done, errors, total_pages = len(work), 0, 0, 0
    t_start = time.perf_counter()
    sem = asyncio.Semaphore(concurrency)

    def _write_progress():
        elapsed = max(time.perf_counter() - t_start, 0.001)
        rate = done / elapsed
        pct = done / total if total else 1.0
        bar_w = 24
        filled = int(bar_w * pct)
        bar = ('=' * filled + ('>' if filled < bar_w else '') +
               ' ' * max(bar_w - filled - 1, 0))
        line = (f"prepop [{bar}] {done}/{total} secs "
                f"| {rate:.1f}/s | skip={skip_count} err={errors} pages={total_pages}")
        if log_file:
            Path(log_file).write_text(line + '\n')
        else:
            print(f"\r{line}", end='', file=sys.stderr, flush=True)
        try:
            _PREPOP_STATUS.write_text(json.dumps({
                'done': done, 'total': total, 'skip': skip_count,
                'errors': errors, 'pages': total_pages,
                'rate': round(rate, 2),
                'elapsed': round(elapsed, 1), 'running': True,
            }))
        except Exception:
            pass

    async def _worker(nb_name, sec_name, sec_id, cached_mod):
        nonlocal done, errors, total_pages
        if _prepop_cancel:
            return
        async with sem:
            if _prepop_cancel:
                return
            count, status = await _fetch_section_with_retry(
                client, nb_name, sec_name, sec_id, cached_mod)
            done += 1
            total_pages += count
            if status != 'ok':
                errors += 1
            _write_progress()

    loop = asyncio.get_event_loop()
    def _on_cancel():
        global _prepop_cancel
        _prepop_cancel = True
    try:
        loop.add_signal_handler(signal.SIGTERM, _on_cancel)
        loop.add_signal_handler(signal.SIGINT, _on_cancel)
    except (NotImplementedError, RuntimeError):
        pass

    _write_progress()
    tasks = [asyncio.create_task(_worker(*args)) for args in work]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        _prepop_cancel = True
        for t in tasks:
            t.cancel()

    if not log_file:
        print('', file=sys.stderr)

    elapsed = time.perf_counter() - t_start
    result = {
        'done': done, 'total': total, 'skip': skip_count,
        'errors': errors, 'pages': total_pages,
        'elapsed': round(elapsed, 1), 'cancelled': _prepop_cancel,
    }
    try:
        _PREPOP_STATUS.write_text(json.dumps({**result, 'running': False}))
    except Exception:
        pass
    if log_file:
        verb = 'cancelled' if _prepop_cancel else 'complete'
        Path(log_file).write_text(
            f"{verb}: {done}/{total} sections, {total_pages} pages, "
            f"{errors} errors in {elapsed:.1f}s\n"
        )
    return result


async def _background_prepopulate() -> None:
    """Daemon background task: pre-populate page IDs 5 s after daemon start."""
    await asyncio.sleep(5)
    try:
        from onenote_setup import make_graph_client
        client = make_graph_client()
        await prepopulate_page_ids(client, log_file=_PREPOP_LOG)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        try:
            _PREPOP_LOG.write_text(f"prepopulate failed: {e}\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Daemon — async UNIX socket server (removed in Phase 2)
# ---------------------------------------------------------------------------

_daemon_last_active: float = 0.0


async def _daemon_dispatch(req: dict) -> list:
    """Route a daemon request to the appropriate function."""
    cmd = req.get('cmd')

    if cmd == 'search':
        limit = req.get('limit', 20)
        if limit == 0:
            limit = None
        all_hits = search_pages(req['query'])
        hits = all_hits[:limit] if limit else all_hits
        lines = [f"{h['title']}  |  {h['notebook']} / {h['section']}" for h in hits]
        if len(all_hits) > len(hits):
            lines.insert(0, f"[{len(all_hits)} matches — showing {len(hits)}, use --limit N for more]")
        return lines or ['No results.']

    from onenote_setup import make_graph_client
    client = make_graph_client()

    if cmd == 'list-notebooks':
        nbs = await get_notebooks(client)
        return [n['name'] for n in nbs]

    elif cmd == 'list-sections':
        secs = await get_sections(client, req['notebook'])
        return [s['name'] for s in secs]

    elif cmd == 'list-pages':
        pages = await get_pages(client, req['notebook'], req['section'])
        return [p['title'] for p in pages]

    elif cmd == 'read-page':
        result = await find_page(client, req['notebook'], req['section'], req['page'])
        content = result['content']
        max_chars = req.get('max_chars', 4000)
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + f'\n... [truncated — {len(result["content"])} chars total, use --full for complete content]'
        return content.splitlines()

    elif cmd == 'read-page-html':
        result = await find_page(client, req['notebook'], req['section'], req['page'])
        return result['html'].splitlines()

    elif cmd == 'refresh':
        stats = await refresh_notebook(client, req['notebook'])
        return [f"Refreshed '{req['notebook']}': {stats['sections']} sections, {stats['pages']} pages"]

    else:
        raise ValueError(f"Unknown command: {cmd}")


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global _daemon_last_active
    _daemon_last_active = time.time()
    try:
        line = await reader.readline()
        req = json.loads(line.decode().strip())
        lines = await _daemon_dispatch(req)
        resp = json.dumps({'status': 'ok', 'lines': lines}) + '\n'
    except Exception as e:
        resp = json.dumps({'status': 'error', 'message': str(e)}) + '\n'
    writer.write(resp.encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _idle_watchdog(server: asyncio.AbstractServer) -> None:
    while True:
        await asyncio.sleep(300)
        if time.time() - _daemon_last_active > IDLE_TIMEOUT:
            print(f"Daemon idle for {IDLE_TIMEOUT//3600}h — shutting down.", file=sys.stderr)
            server.close()
            if DAEMON_SOCK.exists():
                DAEMON_SOCK.unlink()
            if DAEMON_PID.exists():
                DAEMON_PID.unlink()
            os._exit(0)


def run_daemon() -> None:
    global _daemon_last_active
    _daemon_last_active = time.time()

    if DAEMON_SOCK.exists():
        DAEMON_SOCK.unlink()

    DAEMON_PID.write_text(str(os.getpid()))

    async def _serve():
        server = await asyncio.start_unix_server(_handle_client, path=str(DAEMON_SOCK))
        asyncio.create_task(_idle_watchdog(server))
        asyncio.create_task(_background_prepopulate())
        print(f"Daemon started (pid {os.getpid()}, idle timeout {IDLE_TIMEOUT//3600}h)", file=sys.stderr)
        async with server:
            await server.serve_forever()

    try:
        asyncio.run(_serve())
    finally:
        for p in (DAEMON_SOCK, DAEMON_PID):
            if p.exists():
                p.unlink()


def _daemon_running() -> bool:
    if not DAEMON_PID.exists() or not DAEMON_SOCK.exists():
        return False
    try:
        pid = int(DAEMON_PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def _start_daemon_bg() -> None:
    import subprocess
    subprocess.Popen(
        [sys.executable, __file__, '--serve'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _call_daemon(request: dict) -> list:
    import socket as _socket
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    for attempt in range(10):
        try:
            sock.connect(str(DAEMON_SOCK))
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if attempt == 9:
                raise RuntimeError("Could not connect to daemon socket.")
            time.sleep(0.2)
    try:
        sock.sendall((json.dumps(request) + '\n').encode())
        data = b''
        while not data.endswith(b'\n'):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    resp = json.loads(data.decode())
    if resp['status'] == 'error':
        raise RuntimeError(resp['message'])
    return resp['lines']


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main_async(args):
    cmd_map = {k: v for k, v in vars(args).items() if k != 'cmd' and v is not None}
    cmd_map['cmd'] = args.cmd

    if args.cmd == 'search':
        limit = args.limit if args.limit > 0 else None
        all_hits = search_pages(args.query)
        hits = all_hits[:limit] if limit else all_hits
        lines = [f"{h['title']}  |  {h['notebook']} / {h['section']}" for h in hits]
        if len(all_hits) > len(hits):
            lines.insert(0, f"[{len(all_hits)} matches — showing {len(hits)}, use --limit N for more]")
        for line in (lines or ['No results.']):
            print(line)
        return

    if args.cmd == 'search-content':
        limit = args.limit if args.limit > 0 else None
        context = args.context
        hits = search_content(args.query, context_chars=context, limit=limit)
        if not hits:
            print('No results in cached pages.')
            return
        for h in hits:
            print(f"\n{'='*60}")
            print(f"  {h['title']}  |  {h['notebook']} / {h['section']}")
            print(f"  ({len(h['snippets'])} occurrence{'s' if len(h['snippets']) != 1 else ''})")
            for i, snip in enumerate(h['snippets'][:3], 1):
                print(f"\n  [{i}] ...{snip}...")
            if len(h['snippets']) > 3:
                print(f"\n  ... and {len(h['snippets']) - 3} more occurrence(s)")
        return

    if args.cmd == 'routing-index':
        available = [f.stem for f in sorted(SUMMARIES_DIR.glob('*.json'))]
        if args.notebook:
            nb_lower = {n.lower() for n in args.notebook}
            available = [nb for nb in available if nb.lower() in nb_lower]
        if not available:
            print('No summary files found. Build with: python3 build_summaries.py <Notebook>')
            return
        print(_build_compact_index(available))
        return

    if _daemon_running():
        try:
            for line in _call_daemon(cmd_map):
                print(line)
            return
        except Exception:
            pass

    if not _daemon_running():
        _start_daemon_bg()

    from onenote_setup import make_graph_client
    client = make_graph_client()

    if args.cmd == 'list-notebooks':
        for n in await get_notebooks(client):
            print(n['name'])

    elif args.cmd == 'list-sections':
        for s in await get_sections(client, args.notebook):
            print(s['name'])

    elif args.cmd == 'list-pages':
        for p in await get_pages(client, args.notebook, args.section):
            print(p['title'])

    elif args.cmd == 'read-page':
        result = await find_page(client, args.notebook, args.section, args.page)
        content = result['content']
        max_chars = 0 if args.full else args.max_chars
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + f'\n... [truncated — {len(result["content"])} chars total, use --full for complete content]'
        print(content)

    elif args.cmd == 'read-page-html':
        result = await find_page(client, args.notebook, args.section, args.page)
        print(result['html'])

    elif args.cmd == 'refresh':
        stats = await refresh_notebook(client, args.notebook)
        print(f"Refreshed '{args.notebook}': {stats['sections']} sections, {stats['pages']} pages")

    elif args.cmd == 'prepopulate':
        result = await prepopulate_page_ids(client)
        verb = 'Cancelled' if result['cancelled'] else 'Done'
        print(f"{verb}: {result['done']}/{result['total']} sections populated, "
              f"{result['pages']} pages, {result['errors']} errors in {result['elapsed']}s "
              f"({result['skip']} already complete)")

    elif args.cmd == 'prepopulate-status':
        if _PREPOP_STATUS.exists():
            s = json.loads(_PREPOP_STATUS.read_text())
            state = 'running' if s.get('running') else ('cancelled' if s.get('cancelled') else 'done')
            print(f"Status: {state} | {s['done']}/{s['total']} sections | "
                  f"{s['pages']} pages | {s['errors']} errors | "
                  f"{s.get('rate', 0):.1f}/s | {s['elapsed']}s elapsed")
        elif _PREPOP_LOG.exists():
            print(_PREPOP_LOG.read_text().strip())
        else:
            print("No pre-population run found.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='OneNote CLI')
    parser.add_argument('--serve', action='store_true',
                        help='Run as background daemon (started automatically)')
    sub = parser.add_subparsers(dest='cmd')

    sub.add_parser('list-notebooks')

    p = sub.add_parser('list-sections')
    p.add_argument('notebook')

    p = sub.add_parser('list-pages')
    p.add_argument('notebook')
    p.add_argument('section')

    p = sub.add_parser('read-page')
    p.add_argument('notebook')
    p.add_argument('section')
    p.add_argument('page')
    p.add_argument('--max-chars', type=int, default=4000, dest='max_chars',
                   help='Truncate content at N chars (default 4000). Use --full to disable.')
    p.add_argument('--full', action='store_true', help='Return full content without truncation')

    p = sub.add_parser('read-page-html')
    p.add_argument('notebook')
    p.add_argument('section')
    p.add_argument('page')

    p = sub.add_parser('refresh')
    p.add_argument('notebook', help='Refresh all sections + pages in parallel')

    p = sub.add_parser('search')
    p.add_argument('query', help='Search page titles (grep, no API)')
    p.add_argument('--limit', type=int, default=20,
                   help='Max results to show (default 20). Use 0 for all.')

    p = sub.add_parser('search-content')
    p.add_argument('query', help='Search cached page content (no API — offline only)')
    p.add_argument('--limit', type=int, default=0,
                   help='Max pages to show (default 0 = all). Use N to cap.')
    p.add_argument('--context', type=int, default=200,
                   help='Characters of context around each match (default 200).')

    p = sub.add_parser('routing-index',
                       help='Print compact routing index for the agent to route inline (no subprocess)')
    p.add_argument('--notebook', nargs='+', metavar='NOTEBOOK',
                   help='Limit to these notebooks (default: all with summaries)')

    sub.add_parser('prepopulate',
                   help='Pre-populate page IDs for all sections (live progress bar)')
    sub.add_parser('prepopulate-status',
                   help='Show status of last pre-population run')

    args = parser.parse_args()

    if args.serve:
        run_daemon()
    elif not args.cmd:
        parser.print_help()
    else:
        asyncio.run(main_async(args))
