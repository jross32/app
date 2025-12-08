"""
Decode api_dump_v2.json into smaller, usable extracts.

Outputs (into data/decode/):
- alias_stats_extract.json : alias_id -> { "8-ball": {...}, "9-ball": {...} }
- match_extract.json       : list of match-ish records (id, teams, scores, date, typename)
- teams_extract.json       : list of team-ish records (id, name, division, format, rank, points, typename)
- typenames_count.json     : counter of __typename occurrences

Usage:
    python scripts/scraper_sniffer/decode_dump.py \
        --dump data/apa_api_captures/<run>/api_dump_v2.json

If --dump is omitted, the script picks the latest api_dump_v2.json under data/apa_api_captures.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parents[2]  # repo root
DATA_DIR = BASE_DIR / "data"
CAPTURE_ROOT = DATA_DIR / "apa_api_captures"
OUT_DIR = DATA_DIR / "decode"

STAT_KEYS = [
    "matchesPlayed",
    "matchesWon",
    "matchCountForLastTwoYrs",
    "defensiveShotAvg",
    "CLA",
    "lastPlayed",
    "win_pct",
    "percent_points_avail",
    "pa",
    "ppm",
    "points_per_match",
    "skill_level",
]

FMT_MAP = {
    "EightBallPlayer": "8-ball",
    "EightBallLifetimeStatistics": "8-ball",
    "EightBallStats": "8-ball",
    "NineBallPlayer": "9-ball",
    "NineBallLifetimeStatistics": "9-ball",
    "NineBallStats": "9-ball",
}


def find_latest_dump() -> Path | None:
    if not CAPTURE_ROOT.exists():
        return None
    candidates = list(CAPTURE_ROOT.rglob("api_dump_v2.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def harvest(dest: Dict[str, Any], stat: Dict[str, Any]) -> None:
    for key in STAT_KEYS:
        if key in stat:
            dest[key] = stat[key]


def parse_response_body(response_body: Any) -> Any:
    if isinstance(response_body, str):
        try:
            return json.loads(response_body)
        except json.JSONDecodeError:
            return []
    return response_body


def walk(
    node: Any,
    alias_stats: Dict[Any, Dict[str, Dict[str, Any]]],
    current_alias=None,
    typenames: Counter | None = None,
    teams=None,
    sessions=None,
    leagues=None,
    matches=None,
) -> None:
    if isinstance(node, dict):
        if typenames is not None and "__typename" in node:
            typenames[node["__typename"]] += 1
        t = node.get("__typename")
        # collect known entities
        if teams is not None and t == "Team":
            t_obj = extract_team(node)
            if t_obj:
                teams.append(t_obj)
        if sessions is not None and t == "Session":
            sessions.append(node)
        if leagues is not None and t == "League":
            leagues.append(node)
        if matches is not None:
            m_obj = extract_matches(node)
            if m_obj:
                matches.append(m_obj)

        if t == "Alias" and "id" in node:
            current_alias = node["id"]
            for key, fmt in (("EightBallStats", "8-ball"), ("NineBallStats", "9-ball")):
                for stat in node.get(key) or []:
                    harvest(alias_stats[current_alias][fmt], stat)
            for pl in node.get("players") or []:
                walk(pl, alias_stats, current_alias, typenames, teams, sessions, leagues, matches)
        elif t in FMT_MAP and "id" in node:
            fmt = FMT_MAP[t]
            harvest(alias_stats[current_alias or node["id"]][fmt], node)
        for v in node.values():
            walk(v, alias_stats, current_alias, typenames, teams, sessions, leagues, matches)
    elif isinstance(node, list):
        for item in node:
            walk(item, alias_stats, current_alias, typenames, teams, sessions, leagues, matches)


def extract_matches(obj: Dict[str, Any]) -> Dict[str, Any] | None:
    keys = obj.keys()
    possible_id = obj.get("match_id") or obj.get("matchId") or obj.get("id")
    if not possible_id:
        return None
    # Heuristic: must have some team names or scores
    teams = {
        "home": obj.get("home") or obj.get("homeTeamName") or obj.get("home_team") or obj.get("homeTeam"),
        "away": obj.get("away") or obj.get("awayTeamName") or obj.get("away_team") or obj.get("awayTeam"),
    }
    scores = {
        "home_score": obj.get("homeScore") or obj.get("home_score"),
        "away_score": obj.get("awayScore") or obj.get("away_score"),
    }
    if not any(teams.values()) and not any(scores.values()):
        return None
    return {
        "match_id": possible_id,
        "home_team": teams["home"],
        "away_team": teams["away"],
        "home_score": scores["home_score"],
        "away_score": scores["away_score"],
        "date": obj.get("date") or obj.get("matchDate") or obj.get("played_at"),
        "division": obj.get("division") or obj.get("division_id") or obj.get("divisionId"),
        "status": obj.get("status") or obj.get("state"),
        "__typename": obj.get("__typename"),
    }


def extract_team(obj: Dict[str, Any]) -> Dict[str, Any] | None:
    possible_id = obj.get("team_id") or obj.get("teamId") or obj.get("id")
    name = obj.get("team_name") or obj.get("teamName") or obj.get("name")
    if not (possible_id or name):
        return None
    return {
        "team_id": possible_id,
        "team_name": name,
        "division": obj.get("division") or obj.get("division_id") or obj.get("divisionId"),
        "format": obj.get("format") or (obj.get("type") if str(obj.get("type") or "").lower() in ("8-ball", "9-ball") else None),
        "rank": obj.get("rank") or obj.get("standing"),
        "points": obj.get("points") or obj.get("pts"),
        "__typename": obj.get("__typename"),
    }


def main():
    parser = argparse.ArgumentParser(description="Decode api_dump_v2.json into smaller extracts.")
    parser.add_argument("--dump", help="Path to api_dump_v2.json (defaults to latest)")
    args = parser.parse_args()

    dump_path = Path(args.dump) if args.dump else find_latest_dump()
    if not dump_path or not dump_path.exists():
        raise SystemExit("dump not found. Provide --dump or ensure data/apa_api_captures/*/api_dump_v2.json exists.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    data = json.load(open(dump_path, "r", encoding="utf-8"))
    typenames = Counter()
    alias_stats = defaultdict(lambda: {"8-ball": {}, "9-ball": {}})
    matches = []
    teams = []
    sessions = []
    leagues = []

    def process_body(body):
        parsed = parse_response_body(body)
        items = parsed if isinstance(parsed, list) else [parsed]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            walk(obj, alias_stats, None, typenames, teams, sessions, leagues, matches)

    for entry in data:
        process_body(entry.get("response_body"))

    alias_out = {str(k): v for k, v in alias_stats.items() if v.get("8-ball") or v.get("9-ball")}
    (OUT_DIR / "alias_stats_extract.json").write_text(json.dumps(alias_out, indent=2), encoding="utf-8")
    (OUT_DIR / "match_extract.json").write_text(json.dumps(matches, indent=2), encoding="utf-8")
    (OUT_DIR / "teams_extract.json").write_text(json.dumps(teams, indent=2), encoding="utf-8")
    (OUT_DIR / "sessions_extract.json").write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    (OUT_DIR / "leagues_extract.json").write_text(json.dumps(leagues, indent=2), encoding="utf-8")
    (OUT_DIR / "typenames_count.json").write_text(json.dumps(typenames.most_common(), indent=2), encoding="utf-8")

    print("dump:", dump_path)
    print("aliases with stats:", len(alias_out))
    print("matches captured:", len(matches))
    print("teams captured:", len(teams))
    print("sessions captured:", len(sessions))
    print("leagues captured:", len(leagues))
    print("top typenames:", typenames.most_common(10))
    print("outputs:", OUT_DIR)


if __name__ == "__main__":
    main()
