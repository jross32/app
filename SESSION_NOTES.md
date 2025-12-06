## Session Notes (most recent)

Repo: https://github.com/jross32/app  
Run helper: `run.ps1` (PowerShell) — activates `.venv` if present, sets `FLASK_APP=app.py`, `FLASK_ENV=development`, runs `flask run` on 127.0.0.1:5000. If flask not found, run inside venv: `.\.venv\Scripts\python -m flask run`.

### Recent Changes (chronological highlights)
- Added “Push to GitHub” button in admin (Scripts/Automation section). New route `/admin/git/push` runs `git status --short` then `git push`, returns logs as JSON. (Files: `app.py`, `templates/admin.html`)
- Added `merge_dump_to_structured.py` to transform the latest `api_dump_v2.json` into structured data and write to `data/backups/session/league_data_from_dump.json` (players only: membership, sessions, lifetime stats, highlights). Promoted the merged file to live `data/league_data_final_week11.json`, backed up prior live file to `data/backups/session/league_data_final_week11.json.bak`.
- Synced capture scripts between capture_v1 and capture_v2; capture_v1 now filters URL discovery by allowed paths (`/member/, /team/, /standings, /schedule, /match`) and API sniffer by hosts/paths (defaults: gql.poolplayers.com, /graphql).
- Profile lifetime cards: improved fallbacks and formatting; win% computed from wins/matches if missing, ppm alias included, CLA/defensive avg pulled from lifetime_extended when missing in main. Note: Justin Ross 9-ball CLA is 0 in data; not derivable from dump.
- Profile Edit modal: added APA player link dropdown (top of modal) showing only player name; links profile to player_ref (applies to both 8/9 ball stats).
- Past Seasons highlights: aggregated session/format/stat with counts; filters for format/achievement; counts show once.
- Music card: unified styling, embed shell for SoundCloud/YouTube/audio; fallbacks intact.
- Git repo initialized, committed, pushed to `https://github.com/jross32/app`. `.gitignore` added to exclude venvs, backups, captures, uploads.

### Large Files / Parts
- GitHub 100MB limit: big dumps were split into 50MB chunks (.partNN) and originals removed.
- Recombine with:
  ```
  python scripts/join_parts.py "data/backups/raw/apa_raw_latest.json"
  python scripts/join_parts.py "data/backups/raw/apa_raw_latest_2025-12-04_12-41-50.json"
  python scripts/join_parts.py "data/apa_api_captures/2025-12-01_14-46-33/api_dump_v2.json"
  ```
  (Looks for .part files in the same folder and concatenates in order.)

### Key Data/Mappings
- Active data file: `data/league_data_final_week11.json` (set in `config.json`).
- Mapping guide: `data/api_dump_v2_mapping.md`.
- Backup structure: `data/backups/raw/` (raw dumps), `data/backups/session/` (structured backups, including `league_data_from_dump.json`).
- Reference docs: see `data/README_for_future_assistants.md` for list of PDFs under `APA RULES & RESOURCES` and `UNDERSTANDING DUMP.json FILE` (covers API schema, Equalizer, CLA, patch rules).

### How to Run
```powershell
cd C:\Users\dmjr2\app\apa\apa-team-manager
powershell -ExecutionPolicy Bypass -File .\run.ps1
```
(If no venv: `python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt`.)

### Known Data Notes
- Justin Ross: lifetime_stats['9-ball'] is None; lifetime_extended['9-ball'] shows CLA: 0. Dump (api_dump_v2.json) contains no CLA for his 9-ball; not derivable from wins/pa.
- CLA is an internal “Current Lifetime Average”; only use values present in dumps; do not compute ad hoc.

### Next Steps (if needed)
- If you want CLA hidden when 0/missing, adjust lifetime card UI to show “—” instead of 0.0.
- To refresh data from a new dump: place dump in `scripts/scraper_sniffer/capture_v2/apa_api_captures/<run>/api_dump_v2.json`, run `merge_dump_to_structured.py`, review `data/backups/session/league_data_from_dump.json`, then swap into live after backing up.
