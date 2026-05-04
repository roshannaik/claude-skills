#!python3
"""Shared Gemini client + retry helpers used across the skill's LLM call sites
(embeddings, media OCR/caption/transcribe, subject classifier).

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


def _api_retry_delay(err: Exception) -> float | None:
    """Extract the server-suggested retry delay (seconds) from a 429 response body.

    Google's API embeds a RetryInfo proto at:
      err.details['error']['details'][n]['retryDelay']  e.g. '30s'
    Returns None if the hint is absent or unparseable.
    """
    try:
        details = err.details  # type: ignore[attr-defined]
        for d in details.get('error', {}).get('details', []):
            val = d.get('retryDelay', '')
            if val.endswith('s'):
                return float(val[:-1])
    except Exception:
        pass
    return None


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

    Uses the server-supplied retryDelay hint from 429 responses when present;
    falls back to exponential backoff otherwise.
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
            hint = _api_retry_delay(e)
            wait = (hint + 0.3) if hint is not None else min(max_wait, base_wait * (2 ** attempt))
            wait = min(wait, max_wait)
            tag = f'{label}: ' if label else ''
            source = 'server hint' if hint is not None else 'backoff'
            print(f'  ! {tag}transient ({str(e)[:80]}); sleep {wait:.0f}s [{source}] '
                  f'(attempt {attempt+1}/{max_attempts})', file=sys.stderr)
            time.sleep(wait)
    raise last  # unreachable, but satisfies type checkers
