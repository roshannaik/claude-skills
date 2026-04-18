# OneNote Skill

Read and write OneNote notebooks via the Microsoft Graph API. Supports listing notebooks/sections/pages, reading page content, creating or updating pages, and **semantic search across all pages** via Gemini embeddings.

---

## Prerequisites

### 1. Python 3.11+

```bash
python3 --version   # should be 3.11 or higher
```

If not installed:
- macOS: `brew install python`
- Ubuntu/Debian: `sudo apt install python3`

### 2. Python dependencies

```bash
pip install -r onenote/requirements.txt
```

Packages: `msal`, `msgraph-sdk`, `microsoft-kiota-abstractions`, `microsoft-kiota-authentication-azure`, `microsoft-kiota-http`, `google-genai`, `numpy`.

### 3. Azure app registration (for Microsoft Graph / OneNote access)

The skill authenticates via Microsoft's device-code flow — no browser popup, works in the terminal. You need to register an app in Azure to get a Client ID.

**Steps:**

1. Go to [https://portal.azure.com](https://portal.azure.com) and sign in with your Microsoft account.
2. Navigate to **Azure Active Directory → App registrations → New registration**.
3. Name: anything (e.g. `claude-skills`)
4. Supported account types: **Personal Microsoft accounts only**
5. Redirect URI: leave blank
6. Click **Register**
7. Copy the **Application (client) ID**.
8. Go to **API permissions → Add a permission → Microsoft Graph → Delegated permissions** and add:
   - `Notes.Read`
   - `Notes.ReadWrite`
   - `User.Read`
9. Click **Grant admin consent** (or proceed — it will prompt on first auth).

### 4. Google AI Studio API key (for semantic search)

Semantic search embeds pages with Google's `gemini-embedding-001` model. Free tier is generous; no credit card required.

1. Go to [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey) and sign in with a Google account.
2. Click **Create API key** → copy it (starts with `AIza…`).

### 5. Set environment variables

Add to your `~/.zshrc` or `~/.bashrc`:

```bash
export MS_CLIENT_ID="your-azure-client-id-from-step-3"
export GEMINI_API_KEY="your-google-ai-studio-key-from-step-4"
```

Then reload:

```bash
source ~/.zshrc   # or ~/.bashrc
```

---

## Installation

```bash
git clone https://github.com/roshannaik/claude-skills.git
cd claude-skills
./onenote/install.sh
```

This creates a symlink `~/.claude/skills/onenote` pointing to the cloned repo directory. No files are copied — edits in the repo are reflected immediately, and `git pull` is all you need to update.

To uninstall:

```bash
./onenote/uninstall.sh
```

---

## First-time authentication

```bash
python3 ~/.claude/skills/onenote/scripts/onenote_setup.py
```

This prints a device code and a URL. Open the URL in any browser, enter the code, and sign in. The token is cached at `~/.cache/ms_graph_token_cache.json` — subsequent runs skip this step entirely.

---

## Build the semantic-search index

After authenticating and letting the skill cache some pages (any `read-page` or `refresh` call populates the content cache), run:

```bash
python3 ~/.claude/skills/onenote/scripts/build_embeddings.py
```

This embeds every cached page via Gemini and writes `cache/embeddings.npz`. It's **incremental** — on later runs, only pages whose `last_modified` has changed are re-embedded. A first full build over ~1K pages takes ~25 minutes on Gemini's free tier due to the 30K TPM rate cap. Incremental updates are near-instant.

Re-run after significant edits, or let the skill trigger it automatically as needed.

---

## Usage

Once installed, invoke in Claude Code:

```
/onenote what do my notes say about sleep supplements
/onenote read Health/Supplements/My Stack
/onenote list sections in Home Stuff
/onenote update page X in notebook Y
```

Or describe what you want in natural language — Claude Code will invoke the skill automatically.

CLI subcommands (for direct use outside Claude Code):

```bash
onenote_ops.py semantic-search "<natural-language query>" [--top-k N] [--notebook NAME]
onenote_ops.py search "<title keyword>"        # title grep, no API
onenote_ops.py search-content "<keyword>"      # HTML grep over cached pages
onenote_ops.py read-page <nb> <sec> <page>     # plain-text
onenote_ops.py read-page-html <nb> <sec> <page>
onenote_ops.py list-notebooks | list-sections <nb> | list-pages <nb> <sec>
onenote_ops.py refresh <nb>                    # force re-fetch sections + pages
```

---

## What gets cached

The skill caches API responses locally for speed:

- `cache/onenote_cache.json` — full notebook/section/page index (never read directly — too large)
- `cache/page_index.txt` — grep-able title + path index
- `cache/page_content/` — individual page HTML snapshots (keyed by page ID)
- `cache/embeddings.npz` — Gemini vectors for every cached page (~4 MB per ~1K pages)
- `cache/embeddings_meta.json` — per-page `last_modified` + title/notebook/section for incremental rebuilds and query-time metadata

All of these are in `.gitignore` and never committed. They rebuild automatically on a new machine after you run `onenote_setup.py` once and then `build_embeddings.py`.
