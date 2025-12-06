#!/usr/bin/env python
"""
Merge extended lifetime stats (CLA, defensiveShotAvg, matchCountForLastTwoYrs, lastPlayed)
from the latest api_dump_v2.json into league_data_final_week11.json.
Matches players by apa_id, member_id, alias_id, or normalized name.
Creates a backup alongside the league file and overwrites the main file.
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
LEAGUE_FILE = DATA_DIR / "league_data_final_week11.json"
CAPTURES_ROOT = DATA_DIR / "apa_api_captures"


def find_latest_dump(root: Path) -> Path | None:
    candidates = list(root.rglob("api_dump_v2.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_dump(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return data.get("responses") or data.get("response_body") or []


def normalize_name(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def build_lifetime_map(entries):
    stats_map = {}  # key: (kind,id) -> {'8-ball': {...}, '9-ball': {...}}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rb = entry.get("response_body")
        if not rb or not isinstance(rb, list):
            continue
        for part in rb:
            if not isinstance(part, dict):
                continue
            data = part.get("data") if isinstance(part.get("data"), dict) else None
            if not data:
                continue
            alias = data.get("alias")
            if not isinstance(alias, dict):
                continue
            member = alias.get("member") if isinstance(alias.get("member"), dict) else None
            alias_id = alias.get("id")
            member_id = member.get("id") if member else None
            apa_id = member.get("apaId") if member else None
            name_key = normalize_name(alias.get("displayName") or alias.get("name") or "")
            for fmt_label, field in (("8-ball", "EightBallStats"), ("9-ball", "NineBallStats")):
                stats_list = alias.get(field) if isinstance(alias.get(field), list) else None
                if not stats_list:
                    continue
                for item in stats_list:
                    if not isinstance(item, dict):
                        continue
                    stats = {
                        "matchesPlayed": item.get("matchesPlayed"),
                        "matchesWon": item.get("matchesWon"),
                        "CLA": item.get("CLA"),
                        "defensiveShotAvg": item.get("defensiveShotAvg"),
                        "matchCountForLastTwoYrs": item.get("matchCountForLastTwoYrs"),
                        "lastPlayed": item.get("lastPlayed"),
                    }
                    for key in (("apa_id", apa_id), ("member_id", member_id), ("alias_id", alias_id), ("name", name_key)):
                        kind, kid = key
                        if kid:
                            entry_key = (kind, str(kid))
                            stats_map.setdefault(entry_key, {})[fmt_label] = stats
    return stats_map


def apply_lifetime(league_path: Path, stats_map: dict) -> int:
    league = json.loads(league_path.read_text(encoding="utf-8"))
    players = league.get("players", {}) or {}
    updated = 0
    for pid, p in players.items():
        applied = False
        for kfield in ("apa_id", "member_id", "alias_id"):
            k = str(p.get(kfield) or "")
            if k and (kfield, k) in stats_map:
                p["lifetime_extended"] = stats_map[(kfield, k)]
                applied = True
                break
        if not applied:
            name_key = normalize_name(p.get("full_name") or p.get("short_name") or "")
            if name_key and ("name", name_key) in stats_map:
                p["lifetime_extended"] = stats_map[("name", name_key)]
                applied = True
        if applied:
            updated += 1
    backup = league_path.with_suffix(".bak.lifetime_extended.json")
    backup.write_text(json.dumps(league, indent=2), encoding="utf-8")
    # overwrite main
    league_path.write_text(json.dumps(league, indent=2), encoding="utf-8")
    print(f"lifetime_extended mapped to {updated} players; backup at {backup}")
    return updated


def main():
    latest = find_latest_dump(CAPTURES_ROOT)
    if not latest:
        print("No api_dump_v2.json found.")
        return 1
    entries = load_dump(latest)
    stats_map = build_lifetime_map(entries)
    if not stats_map:
        print("No lifetime stats found in dump.")
        return 1
    updated = apply_lifetime(LEAGUE_FILE, stats_map)
    return 0 if updated else 1


if __name__ == "__main__":
    raise SystemExit(main())
