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

After authenticating and letting the skill cache some pages (any `read-page` or `refresh` call populates the content cache), there are two index paths — **v2 (default, recommended)** and **v1 (legacy)**.

### v2 — chunked + multimodal (recommended)

```bash
# 1. Pull media resources + run OCR/caption/transcribe (one-off, per new content)
python3 scripts/onenote_ops.py fetch-media --all

# 2. Classify pages by subject (one-off, for subject-aware filtering)
python3 scripts/classify_subjects.py

# 3. Build chunked embeddings (gemini-embedding-2-preview, 768d, unified text+media)
python3 scripts/build_embeddings.py --v2
```

Step 1 fetches each image/PDF/mp4 referenced in the cached HTML, then runs Gemini 2.5 flash OCR on every image (scene caption for empty-OCR ones) and transcription on audio/video. Step 2 labels each page so subject-aware queries work. Step 3 chunks each page adaptively and embeds text + media bytes into a single vector space.

All three are **incremental** — unchanged pages/resources are skipped. First full build on ~1K pages costs ~$2 on the Gemini paid tier; subsequent runs are pennies. Checkpoints are saved every 50 chunks so a crash mid-build keeps progress.

### v1 — page-level, text-only (legacy)

```bash
python3 scripts/build_embeddings.py
```

Kept for backward compatibility. Produces one vector per page with `gemini-embedding-001`. Run only if you explicitly want the legacy path; the v2 store supersedes it.

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
# Semantic search (v2 — chunked + multimodal + subject-aware)
onenote_ops.py query "<query>" --v2 [--top-k N] [--max-n M] [--notebook NB]
                                [--subject self,Dad,...] [--include-general]
                                [--no-subject-filter]

# Semantic search (v1 — legacy page-level)
onenote_ops.py query "<query>" [--top-k N] [--notebook NB]

# Cached / non-semantic operations
onenote_ops.py search-title "<title keyword>"     # title grep, no API
onenote_ops.py search-content "<keyword>"         # HTML grep over cached pages
onenote_ops.py read-page <nb> <sec> <page>        # plain-text
onenote_ops.py read-page-html <nb> <sec> <page>
onenote_ops.py list-notebooks | list-sections <nb> | list-pages <nb> <sec>
onenote_ops.py refresh <nb>                       # force re-fetch sections + pages

# Media management (v2 ingest)
onenote_ops.py fetch-media "<page>" [--all] [--pages-file PATH] [--no-derived]
onenote_ops.py render-page "<page>"               # write browser-viewable HTML with local image src
onenote_ops.py gc-media [--dry-run]               # delete orphaned resource bytes
```

---

## Strategy: ingest, index, query

### Ingest pipeline

Runs at `fetch-media`, `refresh`, `classify_subjects.py`, and `build_embeddings.py` time. One-shot per item; outputs are cached on disk and reused forever.

| Step | Input | Tool | Output |
|---|---|---|---|
| 1. Fetch HTML | OneNote page | Graph API | `cache/page_content/<pid>.html` |
| 2. Fetch resource bytes | `<img>` / `<object>` refs in HTML | Graph API `/onenote/resources/{id}/content` | `cache/page_resources/<rid>.<ext>` |
| 3. OCR images | image bytes | **Gemini 2.5 flash** | `<rid>.ocr.txt` (only if ≥30 non-ws chars) |
| 4. Scene-caption empty-OCR images | image bytes | **Gemini 2.5 flash** | `<rid>.caption.txt` |
| 5. Transcribe audio/video | media bytes | **Gemini 2.5 flash** | `<rid>.transcript.txt` |
| 6. Subject classification | page meta + body + OCR | **Gemini 2.5 flash** | `cache/page_subjects.json` (one label per page: `self` / `general` / `<Person>`) |
| 7. Adaptive chunking | HTML | (local, deterministic) | typed chunks per page |
| 8. Embedding | text strings + media bytes | **Gemini `gemini-embedding-2-preview`** @ 768d | `cache/embeddings_v2.npz` + `_meta.json` |

Gemini is chosen for steps 3–6 and 8 for economy: flash runs ingest at ~$0.001/item in batch; Claude via paid API would cost ~10×. Steps 1–2 and 7 involve no LLM.

### Chunking policy (step 7)

Per-page chunks in document order, then embedded individually:

- **Text chunks**: headings (`h1`/`h2`) as *hard* boundaries only when the section has ≥500 chars; otherwise the heading becomes a soft marker embedded inline. Paragraphs with ≥3 sentences AND ≥150 non-ws chars get their own chunk; shorter ones pack up to 1.5K chars. Over-sized paragraphs are sliding-window split with 200-char overlap.
- **Table chunks**: small tables (<1.5K chars) emit one chunk; larger tables produce row-group chunks with the column header re-prefixed. Row-group size adapts to average row body (5–10 rows typical). Cells >1K chars trigger intra-cell windowing with row-context re-prefix.
- **Media chunks** (per resource): one raw-bytes chunk (image/PDF/audio → multimodal embedding) + sibling text chunks when OCR or caption exists.
- **Page-summary chunk**: one per page (`kind=summary`) — whole body capped at 5K chars.
- **Routing header** (prepended to every text chunk): `Notebook / Section / Page / Heading path`. Provides retrieval context beyond the chunk body.

Typical corpus: ~5× chunks per page; ~6K total chunks for ~1.1K pages.

### Index structure

- `cache/embeddings_v2.npz` — `{ids: (N,) str, vectors: (N, 768) float32 L2-normalized, kinds: (N,) str}`.
- `cache/embeddings_v2_meta.json` — `{model, dim, pages: {page_id: {...}}, chunks: {chunk_id: {kind, page_id, heading_path, resource_id, filename, ...}}}`. No embed text stored — retrieved on-demand by re-chunking a hit page (cached per-session).
- `cache/page_subjects.json` — `{page_id: subject_label}`; subject_overrides.json can patch specific labels.

All are plain files; no external vector DB. Size: ~22 MB for 5,977 vectors.

### Query pipeline

```
 query text
  │
  ├─► Gemini embed_content ──► 768-d query vector        (~180 ms)
  │
  ├─► (local) subject auto-detect: first-person pronouns +
  │             person names in query → allowed subjects
  │
  ├─► matmul (vectors @ query)                           (~1 ms)
  │
  ├─► filters: notebook, subject-set, --include-general? (~1 ms)
  │
  ├─► top-K pages × max-N chunks/page                    (~1 ms)
  │
  └─► return hits with chunk_id, subject tag, heading_path
