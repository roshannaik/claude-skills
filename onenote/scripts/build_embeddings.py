#!python3
"""Build chunked + multimodal Gemini embeddings for cached OneNote pages.

Incremental: pages whose `last_modified` matches the stored meta are skipped.
Force-rebuild: pass --force.

Usage:
  python3 scripts/build_embeddings.py                                   # incremental, all pages
  python3 scripts/build_embeddings.py --force                           # full rebuild
  python3 scripts/build_embeddings.py --pages-file cache/prototype.txt  # subset via file
  python3 scripts/build_embeddings.py --pages "Notebook / Section / Page" \
                                      --pages <page_id>                  # explicit list
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true',
                    help='Re-embed every chunk (ignore last_modified freshness)')
    ap.add_argument('--pages-file', metavar='PATH',
                    help='File with one page identifier per line')
    ap.add_argument('--pages', action='append', metavar='IDENT', default=[],
                    help='Page identifier (page_id or "Notebook / Section / Title"). '
                         'Repeatable.')
    args = ap.parse_args()

    from onenote_embeddings import build_embeddings
    result = build_embeddings(page_ids=args.pages, pages_file=args.pages_file,
                              force=args.force)
    if 'error' in result:
        print(f"Error: {result['error']}", file=sys.stderr); sys.exit(1)
    print(f"\nDone: pages_targeted={result['pages_targeted']}, "
          f"pages_rebuilt={result['pages_rebuilt']}, "
          f"chunks_total={result['chunks_total']}, "
          f"chunks_embedded={result['chunks_embedded']}, "
          f"chunks_reused={result['chunks_reused']}, "
          f"elapsed={result['elapsed_sec']}s")


if __name__ == '__main__':
    main()
