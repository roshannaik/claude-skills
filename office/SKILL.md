---
name: office
description: Read and write Excel, Word, and PowerPoint files on Roshan's OneDrive via Microsoft Graph API. Use when asked to read/update spreadsheets, documents, or presentations stored on OneDrive.
argument-hint: 'read budget.xlsx, list sheets in report.xlsx, read slides in deck.pptx'
allowed-tools: Bash, Read, Write, Edit
author: clawdi
---

# Office Files Skill (Excel / Word / PowerPoint)

## Setup

- Script: `~/.claude/skills/office/office_ops.py`
- Auth: same token as OneNote — `~/.cache/ms_graph_token_cache.json` (no login needed)
- No extra Azure permissions needed — `Files.Read` / `Files.ReadWrite` cover all Office formats

## Finding Files

```bash
# Search OneDrive for a file by name
python3 ~/.claude/skills/office/office_ops.py search "budget" --type xlsx

# List files in a folder
python3 ~/.claude/skills/office/office_ops.py list-folder "Documents"
python3 ~/.claude/skills/office/office_ops.py list-folder /
```

Both return `name | file_id | url`. Use `--file-id` or `--file-path` in subsequent commands.

## Excel

```bash
# List worksheets
python3 ~/.claude/skills/office/office_ops.py excel-sheets --file-path "Documents/budget.xlsx"

# Read entire used range of a sheet
python3 ~/.claude/skills/office/office_ops.py excel-used --file-path "Documents/budget.xlsx" "Sheet1"

# Read specific range
python3 ~/.claude/skills/office/office_ops.py excel-read --file-path "Documents/budget.xlsx" "Sheet1" "A1:D20"
```

**Write (inline Python):**
```python
import sys; sys.path.insert(0, str(__import__('pathlib').Path.home() / '.claude/skills/office'))
from office_ops import get_file_id, excel_write_range
fid = get_file_id('Documents/budget.xlsx')
excel_write_range(fid, 'Sheet1', 'A1:B2', [['Name', 'Value'], ['Total', 42]])
```

**Read multiple sheets in parallel (inline Python):**
```python
import sys; sys.path.insert(0, str(__import__('pathlib').Path.home() / '.claude/skills/office'))
from office_ops import get_file_id, excel_used_range_batch
fid = get_file_id('Documents/budget.xlsx')
results = excel_used_range_batch(fid, ['Sheet1', 'Sheet2', 'Sheet3'], drive_id='58B31B88585CA325')
# results = {'Sheet1': [[...rows...]], 'Sheet2': [...], 'Sheet3': [...]}
```

## Word

```bash
# Read full text of a .docx
python3 ~/.claude/skills/office/office_ops.py word-read --file-path "Documents/report.docx"
```

**Append content (inline Python):**
```python
import sys; sys.path.insert(0, str(__import__('pathlib').Path.home() / '.claude/skills/office'))
from office_ops import get_file_id, word_append
fid = get_file_id('Documents/report.docx')
word_append(fid, ['New paragraph here.'], heading='New Section')
```

## PowerPoint

```bash
# Read all slides as text
python3 ~/.claude/skills/office/office_ops.py pptx-read --file-path "Documents/deck.pptx"
```

## Excel Tab Cache (AUTO — do this every time)

Whenever you open an Excel file:
1. Check `~/.claude/skills/office/references/excel/` for a cached `.json` file for that `file_id`
2. If cache exists and fresh, load tabs — no API call needed
3. If stale or missing, rebuild (see below)

```python
import sys; sys.path.insert(0, str(__import__('pathlib').Path.home() / '.claude/skills/office'))
from office_ops import (load_excel_cache, save_excel_cache, is_excel_cache_stale,
                         rebuild_excel_cache, excel_used_range)

# 1. Check freshness (1 lightweight Graph call)
stale, current_mod = is_excel_cache_stale(file_id, drive_id=drive_id)

if not stale:
    cache = load_excel_cache(file_id)
    tabs       = cache['tabs']        # named tab descriptions
    tab_groups = cache.get('tab_groups', {})  # grouped tabs (e.g. monthly sheets)
else:
    # Rebuild — carries forward descriptions for unchanged/renamed sheets
    tabs = rebuild_excel_cache(file_id, filename, drive_id=drive_id, file_modified=current_mod)
    # Fill in descriptions for any new sheets (tabs[name] == '' means new)
    for sheet, desc in tabs.items():
        if not desc:
            rows = excel_used_range(file_id, sheet, drive_id=drive_id, max_rows=25)
            tabs[sheet] = "<write section-aware description — see format below>"
    save_excel_cache(file_id, filename, tabs, file_modified=current_mod)
```

### Cache schema

```json
{
  "file_id": "...", "filename": "...", "file_modified": "...", "sheet_ids": {...},
  "tab_groups": {
    "monthly_expense": {
      "pattern": "^(Jan|Feb|...) \\d{4}$",
      "range": "Jan 2009 – Oct 2018",
      "count": 115,
      "desc": "Row 1 = category totals. Categories: Eating Out | Shopping | ..."
    }
  },
  "tabs": {
    "Simple tab":  "Single-line description (A1:Z∞): col headers or key contents.",
    "Dashboard tab": [
      {"section": "Section name (A1:F10)", "range": "A1:F10", "desc": "What's here."},
      {"section": "Another section (A11:F30)", "range": "A11:F30", "desc": "What's here."}
    ]
  }
}
```

**Tab description format rules:**
- `tab_groups`: covers structurally identical tabs (e.g. monthly sheets) — no individual entry needed in `tabs`
- Simple/single-section tabs → string: `"Description (A1:Z∞): contents"`
- Multi-section or dashboard tabs → list of `{section, range, desc}` dicts
- Ranges in descriptions enable direct `excel-read` targeting without scanning
- When writing new descriptions, read 20-25 rows to find all sections before writing

**Using the cache to answer queries:**
- Check `tab_groups` first — if query matches a group pattern, go to that tab directly
- For named tabs: scan `tabs` values for matching sections/topics, then use the `range` to read only that cell range
- For section-list tabs: pick the right section's `range` and pass it to `excel-read` directly

Staleness check is a single lightweight Graph metadata call.
Renames are detected via stable worksheet IDs — descriptions carry forward automatically.

## Excel CLI Options

```bash
# Limit rows returned (large sheets)
python3 ~/.claude/skills/office/office_ops.py excel-used --file-id "ID" "Sheet1" --max-rows 20

# Word — truncate long docs (default 8000 chars)
python3 ~/.claude/skills/office/office_ops.py word-read --file-path "doc.docx" --max-chars 4000
python3 ~/.claude/skills/office/office_ops.py word-read --file-path "doc.docx" --full

# PowerPoint — limit slides (default 20)
python3 ~/.claude/skills/office/office_ops.py pptx-read --file-path "deck.pptx" --max-slides 10
python3 ~/.claude/skills/office/office_ops.py pptx-read --file-path "deck.pptx" --full
```

## Key Notes

- **Always check the Excel cache before any API call** — cache lives in `~/.claude/skills/office/references/excel/`
- For shared files (Roshan's OneDrive), always pass `drive_id='58B31B88585CA325'`
- Excel reads use a persistent workbook session (fast after first call ~0.5s vs ~3.5s cold)
- Excel writes use `persistChanges=True` session — changes are committed to the file
- Word/PowerPoint writes download, modify locally, then re-upload
- Always `search` by filename if you don't know the exact path
