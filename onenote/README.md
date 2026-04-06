# OneNote Skill

Read and write OneNote notebooks via the Microsoft Graph API. Supports listing notebooks/sections/pages, reading page content, and creating or updating pages.

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

Packages: `msal`, `msgraph-sdk`, `kiota-abstractions`, `kiota-authentication-azure`, `kiota-http`

### 3. Azure app registration

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

### 4. Set the Client ID environment variable

Add to your `~/.zshrc` or `~/.bashrc`:

```bash
export MS_CLIENT_ID="your-client-id-from-step-7"
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

## Usage

Once installed, invoke in Claude Code:

```
/onenote read Health/Supplements/My Stack
/onenote list sections in Home Stuff
/onenote update page X in notebook Y
```

Or describe what you want in natural language — Claude Code will invoke the skill automatically.

---

## What gets cached

The skill caches API responses locally for speed:

- `references/onenote_cache.json` — full notebook/section/page index (never read directly — too large)
- `references/page_index.txt` — grep-able search index (~9K tokens)
- `references/page_content/` — individual page HTML snapshots

These are in `.gitignore` and never committed. They are rebuilt automatically on first use on a new machine.
