# Office Skill

Read and write Excel, Word, and PowerPoint files stored on OneDrive via the Microsoft Graph API.

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
pip install -r office/requirements.txt
```

Packages: `msal`, `msgraph-sdk`, `kiota-abstractions`, `kiota-authentication-azure`, `kiota-http`, `python-docx`, `python-pptx`

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
   - `Files.Read`
   - `Files.ReadWrite`
   - `User.Read`
9. Click **Grant admin consent** (or proceed — it will prompt on first auth).

> If you already installed the `onenote` skill, you can reuse the same Azure app — just add the `Files.Read` and `Files.ReadWrite` permissions to it. The token cache at `~/.cache/ms_graph_token_cache.json` is shared.

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
./office/install.sh
```

This creates a symlink `~/.claude/skills/office` pointing to the cloned repo directory. No files are copied — edits in the repo are reflected immediately, and `git pull` is all you need to update.

To uninstall:

```bash
./office/uninstall.sh
```

---

## First-time authentication

If you have the `onenote` skill installed, the token is already cached. Otherwise run:

```bash
python3 -c "
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.home() / '.claude/skills/office'))
from office_ops import get_auth_token
get_auth_token()
"
```

This prints a device code and URL. Open the URL in any browser, enter the code, and sign in. The token is cached at `~/.cache/ms_graph_token_cache.json` — subsequent runs skip this step entirely.

---

## Usage

Once installed, invoke in Claude Code:

```
/office read budget.xlsx
/office list sheets in report.xlsx
/office read slides in deck.pptx
```

Or describe what you want in natural language — Claude Code will invoke the skill automatically.

---

## What gets cached

The skill caches Excel tab descriptions locally to avoid redundant API calls:

- `references/excel/<file_id>.json` — tab descriptions and staleness metadata per file

These are in `.gitignore` and never committed. They are rebuilt automatically on first use or when the file is modified.
