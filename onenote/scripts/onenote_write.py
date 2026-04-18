"""OneNote write operations.

update_page replaces the full page body. Always read the page HTML first and
reconstruct with set_container_html(), or the unchanged content is lost.
"""
import json
import re
import urllib.request


def _patch_page_content(page_id: str, patch_body: list) -> None:
    """Send a PATCH request to the OneNote page content endpoint."""
    from onenote_setup import get_access_token
    token = get_access_token()
    url = f'https://graph.microsoft.com/v1.0/me/onenote/pages/{page_id}/content'
    data = json.dumps(patch_body).encode('utf-8')
    req = urllib.request.Request(
        url, data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        method='PATCH',
    )
    with urllib.request.urlopen(req):
        pass


async def update_page(client, page_id: str, new_html_content: str):
    """Replace the entire body of a OneNote page."""
    _patch_page_content(page_id, [{"target": "body", "action": "replace", "content": new_html_content}])


_CONTAINER_RE = re.compile(
    r'(<div\b[^>]*style="[^"]*position:absolute[^"]*"[^>]*>)(.*?)(</div>)',
    re.DOTALL | re.IGNORECASE,
)


def _get_body(html: str) -> str:
    m = re.search(r'<body[^>]*>(.*)</body>', html, re.DOTALL | re.IGNORECASE)
    if not m:
        raise ValueError("Could not parse page body.")
    return m.group(1)


def get_container_html(html: str) -> str:
    """Return the inner HTML of the single note container in a page.

    Raises ValueError if the page has zero or multiple note containers.
    """
    matches = _CONTAINER_RE.findall(_get_body(html))
    if len(matches) == 0:
        raise ValueError("Page has no note containers.")
    if len(matches) > 1:
        raise ValueError(
            f"Page has {len(matches)} note containers — only single-container pages are supported."
        )
    return matches[0][1]


def set_container_html(html: str, new_inner: str) -> str:
    """Return page body HTML with the single container's inner HTML replaced.

    Pass the return value directly to update_page().
    Raises ValueError if the page has zero or multiple note containers.
    """
    body = _get_body(html)
    matches = list(_CONTAINER_RE.finditer(body))
    if len(matches) == 0:
        raise ValueError("Page has no note containers.")
    if len(matches) > 1:
        raise ValueError(
            f"Page has {len(matches)} note containers — only single-container pages are supported."
        )
    m = matches[0]
    return body[:m.start(2)] + new_inner + body[m.end(2):]


async def create_page(client, section_id: str, title: str, html_body: str):
    html = f"""<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body>{html_body}</body></html>"""
    return await client.post(
        f"/me/onenote/sections/{section_id}/pages",
        data=html.encode('utf-8'),
        headers={"Content-Type": "text/html"}
    )
