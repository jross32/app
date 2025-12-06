## Working Notes for Future Assistants

Keep this project consistent and avoid regressions. Update this file when workflows or data shapes change.

### Key Data Files
- Active structured data: `data/league_data_final_week11.json` (also set in `config.json` as `data_file`).
- Mapping guide for raw dump → structured JSON: `data/api_dump_v2_mapping.md`.
- Backups:
  - `data/backups/raw/` for raw dumps (`apa_raw_latest*.json`).
  - `data/backups/session/` for structured backups (e.g., `league_data_final_week11.bak.*.json`).

### Reference Docs (read these before changing schema/rules)
- `data/UNDERSTANDING DUMP.json FILE/`:
  - `Understanding the API Dump (api_dump_v2.json).pdf`
  - `Structured Data Overview.pdf`
  - `api_dump APA League Data JSON Structure Analysis.pdf`
  - `APA Member vs. Alias Understanding Player IDs_APA League Data Structure and Stats Explained.pdf`
  - `Deriving the APA (Equalizer) Cover-Points Formula.pdf`
- `data/APA RULES & RESOURCES/`:
  - `Deep Dive into APAs Behind-the-Scenes Data and Handicapping System.pdf`
  - `APA Patch Program - Comprehensive Guide.pdf`
  - `team-manual-english.pdf`
  - `785_bylaws_2025.pdf.pdf`

Use these to understand IDs (member vs alias), JSON shapes, Equalizer math, and patch rules before altering data ingestion or UI assumptions.

### When Updating Data
1. Place new raw dumps in `data/backups/raw/`.
2. Process/merge into the structured file (match current schema).
3. Update `config.json` if the data filename changes and sync `api_dump_v2_mapping.md`.
4. Keep at least one backup copy in `data/backups/session/` before overwriting the active file.

### Schema & Mapping
- Follow `api_dump_v2_mapping.md` for how `api_dump_v2.json` fields map into structured JSON.
- Add new stats/fields by:
  - Per-session → `players[alias_id].sessions[format]`
  - Lifetime → `players[alias_id].lifetime_stats[format]` (and mirror into `lifetime_extended` if extended)
  - Achievements → aggregate into `session_highlights`
- Always format missing fields as “—” in UI, not raw `None/0` unless the value is truly zero.

### Common UI Patterns
- Music card: keep embed/audio/fallback within the same card; dark theme, neon accents. SoundCloud can use standard embed; if styling shifts, adjust the embed shell, not backend logic.
+- Past Seasons: highlights are aggregated (session + format + stat). Avoid repeating raw events.
+- Lifetime cards: two balanced tiles (8-ball/9-ball) with win%, matches/W/L/SL, PPM/PA, CLA/Def Avg, last played, last 2 yrs.
+- Match history modal: triggered by current-season cards or stat tiles; keep sort/filter intact.

### Roles & Access
- `is_staff`/admin can access Coach/Planner routes; do not expose to non-staff.
- Session lifetime: default 6h; “remember” extends to ~14 days.

### Don’ts
- Don’t hard-delete backups or raw dumps.
- Don’t change schema without updating mapping doc and config.
- Don’t leave embed iframes unframed—use the music card shell.

### Quick Pointers
- Nav/location for music card styling: `templates/profile_detail.html`.
- Data constants: `app.py` (`LEAGUE_DATA_FILENAME`) and `config.json`.
- Index helpers live in `data/*index*.json`; keep them in sync if data shape changes.

Keep changes additive, respect the dark/neon design language, and ensure mobile responsiveness for profile components and modals.***
