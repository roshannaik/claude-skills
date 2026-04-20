#!python3
"""Shared Gemini client + retry helpers used across the skill's LLM call sites
(embeddings v1/v2, media OCR/caption/transcribe, subject classifier).

Kept tiny and import-light so callers that don't touch Gemini (cache-only
CLI subcommands, search, etc.) are not taxed on startup.
"""
import os
import sys
import time


_TRANSIENT_TOKENS = ('429', '503', 'RESOURCE_EXHAUSTED', 'UNAVAILABLE')
_TRANSIENT_SUBSTRINGS = ('rate', 'quota')


def is_transient_error(err: Exception) -> bool:
    msg = str(err)
    low = msg.lower()
    return (any(tok in msg for tok in _TRANSIENT_TOKENS)
            or any(sub in low for sub in _TRANSIENT_SUBSTRINGS))


def get_client():
    """Return a google-genai Client using GEMINI_API_KEY or GOOGLE_API_KEY."""
    from google import genai
    api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        raise SystemExit('GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable is not set.')
    return genai.Client(api_key=api_key)


def with_retry(fn, *args, max_attempts: int = 6, base_wait: float = 4.0,
               max_wait: float = 60.0, label: str = '', **kwargs):
    """Invoke fn(*args, **kwargs) with exponential backoff on transient errors.

    Non-transient errors (auth, invalid arg, oversized payload) raise immediately.
    On final-attempt transient failure, re-raises the last exception.
    """
    last = None
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            if attempt == max_attempts - 1 or not is_transient_error(e):
                raise
            wait = min(max_wait, base_wait * (2 ** attempt))
            tag = f'{label}: ' if label else ''
            print(f'  ! {tag}transient ({str(e)[:80]}); sleep {wait:.0f}s '
                  f'(attempt {attempt+1}/{max_attempts})', file=sys.stderr)
            time.sleep(wait)
    raise last  # unreachable, but satisfies type checkers
