"""
Merge decoded matches into a copy of the league data (non-destructive).

Inputs:
- --league defaults to data/league_data_final_week11.json
- --matches defaults to data/decode/match_extract.json

Output:
- data/merge/league_with_matches.json (copy of league data with added matches)
- summary printed to stdout

Mapping strategy (best-effort):
- Normalize team names to match league teams by name only.
- Only adds matches where both home and away teams map to known league teams.
- Existing matches are left untouched (only adds new ones).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def normalize_name(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in str(name)).strip("_")

# Manual APA id -> league team id overrides
APA_TEAM_OVERRIDES = {
    # Cue-ligans
    "12867592": "cue_ligans_8",
    "12867593": "cue_ligans_9",
    # Ballerz 956
    "12864716": "ballerz_956_8",
    "12864636": "ballerz_956_9",
}

# Manual name -> league team id overrides (normalized name)
NAME_OVERRIDES = {
    "cue_ligans": ["cue_ligans_8", "cue_ligans_9"],
    "ballerz_956": ["ballerz_956_8", "ballerz_956_9"],
    "ballerz56": ["ballerz_956_8", "ballerz_956_9"],
}


def main():
    parser = argparse.ArgumentParser(description="Merge decoded matches into league data (copy-only).")
    parser.add_argument("--league", default="data/league_data_final_week11.json")
    parser.add_argument("--matches", default="data/decode/match_extract.json")
    parser.add_argument("--out", default="data/merge/league_with_matches.json")
    args = parser.parse_args()

    league_path = Path(args.league)
    matches_path = Path(args.matches)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not league_path.exists():
        raise SystemExit(f"League file not found: {league_path}")
    if not matches_path.exists():
        raise SystemExit(f"Decoded matches not found: {matches_path}")

    league = json.load(league_path.open("r", encoding="utf-8"))
    decoded = json.load(matches_path.open("r", encoding="utf-8"))

    teams = league.get("teams", {}) or {}
    team_by_norm = {}
    team_by_idnum = {}  # map APA numeric id/number -> team_id if present
    for tid, t in teams.items():
        name = t.get("name") or t.get("team_name") or tid
        norm = normalize_name(name)
        team_by_norm.setdefault(norm, []).append(tid)
        # optional: store APA numeric ids if available
        apa_id = t.get("apa_team_id") or t.get("id") or t.get("team_id")
        if apa_id is not None:
            team_by_idnum[str(apa_id)] = tid
        # store team number if present (e.g., '02207')
        num = t.get("number")
        if num:
            team_by_idnum[str(num)] = tid
        # derive numeric id from url if present
        url = t.get("url") or ""
        tail = url.rstrip("/").split("/")[-1]
        if tail.isdigit():
            team_by_idnum[tail] = tid

    existing_matches = league.get("matches", {}) or {}
    added = 0
    skipped = 0

    for m in decoded:
        home = m.get("home_team") or {}
        away = m.get("away_team") or {}
        hname = home.get("name") or home.get("team_name")
        aname = away.get("name") or away.get("team_name")
        if not hname or not aname:
            skipped += 1
            continue
        hnorm = normalize_name(hname)
        anorm = normalize_name(aname)
        hids = []
        aids = []
        # try APA ids first if present
        hid_num = home.get("team_id") or home.get("id") or home.get("teamId") or home.get("number")
        aid_num = away.get("team_id") or away.get("id") or away.get("teamId") or away.get("number")
        if hid_num is not None and str(hid_num) in team_by_idnum:
            hids.append(team_by_idnum[str(hid_num)])
        if aid_num is not None and str(aid_num) in team_by_idnum:
            aids.append(team_by_idnum[str(aid_num)])
        # fallback to name matching
        if not hids:
            hids = team_by_norm.get(hnorm, [])
        if not aids:
            aids = team_by_norm.get(anorm, [])
        # apply manual overrides if still ambiguous
        if not hids and hnorm in NAME_OVERRIDES:
            hids = NAME_OVERRIDES[hnorm]
        if not aids and anorm in NAME_OVERRIDES:
            aids = NAME_OVERRIDES[anorm]
        # apply manual APA id overrides
        if not hids and hid_num is not None and str(hid_num) in APA_TEAM_OVERRIDES:
            hids = [APA_TEAM_OVERRIDES[str(hid_num)]]
        if not aids and aid_num is not None and str(aid_num) in APA_TEAM_OVERRIDES:
            aids = [APA_TEAM_OVERRIDES[str(aid_num)]]
        def pick_candidate(cands, number_hint):
            if not cands:
                return None
            if len(cands) == 1:
                return cands[0]
            # Heuristic: if number_hint looks like '02...' => 8-ball, '22...' => 9-ball
            if number_hint:
                num_str = str(number_hint)
                if num_str.startswith('02'):
                    for c in cands:
                        if c.endswith('_8'):
                            return c
                if num_str.startswith('22'):
                    for c in cands:
                        if c.endswith('_9'):
                            return c
            # else pick the first deterministically
            return cands[0]

        home_id = pick_candidate(hids, hid_num if 'hid_num' in locals() else None)
        away_id = pick_candidate(aids, aid_num if 'aid_num' in locals() else None)
        if not home_id or not away_id:
            skipped += 1
            continue
        apa_id = m.get("match_id")
        key = f"apa_{apa_id}" if apa_id is not None else f"new_{len(existing_matches)+added}"
        if key in existing_matches:
            continue
        entry = {
            "id": key,
            "apa_match_id": apa_id,
            "date": m.get("date"),
            "division_id": None,
            "format": None,
            "location": None,
            "home_team_id": home_id,
            "away_team_id": away_id,
            "team_scores": {
                "home_subtotal": m.get("home_score"),
                "away_subtotal": m.get("away_score"),
                "home_bonus": None,
                "away_bonus": None,
                "home_total": m.get("home_score"),
                "away_total": m.get("away_score"),
            },
            "sets": [],
            "status": m.get("status"),
        }
        existing_matches[key] = entry
        added += 1

    league["matches"] = existing_matches
    out_path.write_text(json.dumps(league, indent=2), encoding="utf-8")
    print(f"Matches added: {added}, skipped: {skipped}")
    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    main()
