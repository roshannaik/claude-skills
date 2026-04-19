#!python3
"""
OneNote / OneDrive access via Microsoft Graph API
Uses MSAL device-code flow — no browser popup needed, works in CLI and Claude Code.

Usage:
    python3 onenote_setup.py

On first run it prints a device code; go to https://microsoft.com/devicelogin,
enter the code, sign in. The token is cached locally so subsequent runs skip this.

Imports of msal / msgraph / kiota are deferred into the functions that use them,
so importing this module does NOT pull in the Graph/auth stack. Cache-only callers
(e.g. semantic search, offline page reads) can import `onenote_setup` without
needing those packages installed at all.
"""

import asyncio
import json
import os
import time
from pathlib import Path

# --- Config -----------------------------------------------------------
TENANT_ID = os.environ.get("MS_TENANT_ID", "consumers")  # "consumers" for personal MSA
TOKEN_CACHE_PATH = Path.home() / ".cache" / "ms_graph_token_cache.json"

SCOPES = [
    "Notes.Read",
    "Notes.ReadWrite",
    "Files.Read",
    "Files.ReadWrite",
    "User.Read",
]


def _client_id() -> str:
    cid = os.environ.get("MS_CLIENT_ID")
    if not cid:
        raise RuntimeError(
            "MS_CLIENT_ID environment variable is not set. See the README for setup instructions."
        )
    return cid


# --- Token cache helpers -----------------------------------------------

def _load_cache():
    import msal
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())
    return cache


def _save_cache(cache) -> None:
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize())


# --- Auth ---------------------------------------------------------------

_token_cache_mem: dict = {'token': None, 'expires_at': 0.0}

def get_access_token() -> str:
    """
    Returns a valid access token, prompting device-code auth if needed.
    Token is cached in-memory (TTL = expiry - 60s) and on disk.
    """
    if (_token_cache_mem['token'] and
            time.time() < _token_cache_mem['expires_at'] - 60):
        return _token_cache_mem['token']

    import msal
    cache = _load_cache()
    app = msal.PublicClientApplication(
        _client_id(),
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        token_cache=cache,
    )

    accounts = app.get_accounts()
    result = None

    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")
        print("\n" + flow["message"])
        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache)

    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description')}")

    _token_cache_mem['token'] = result['access_token']
    _token_cache_mem['expires_at'] = time.time() + result.get('expires_in', 3600)
    return result['access_token']


# --- Graph client factory -----------------------------------------------

def _make_token_provider_class():
    """Build the MSAL-backed AccessTokenProvider subclass lazily."""
    from kiota_abstractions.authentication import AccessTokenProvider, AllowedHostsValidator

    class _MSALTokenProvider(AccessTokenProvider):
        """Wraps MSAL token acquisition for use with GraphServiceClient."""

        def __init__(self):
            self._token = get_access_token()

        async def get_authorization_token(self, uri: str, additional_authentication_context=None) -> str:
            return self._token

        def get_allowed_hosts_validator(self) -> AllowedHostsValidator:
            return AllowedHostsValidator(["graph.microsoft.com"])

    return _MSALTokenProvider


def make_graph_client():
    """
    Returns an authenticated GraphServiceClient using MSAL device-code flow.
    Prompts for device-code login on first run; uses cached token thereafter.
    """
    from msgraph import GraphServiceClient
    from kiota_abstractions.authentication import BaseBearerTokenAuthenticationProvider
    from kiota_http.httpx_request_adapter import HttpxRequestAdapter

    token_provider = _make_token_provider_class()()
    auth_provider = BaseBearerTokenAuthenticationProvider(access_token_provider=token_provider)
    adapter = HttpxRequestAdapter(authentication_provider=auth_provider)
    return GraphServiceClient(request_adapter=adapter)


# --- OneNote helpers ----------------------------------------------------

async def list_notebooks(client) -> list[dict]:
    notebooks = await client.me.onenote.notebooks.get()
    return [
        {"id": nb.id, "name": nb.display_name, "last_modified": str(nb.last_modified_date_time)}
        for nb in (notebooks.value or [])
    ]


async def list_sections(client, notebook_id: str) -> list[dict]:
    sections = await client.me.onenote.notebooks.by_notebook_id(notebook_id).sections.get()
    return [
        {"id": s.id, "name": s.display_name,
         "last_modified": str(s.last_modified_date_time)}
        for s in (sections.value or [])
    ]


async def get_notebook_modified(client, notebook_id: str) -> str:
    """Lightweight fetch — returns just lastModifiedDateTime for a notebook."""
    nb = await client.me.onenote.notebooks.by_notebook_id(notebook_id).get()
    return str(nb.last_modified_date_time)


async def get_section_modified(client, section_id: str) -> str:
    """Lightweight fetch — returns just lastModifiedDateTime for a section."""
    sec = await client.me.onenote.sections.by_onenote_section_id(section_id).get()
    return str(sec.last_modified_date_time)


async def list_pages(client, section_id: str) -> list[dict]:
    pages = await client.me.onenote.sections.by_onenote_section_id(section_id).pages.get()
    return [
        {"id": p.id, "title": p.title, "last_modified": str(p.last_modified_date_time)}
        for p in (pages.value or [])
    ]


async def get_page_content(client, page_id: str) -> str:
    """Returns the HTML content of a OneNote page."""
    content_stream = await client.me.onenote.pages.by_onenote_page_id(page_id).content.get()
    return content_stream.decode("utf-8") if content_stream else ""


async def list_all_sections(client, notebooks: list[dict]) -> dict[str, list[dict]]:
    """Fetch sections for all notebooks in parallel.
    Returns {notebook_id: [sections]}. Failed notebooks return []."""
    async def _fetch(nb):
        try:
            return nb['id'], await list_sections(client, nb['id'])
        except Exception:
            return nb['id'], []
    results = await asyncio.gather(*[_fetch(nb) for nb in notebooks])
    return dict(results)


async def list_all_pages(client, sections: list[dict]) -> dict[str, list[dict]]:
    """Fetch pages for all sections in parallel.
    Returns {section_id: [pages]}. Failed sections return []."""
    async def _fetch(sec):
        try:
            return sec['id'], await list_pages(client, sec['id'])
        except Exception:
            return sec['id'], []
    results = await asyncio.gather(*[_fetch(sec) for sec in sections])
    return dict(results)


# --- Demo ---------------------------------------------------------------

async def main():
    client = make_graph_client()

    print("Fetching notebooks...")
    notebooks = await list_notebooks(client)
    print(json.dumps(notebooks, indent=2))

    if notebooks:
        nb_id = notebooks[0]["id"]
        print(f"\nSections in '{notebooks[0]['name']}':")
        sections = await list_sections(client, nb_id)
        print(json.dumps(sections, indent=2))

        if sections:
            sec_id = sections[0]["id"]
            print(f"\nPages in '{sections[0]['name']}':")
            pages = await list_pages(client, sec_id)
            print(json.dumps(pages, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
