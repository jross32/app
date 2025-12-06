## Cue-ligans data notes (for developers)

### Primary dataset
- **apa_data_2025-12-03_update1.json** (root keys: `meta`, `viewer`, `league`, `divisions`, `teams`, `players`, `matches`, `locations`, `features`, `operations`). Note: the file begins with a stray leading digit (`2`); loaders should trim before JSON parse.
- Counts: 2 divisions, 8 teams, 142 players, 1 (placeholder) match.
- Teams: Dead On, Chalk and Awe, Cue-ligans, Ballerz 956, Bad Boys, Sour Patch Kids, Palochones, Quete Importa. `format` is null; infer from `apa_meta.raw_team.division.type` (`EIGHT`/`NINE`) or alternate when absent.
- Players: stats per format live under `players.*.stats` with `raw.__typename` hints (`EightBallPlayer`, `NineBallPlayer`). Many are keyed as `unknown`, so format inference must inspect `raw.__typename`, key names, and `current_skill_levels`. Some players (e.g., Arturo Rivera, Ivan Sanchez) only show one format via `unknown.raw.__typename`.
- Matches: only one entry with mostly empty fields—no usable scores/dates in this drop.

### Supporting captures (for traceability)
- `apa_api_captures/2025-12-01_14-46-33/` contains raw and structured API dumps (`api_dump_v2*.json`, `dump_structured_v2*.json`) plus helper scripts for splitting/renaming.
- `apa_url_discovery/2025-12-01_07-49-45/` contains discovered URLs (`all_urls.txt`, `url_graph.json`) and hundreds of screenshots that show how APA pages map team IDs, member IDs, and formats.

### Reference PDFs
- APA rules/bylaws: `785_bylaws_2025.pdf.pdf`, `apa_rulebook.txt`, `team-manual-english.pdf`.
- Data/handicap explainers: `APA Member vs. Alias Understanding Player IDs_APA League Data Structure and Stats Explained.pdf`, `Deep Dive into APA’s “Behind-the-Scenes” Data and Handicapping System.pdf`.
- Patch program reference: `APA Patch Program – Comprehensive Guide.pdf`.

### Known gaps in the new JSON
- Team formats and division formats are missing/null; must be inferred.
- Matches block lacks real schedules, scores, or sets.
- Player stats often only under `stats.unknown` without explicit format keys.
- Roster split per format is not explicit (Cue-ligans appears once; per-player stats determine 8- vs 9-ball eligibility).

### Helpful additional data to add (if available)
- Real match schedule/results (with dates, home/away, team scores, per-set points).
- Per-player match history (opponent, SL, points, result, date, format).
- Explicit team `format` and division `format/type` fields.
- Clear roster declarations per format (or per-player flags `plays_8`, `plays_9`).
- APA member IDs/URLs for every player to avoid name ambiguity.

### Quick decoding tips for future work
- If `stats` has only `unknown`, use `stats.unknown.raw.__typename` to decide format (`EightBallPlayer` → 8-ball, `NineBallPlayer` → 9-ball).
- If no format can be inferred for a player, fall back cautiously to both formats; prefer per-player evidence over division guesses.
- Cue-ligans in this drop is tagged NINE in `apa_meta.raw_team.division.type`; no separate 8-ball team/stats are present for Cue-ligans in this file.
