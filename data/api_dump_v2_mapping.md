## APA API Dump v2 → Structured League JSON Mapping

This guide explains how to interpret `api_dump_v2.json` (raw capture) and map it into our structured league data (`league_data_final_week11.json`, `LEAGUE_DATA_FILENAME`). Update this file whenever the dump schema or our structured JSON changes.

### Core Objects in `api_dump_v2.json`

Each entry is a network capture with `page_url`, `api_url`, `method`, `status`, `request_body`, and `response_body` (usually GraphQL).

Important GraphQL shapes:
- **Alias / Member**  
  `alias(id) { id, displayName, league { id, slug, currentSessionId }, players { ... }, sessions { id, name }, member { id, consecutiveYearsPlayed, membershipHistory { year, leaguePaidIn { id, name, salutation } } } }`
- **Eight-ball stats**: `eightBallBreakAndRuns`, `eightOnBreaks`, `rackless`, `miniSlams`, `eightBallMatchPointsEarned`, `pa`, `ppm`, `matchesPlayed`, `matchesWon`
- **Nine-ball stats**: `nineBallBreakAndRuns`, `nineOnSnaps`, `miniSlams`, `skunks`, `nineBallMatchPointsEarned`, `pa`, `ppm`, `matchesPlayed`, `matchesWon`
- **Lifetime blocks**: `EightBallStats: stats(filter: EIGHT) { ...EightBallLifetimeStatistics... }` and `NineBallStats: stats(filter: NINE) { ...NineBallLifetimeStatistics... }` containing `matchesPlayed`, `matchesWon`, `CLA`, `defensiveShotAvg`, `matchCountForLastTwoYrs`, `lastPlayed`
- **Sessions list per alias**: `alias(id){ sessions(format: EIGHT|NINE) { id, name } }`
- **Team context**: `players { team { id, name, number, active } }` for session-specific players.

### Target Structured JSON (league_data_final_week11.json)

Top-level keys:
- `meta`: league/session metadata.
- `divisions`: keyed by division_id; includes `type/format`, `code`, `session`, `night`, `standings`.
- `teams`: keyed by internal team id; includes `format`, `division_id`, `home_location(_id)`, `roster` (player_ids), `session_summary`, optional `session_stats` / `patch_counts`.
- `players`: keyed by player_id; includes:
  - `full_name`, `short_name`, `apa_id` (alias id), `member_id`
  - `current_skill_levels` (by format)
  - `sessions` (per format) for season stats
  - `lifetime_stats` (by format)
  - `lifetime_extended` (by format)
  - `membership_years`, `membership_leagues`, `consecutive_years_played`
  - `session_highlights` (aggregated patches per session/format)
- `matches`: keyed by match_id; team vs team, with `sets` describing player vs player.
- `patch_index`: catalog of patch definitions.

### Field-by-Field Mapping Guide

**Players**
- `players[alias_id].full_name` ← `alias.displayName`
- `players[alias_id].apa_id` ← `alias.id`
- `players[alias_id].member_id` ← `alias.member.id`
- `players[alias_id].membership_years` ← `alias.member.membershipHistory[].year`
- `players[alias_id].membership_leagues` ← `alias.member.membershipHistory[].leaguePaidIn {id,name,salutation}`
- `players[alias_id].consecutive_years_played` ← `alias.member.consecutiveYearsPlayed`

**Per-format session stats (current or per session)**
- Source: `alias.players` (EightBallPlayer / NineBallPlayer), often scoped by `session`.
- Map to `players[alias_id].sessions[format]`:
  - `points_per_match` ← `ppm`
  - `percent_points_avail` ← `pa` (ratio; format as pct for display)
  - `matches_played` ← `matchesPlayed`
  - `matches_won` ← `matchesWon`
  - `eightOnBreaks` / `nineOnSnaps`
  - `eightBallBreakAndRuns` / `nineBallBreakAndRuns`
  - `rackless` / `skunks`
  - `miniSlams`
  - `team` info (team.id, team.name, number) for that session

**Lifetime stats**
- Source: `EightBallStats: stats(filter: EIGHT)` → `EightBallLifetimeStatistics`
- Map to `players[alias_id].lifetime_stats['8-ball']`:
  - `matchesPlayed`, `matchesWon`, `CLA`, `defensiveShotAvg`, `matchCountForLastTwoYrs`, `lastPlayed`
- Same for `NineBallStats` → `lifetime_stats['9-ball']`
- Keep `lastPlayed` as ISO string; format on render.

**Lifetime extended (internal helper)**
- Copy lifetime fields into `lifetime_extended[format]` for UI convenience:
  - `CLA`, `defensiveShotAvg`, `matchCountForLastTwoYrs`, `lastPlayed`, plus mirrored matches/wins/losses.

**Current skill levels**
- Source: `alias.players` (or derive from team roster/sets); store as `current_skill_levels[format] = sl`.

**Session highlights / patch-like counts**
- For each session stats block, sum:
  - 8B: `eightBallBreakAndRuns`, `eightOnBreaks`, `rackless`, `miniSlams`
  - 9B: `nineBallBreakAndRuns`, `nineOnSnaps`, `skunks`, `miniSlams`
- Aggregate by `(session_name or session_id, format, stat_label)` into `session_highlights`.

**Sessions list (for dropdowns/history)**
- Source: `alias.sessions(format: …) { id, name }`
- Store under `players[alias_id].session_list[format] = [ {id,name} ]`

**Teams / Divisions**
- Use team objects from match data or roster lookups; do not invent teams solely from alias players.
- Division info comes from `division_index.json`/schedule; dump may include APA division codes; map `division.code` / `division.type` (“8-ball”/“9-ball”) to teams.

**Matches**
- Dump may contain `matches`/`sets`; our structured file already has matches built. If regenerating, transform `sets` into per-player stat lines and link `home_team_id`/`away_team_id`, `week/date`.

### Aggregation Rules Used in the App

- **Session highlights**: aggregated by (season, format, stat). Display once per combo with counts (e.g., “Summer 2025 • 9-BALL • 9-On-The-Snap ×7”).
- **Lifetime cards**: per format; show win%, matches/w/l, SL, PPM/PA, CLA, Def Avg, last played, last 2 yrs.
- **Membership**: years sorted desc; leagues listed uniquely; consecutive years displayed if provided.

### Adding New Fields

When a new stat appears in `api_dump_v2`:
1) Find it in the GraphQL response (usually under alias or player).  
2) Decide scope: per-session → `sessions[format]`; lifetime → `lifetime_stats[format]` (and mirror to `lifetime_extended` if extended); achievement → fold into `session_highlights`.  
3) Update UI to show “—” when missing instead of raw `None/0` if absent.

### File Pointers

- Raw capture: `scripts/scraper_sniffer/capture_v2/apa_api_captures/<timestamp>/api_dump_v2.json`
- Structured data in use: `data/league_data_final_week11.json` (`LEAGUE_DATA_FILENAME`)
- Patch catalog: `data/apa_patch_index.json`

Keep this document updated when the dump schema changes or when we add new derived fields in the structured JSON.
