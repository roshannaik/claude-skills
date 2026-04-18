#!/usr/bin/env python3
"""Build Voyage embeddings for cached OneNote pages.

Incremental: pages whose last_modified matches the stored meta are skipped.
Force-rebuild: pass --force.

Usage:
  python3 scripts/build_embeddings.py              # incremental for all notebooks
  python3 scripts/build_embeddings.py --force      # rebuild everything
  python3 scripts/build_embeddings.py --notebook Health AI   # limit to some
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from onenote_embeddings import build_embeddings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true',
                    help='Re-embed all pages (ignore last_modified freshness check)')
    ap.add_argument('--notebook', nargs='+', metavar='NOTEBOOK',
                    help='Limit to these notebooks (default: all)')
    args = ap.parse_args()

    nb_filter = set(args.notebook) if args.notebook else None
    result = build_embeddings(force=args.force, notebook_filter=nb_filter)
    print(f"\nDone: total={result['total']}, rebuilt={result['rebuilt']}, "
          f"reused={result['reused']}, skipped_no_content={result['skipped_no_content']}, "
          f"tokens={result['tokens']}, elapsed={result['elapsed']}s")


if __name__ == '__main__':
    main()
