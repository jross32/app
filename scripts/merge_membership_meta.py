#!/usr/bin/env python
"""
Merge membership metadata (years, leagues, consecutiveYearsPlayed) from the latest api_dump_v2.json
into league_data_final_week11.json. Matches by apa_id, member_id, alias_id, or normalized name.
Creates a backup and overwrites the main file.
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


def parse_response_body(response_body):
    if isinstance(response_body, str):
        try:
            return json.loads(response_body)
        except json.JSONDecodeError:
            return []
    return response_body


def load_dump(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data if isinstance(data, list) else data.get("responses") or data.get("response_body") or []
    parsed = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        entry_copy = dict(entry)
        entry_copy["response_body"] = parse_response_body(entry_copy.get("response_body"))
        parsed.append(entry_copy)
    return parsed


def normalize_name(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def build_membership_meta(entries):
    meta_map = {}
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
            if not member:
                continue
            alias_id = alias.get("id")
            member_id = member.get("id")
            apa_id = member.get("apaId")
            name_key = normalize_name(alias.get("displayName") or alias.get("name") or "")
            history = member.get("membershipHistory") if isinstance(member.get("membershipHistory"), list) else []
            years = []
            leagues = []
            for h in history:
                if not isinstance(h, dict):
                    continue
                year = h.get("year")
                if year:
                    years.append(year)
                lp = h.get("leaguePaidIn") if isinstance(h.get("leaguePaidIn"), dict) else None
                if lp and lp.get("id") and lp.get("name"):
                    leagues.append({"league_id": lp["id"], "league_name": lp["name"]})
            years = sorted(set(years))
            meta = {
                "membership_years": years,
                "membership_leagues": leagues,
                "consecutive_years_played": member.get("consecutiveYearsPlayed")
            }
            for key in (("apa_id", apa_id), ("member_id", member_id), ("alias_id", alias_id), ("name", name_key)):
                kind, kid = key
                if kid:
                    meta_map[(kind, str(kid))] = meta
    return meta_map


def apply_meta(league_path: Path, meta_map: dict) -> int:
    league = json.loads(league_path.read_text(encoding="utf-8"))
    players = league.get("players", {}) or {}
    updated = 0
    for pid, p in players.items():
        applied = False
        for kfield in ("apa_id", "member_id", "alias_id"):
            k = str(p.get(kfield) or "")
            if k and (kfield, k) in meta_map:
                m = meta_map[(kfield, k)]
                p["membership_years"] = m.get("membership_years") or p.get("membership_years") or []
                p["membership_leagues"] = m.get("membership_leagues") or []
                p["consecutive_years_played"] = m.get("consecutive_years_played")
                applied = True
                break
        if not applied:
            name_key = normalize_name(p.get("full_name") or p.get("short_name") or "")
            if name_key and ("name", name_key) in meta_map:
                m = meta_map[("name", name_key)]
                p["membership_years"] = m.get("membership_years") or p.get("membership_years") or []
                p["membership_leagues"] = m.get("membership_leagues") or []
                p["consecutive_years_played"] = m.get("consecutive_years_played")
                applied = True
        if applied:
            updated += 1
    backup = league_path.with_suffix(".bak.membership_meta.json")
    backup.write_text(json.dumps(league, indent=2), encoding="utf-8")
    league_path.write_text(json.dumps(league, indent=2), encoding="utf-8")
    print(f"membership meta mapped to {updated} players; backup at {backup}")
    return updated


def main():
    latest = find_latest_dump(CAPTURES_ROOT)
    if not latest:
        print("No api_dump_v2.json found.")
        return 1
    entries = load_dump(latest)
    meta_map = build_membership_meta(entries)
    if not meta_map:
        print("No membership metadata found in dump.")
        return 1
    updated = apply_meta(LEAGUE_FILE, meta_map)
    return 0 if updated else 1


if __name__ == "__main__":
    raise SystemExit(main())