```

- **Subject-aware filtering** is default-on and *strict* (person-only). `--include-general` adds general reference pages alongside; used when the query needs reference material to be answerable (how X works, precautions before X, normal ranges, interpretation). The orchestrating Claude harness decides when to pass the flag based on the query; no Gemini call for this decision.
- **Top-K × max-N** (default 10 × 3): surface up to N matching chunks per page, across K distinct pages. Shows multiple relevant chunks per page when they exist, without one page dominating results.
- **No re-rank**, no ANN index — exact cosine matmul over ~6K vectors takes ~1 ms.

Typical query latency: **~280 ms** steady state (embed call dominates). Local compute is sub-millisecond.

### Synthesis (when invoked via Claude Code)

Retrieval returns chunk IDs; the orchestrating Claude re-chunks each hit page at query time to pull the matched chunks' exact text (`embed_text`), feeds them as context to itself, and writes the answer with page-level citations. Claude is free via the subscription, instant, and avoids the latency and 503-flakiness of calling Gemini flash as a generator.

### Where LLMs are used — summary

| Role | Model | Economic reasoning |
|---|---|---|
| Embedding (build + query) | Gemini `gemini-embedding-2-preview` | Locked to the vector space of the stored index; ~$0.06 for full corpus |
| Ingest OCR / caption / transcribe / classify | Gemini 2.5 flash | Cheapest paid option at batch scale (~$1–2 one-time) |
| Query synthesis (RAG answer) | Claude (harness) | Free via subscription, instant, no 503 |
| Query intent (whether to include general) | Claude (harness) | Free via subscription, accurate without extra API call |

If corpus scale grows 10× or privacy demands on-device: Tesseract (OCR), Whisper (transcribe), and local Ollama (classifier) are viable swaps without touching the embedding path.

---

## What gets cached

The skill caches API responses and derived artifacts locally for speed:

**Graph-fetched source:**
- `cache/onenote_cache.json` — full notebook/section/page index (never read directly — too large)
- `cache/page_index.txt` — grep-able title + path index rebuilt from the above
- `cache/page_content/<safe_pid>.html` + `.meta` — individual page HTML snapshots, invalidated by `last_modified`
- `cache/page_resources/<safe_rid>.<ext>` — raw image / PDF / audio / video bytes fetched from Graph `/onenote/resources/{id}/content`
- `cache/page_resources/<safe_rid>.meta.json` — `{mime, filename, kind, size, fetched_at, page_ids: [...]}` per resource

**LLM-derived artifacts (one-shot per item, reused forever):**
- `cache/page_resources/<safe_rid>.ocr.txt` — Gemini-flash OCR of images (when ≥30 non-ws chars)
- `cache/page_resources/<safe_rid>.caption.txt` — Gemini-flash scene caption (only when OCR empty)
- `cache/page_resources/<safe_rid>.transcript.txt` — Gemini-flash transcript for audio/video
- `cache/page_subjects.json` — per-page classification: `self` / `general` / `<Person>`; used for subject-aware query filtering
- `cache/subject_overrides.json` (optional) — manual overrides; takes precedence

**Embedding index (v2, chunked + multimodal, 768d):**
- `cache/embeddings_v2.npz` — `{ids, vectors, kinds}` — one row per chunk (text / summary / image / image_ocr / image_caption / pdf / audio / video_transcript)
- `cache/embeddings_v2_meta.json` — per-chunk metadata (page_id, heading_path, resource_id, filename, char_count, source) + per-page `last_modified` for incremental rebuilds

**Legacy v1 embeddings** (page-level, text-only, superseded by v2):
- `cache/embeddings.npz` + `cache/embeddings_meta.json` — still built by `build_embeddings.py` without `--v2` flag; safe to delete once v2 is in use

Sync runtime state (also under `cache/`, gitignored):

- `.sync.lock` — `flock`-held mutex; body carries `{pid, started_at, hostname, max_duration_sec}` for `status` / `unstick`
- `.sync.heartbeat` — current step + timestamp, rewritten every 5 s
- `.sync.state.json` — result of the most recent run
- `sync.log` — JSONL, one row per run (timestamp, status, elapsed, page counts, embed counts, any error)
- `sync.launchd.log` — stdout/stderr from the scheduled launchd job (if installed)

All of these are in `.gitignore` and never committed. They rebuild automatically on a new machine after you run `onenote_setup.py` once and then `build_embeddings.py`.
