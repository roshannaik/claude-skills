# OneNote Skill

Read OneNote notebooks via the Microsoft Graph API (read-only). Supports listing notebooks/sections/pages, reading page content, and **semantic search across all pages** via Gemini embeddings.

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
   - `User.Read`

   (The skill is read-only; `Notes.ReadWrite` is not needed. If you previously granted it, you can leave it or remove it — unused.)
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
python3 ~/Projects/skills/onenote/scripts/onenote_setup.py
```

This prints a device code and a URL. Open the URL in any browser, enter the code, and sign in. The token is cached at `~/.cache/ms_graph_token_cache.json` — subsequent runs skip this step entirely.

---

## Build the semantic-search index

After authenticating and letting the skill cache some pages (any `read-page` or `refresh` call populates the content cache), run:

```bash
python3 ~/Projects/skills/onenote/scripts/build_embeddings.py
```

This embeds every cached page via Gemini and writes `cache/embeddings.npz`. It's **incremental** — on later runs, only pages whose `last_modified` has changed are re-embedded. A first full build over ~1K pages takes ~25 minutes on Gemini's free tier due to the 30K TPM rate cap. Incremental updates are near-instant.

Re-run after significant edits, or let the skill trigger it automatically as needed.

---

## Keep the cache fresh (optional background sync)

`scripts/sync.py` is a single-shot job that keeps the local cache and embeddings in sync with OneNote. It starts with a one-call `list_notebooks` check (~1 s) and only drills into notebooks whose `last_modified` has changed. For an unchanged account the whole sync is ~1–2 s; when pages have changed it refetches just those, prunes HTML for deleted pages, and incrementally rebuilds embeddings for the delta.

Newly shared notebooks need to be opened at least once in OneNote (web, desktop, or mobile) to get picked up automatically on the next sync. Until then, the share invite is "pending" from the API's perspective and invisible to any caller — including this sync.

### Manual sync

```bash
python3 ~/Projects/skills/onenote/scripts/sync.py                  # sync now
python3 ~/Projects/skills/onenote/scripts/sync.py status           # idle / running
python3 ~/Projects/skills/onenote/scripts/sync.py unstick          # kill a hung sync
```

Only one sync runs at a time (enforced by `fcntl.flock`), so it's safe to invoke from any harness, cron, launchd, or a keystroke. The kernel releases the lock when the process dies — stale lockfiles can't block future runs.

### Schedule a 3-hour background sync (macOS, launchd)

Install a user launch agent that fires every 3 hours and at login:

```bash
cat > ~/Library/LaunchAgents/com.claude-skills.onenote-sync.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.claude-skills.onenote-sync</string>

  <!-- zsh -ic sources ~/.zshrc so MS_CLIENT_ID + GEMINI_API_KEY are in
       scope without duplicating secrets into the plist. -->
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-ic</string>
    <string>exec /usr/bin/python3 $HOME/Projects/skills/onenote/scripts/sync.py sync --quiet --max-duration 600</string>
  </array>

  <key>StartInterval</key>
  <integer>10800</integer>
  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$HOME/Projects/skills/onenote/cache/sync.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Projects/skills/onenote/cache/sync.launchd.log</string>

  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-skills.onenote-sync.plist
```

Verify it's loaded:

```bash
launchctl list | grep onenote-sync
launchctl print gui/$(id -u)/com.claude-skills.onenote-sync | grep -E "state|run interval|last exit"
```

Kick off a run on demand without waiting 3 hours:

```bash
launchctl kickstart -k gui/$(id -u)/com.claude-skills.onenote-sync
```

Unload / uninstall:

```bash
launchctl bootout gui/$(id -u)/com.claude-skills.onenote-sync
rm ~/Library/LaunchAgents/com.claude-skills.onenote-sync.plist
```

Bash users: put the env vars in `~/.bashrc` and change the `-ic` line to `/bin/bash -ic`. Linux users: use cron or a systemd user timer with equivalent effect — the sync script itself is platform-agnostic.

### Sync logs and stuck-process recovery

Each run appends one JSON line to `cache/sync.log`:

```json
{"ts":"2026-04-19T03:13:08+00:00","status":"ok","elapsed_sec":1.9,"nb_dirty":0,"pages_added":0,"pages_mod":0,"pages_del":0,"embed_rebuilt":0,"embed_reused":1001}
```

Tail it any time: `tail -f cache/sync.log | jq .`

If a sync wedges (e.g. Graph API hangs), it self-kills via `SIGALRM` after `--max-duration` seconds (default 600) and logs `status: "timeout"`. As a belt-and-suspenders measure, the lockfile body carries `{pid, started_at, hostname}`, so `sync.py unstick` can find and `SIGTERM/SIGKILL` the owning process even if the heartbeat was never written.

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
onenote_ops.py query "<natural-language query>" [--top-k N] [--notebook NAME]
onenote_ops.py search-title "<title keyword>"  # title grep, no API
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

Sync runtime state (also under `cache/`, gitignored):

- `.sync.lock` — `flock`-held mutex; body carries `{pid, started_at, hostname, max_duration_sec}` for `status` / `unstick`
- `.sync.heartbeat` — current step + timestamp, rewritten every 5 s
- `.sync.state.json` — result of the most recent run
- `sync.log` — JSONL, one row per run (timestamp, status, elapsed, page counts, embed counts, any error)
- `sync.launchd.log` — stdout/stderr from the scheduled launchd job (if installed)

All of these are in `.gitignore` and never committed. They rebuild automatically on a new machine after you run `onenote_setup.py` once and then `build_embeddings.py`.
