"""
Utility to trim the large structured APA dump to only the content the app needs.

It:
- Reads dump_structured_v2.json (or a specified source).
- Keeps only divisions that include the Cue-ligans team (and their opponents).
- Keeps teams in those divisions, their rosters, and the referenced players.
- Keeps matches tied to those divisions/teams.
- Writes a smaller apa_data_trimmed.json for the Flask app.
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent
# Use the structured dump already copied to data root (smaller than the raw API parts)
SRC = DATA_DIR / "apa_data_2025-12-03_update1.json"
DEST = DATA_DIR / "apa_data_trimmed.json"


def load_json_lenient(path: Path):
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except Exception:
        idx = raw.find("{")
        if idx != -1:
            return json.loads(raw[idx:])
        raise


def main():
    data = load_json_lenient(SRC)
    divisions = data.get("divisions") or {}
    teams = data.get("teams") or {}
    players = data.get("players") or {}
    matches = data.get("matches") or {}

    # Identify Cue-ligans team IDs
    cue_team_ids = set()
    for tid, t in teams.items():
        name = (t.get("name") or "").lower()
        if "cue-ligan" in name or "cue_ligan" in name:
            cue_team_ids.add(tid)

    if not cue_team_ids:
        print("Cue-ligans team not found; nothing to trim.")
        return

    # Divisions that include Cue-ligans
    selected_divs = set()
    for tid in cue_team_ids:
        div_id = teams.get(tid, {}).get("division_id")
        if div_id:
            selected_divs.add(div_id)

    # Keep all teams in the selected divisions
    selected_teams = set()
    for tid, t in teams.items():
        if t.get("division_id") in selected_divs:
            selected_teams.add(tid)

    # Players referenced by selected teams
    selected_players = set()
    for tid in selected_teams:
        roster = teams.get(tid, {}).get("roster") or []
        for ref in roster:
            if isinstance(ref, dict):
                pid = ref.get("player_id") or ref.get("id")
            else:
                pid = ref
            if pid:
                selected_players.add(pid)

    # Matches in selected divisions or involving selected teams
    selected_matches = {}
    selected_locations = set()
    for mid, m in matches.items():
        if m.get("division_id") in selected_divs or m.get("home_team_id") in selected_teams or m.get("away_team_id") in selected_teams:
            selected_matches[mid] = m
            # pull in players in sets just in case
            for s in m.get("sets", []) or []:
                for key in ("home_player_id", "away_player_id"):
                    pid = s.get(key)
                    if pid:
                        selected_players.add(pid)
            loc_id = m.get("location_id")
            if loc_id:
                selected_locations.add(str(loc_id))

    # Locations referenced by matches/teams
    for t in selected_teams:
        loc_id = teams.get(t, {}).get("home_location_id")
        if loc_id:
            selected_locations.add(str(loc_id))

    trimmed = {
        "meta": data.get("meta", {}),
        "league": data.get("league", {}),
        "divisions": {k: v for k, v in divisions.items() if k in selected_divs},
        "teams": {k: v for k, v in teams.items() if k in selected_teams},
        "players": {k: v for k, v in players.items() if k in selected_players},
        "matches": selected_matches,
        "locations": {k: v for k, v in (data.get("locations") or {}).items() if str(k) in selected_locations},
        # omit features/operations to keep size small
    }

    DEST.write_text(json.dumps(trimmed, separators=(",", ":")), encoding="utf-8")
    print(f"Trimmed data written to {DEST} with {len(selected_divs)} divisions, {len(selected_teams)} teams, {len(selected_players)} players, {len(selected_matches)} matches, {len(selected_locations)} locations.")


if __name__ == "__main__":
    main()
