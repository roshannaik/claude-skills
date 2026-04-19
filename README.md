# Claude Code Skills

Three skills for [Claude Code](https://claude.ai/code) and that connect to Microsoft 365 and provide a code maintenance workflow.

| Skill | What it does |
|---|---|
| `onenote` | Read and write OneNote notebooks via Microsoft Graph API |
| `office` | Read and write Excel, Word, and PowerPoint files on OneDrive |
| `code-maintenance` | Holistic audit and cleanup of incrementally evolved code |

---

## Prerequisites

### 1. Python 3.11+

```bash
python3 --version   # should be 3.11 or higher
```

If not installed:
- macOS: `brew install python`
- Ubuntu/Debian: `sudo apt install python3`

### 2. Python dependencies (onenote + office skills only)

Each skill ships its own `requirements.txt`:

```bash
pip3 install -r onenote/requirements.txt   # for the onenote skill
pip3 install -r office/requirements.txt    # for the office skill (superset of onenote's)
```

### 3. Azure app registration (onenote + office skills only)

The skills authenticate via Microsoft's device-code flow — no browser popup, works in the terminal. You need to register an app in Azure to get a Client ID.

**Steps:**

1. Go to [https://portal.azure.com](https://portal.azure.com) and sign in with your Microsoft account.
2. Navigate to **Azure Active Directory → App registrations → New registration**.
3. Name: anything (e.g. `claude-skills`)
4. Supported account types: **Personal Microsoft accounts only**
5. Redirect URI: leave blank
6. Click **Register**
7. Copy the **Application (client) ID** — you'll need it in the next step.
8. Go to **API permissions → Add a permission → Microsoft Graph → Delegated permissions** and add:
   - `Notes.Read`
   - `Notes.ReadWrite`
   - `Files.Read`
   - `Files.ReadWrite`
   - `User.Read`
9. Click **Grant admin consent** (or just proceed — it will prompt on first auth).

### 4. Set the Client ID environment variable

Add to your `~/.zshrc` or `~/.bashrc`:

```bash
export MS_CLIENT_ID="your-client-id-from-step-7-above"
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
./install.sh
```

This symlinks each skill into `~/.claude/skills/<skill>`, pointing back to the cloned repo. No files are copied — edits in the repo are reflected immediately, and `git pull` is all you need to update.

---

## First-time authentication (onenote + office)

```bash
python3 skills/onenote/scripts/onenote_setup.py
```

This prints a device code and a URL. Open the URL in any browser, enter the code, and sign in. The token is cached at `~/.cache/ms_graph_token_cache.json` — subsequent runs skip this step entirely.

---

## Usage

Once installed, invoke skills in Claude Code with a `/` prefix:

```
/onenote read MyNotebook/MySection/MyPage
/office read budget.xlsx
/code-maintenance ~/.claude/skills/onenote
```

Or just describe what you want in natural language — Claude Code will invoke the right skill automatically.

---

## What gets cached (and what doesn't get committed)

The onenote and office skills cache API responses locally for speed:

- `onenote/cache/` — notebook/section/page index and page content
- `office/cache/excel/` — Excel tab descriptions

These directories are in `.gitignore` and never committed. They are rebuilt automatically on first use on a new machine.

---

## Updating

```bash
git pull
```

No reinstall needed — the symlinks stay valid.
