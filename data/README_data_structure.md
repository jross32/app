# Data folder organization
This folder now uses subfolders to keep backups tidy.

- `league_data_final_week11.json` — active structured data file used by the app.
- `backups/session/` — previous session exports/backups (lifetime_extended, membership, highlights, names).
- `backups/raw/` — raw API dumps (`apa_raw_latest*.json`).
- `apa_patch_index.json`, indexes, and other helper JSONs remain at the root for quick access.

When changing data/schema:
- Update `config.json` (data_file) and `data/api_dump_v2_mapping.md` with any new fields.
- Place new raw dumps in `backups/raw/` and processed backups in `backups/session/`.
