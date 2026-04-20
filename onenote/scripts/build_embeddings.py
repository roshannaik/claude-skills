#!python3
"""Build Gemini embeddings for cached OneNote pages.

Incremental: pages whose last_modified matches the stored meta are skipped.
Force-rebuild: pass --force.

v1 (default): one vector per page, gemini-embedding-001, text-only.
v2 (--v2):    chunked + multimodal, gemini-embedding-2-preview, text + media.

Usage:
  python3 scripts/build_embeddings.py              # incremental v1 for all notebooks
  python3 scripts/build_embeddings.py --force      # full v1 rebuild
  python3 scripts/build_embeddings.py --notebook Health AI   # limit to some

  python3 scripts/build_embeddings.py --v2 --pages-file cache/prototype_pages.txt
  python3 scripts/build_embeddings.py --v2 --pages <page_id> --pages <page_id>
  python3 scripts/build_embeddings.py --v2                    # all cached pages (expensive)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--v2', action='store_true',
                    help='Use the chunked + multimodal v2 pipeline '
                         '(gemini-embedding-2-preview).')
    ap.add_argument('--force', action='store_true',
                    help='Re-embed all pages (ignore last_modified freshness check)')
    # v1-only:
    ap.add_argument('--notebook', nargs='+', metavar='NOTEBOOK',
                    help='[v1] Limit to these notebooks (default: all)')
    # v2-only:
    ap.add_argument('--pages-file', metavar='PATH',
                    help='[v2] File with one page identifier per line')
    ap.add_argument('--pages', action='append', metavar='IDENT', default=[],
                    help='[v2] Page identifier (page_id or "Notebook / Section / Title"). '
                         'Repeatable.')
    args = ap.parse_args()

    if args.v2:
        from onenote_embeddings_v2 import build_embeddings as build_v2
        result = build_v2(page_ids=args.pages, pages_file=args.pages_file,
                          force=args.force)
        if 'error' in result:
            print(f"Error: {result['error']}", file=sys.stderr); sys.exit(1)
        print(f"\nDone: pages_targeted={result['pages_targeted']}, "
              f"pages_rebuilt={result['pages_rebuilt']}, "
              f"chunks_total={result['chunks_total']}, "
              f"chunks_embedded={result['chunks_embedded']}, "
              f"chunks_reused={result['chunks_reused']}, "
              f"elapsed={result['elapsed_sec']}s")
        return

    from onenote_embeddings import build_embeddings
    nb_filter = set(args.notebook) if args.notebook else None
    result = build_embeddings(force=args.force, notebook_filter=nb_filter)
    print(f"\nDone: total={result['total']}, rebuilt={result['rebuilt']}, "
          f"reused={result['reused']}, skipped_no_content={result['skipped_no_content']}, "
          f"elapsed={result['elapsed']}s")


if __name__ == '__main__':
    main()
