#!/usr/bin/env python
"""
Merge membership history years from the latest api_dump_v2.json into league_data_final_week11.json.
Matches players by apa_id, then member_id, then alias_id.
Creates a backup alongside the target file before writing.
"""
import json
import sys
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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read dump: {e}")
        return []
    entries = data if isinstance(data, list) else data.get("responses") or data.get("response_body") or []
    parsed = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        entry_copy = dict(entry)
        entry_copy["response_body"] = parse_response_body(entry_copy.get("response_body"))
        parsed.append(entry_copy)
    return parsed


def build_membership_map(entries):
    membership_map = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rb = entry.get("response_body")
        if not rb or not isinstance(rb, list):
            continue
        for part in rb:
            if not isinstance(part, dict):
                continue
            alias_data = part.get("data")
            if not isinstance(alias_data, dict):
                continue
            alias = alias_data.get("alias")
            if not isinstance(alias, dict):
                continue
            member = alias.get("member") if isinstance(alias.get("member"), dict) else None
            if not member:
                continue
            history = member.get("membershipHistory")
            if not (isinstance(history, list) and history):
                continue
            years = sorted({h.get("year") for h in history if isinstance(h, dict) and h.get("year")})
            if not years:
                continue
            mid = str(member.get("id")) if member.get("id") else None
            aid = str(member.get("apaId")) if member.get("apaId") else None
            alias_id = str(alias.get("id")) if alias.get("id") else None
            key = None
            if aid:
                key = ("apa_id", aid)
            elif mid:
                key = ("member_id", mid)
            elif alias_id:
                key = ("alias_id", alias_id)
            if key:
                membership_map[key] = years
    return membership_map


def apply_membership(league_path: Path, membership_map: dict) -> int:
    data = json.loads(league_path.read_text(encoding="utf-8"))
    players = data.get("players", {}) or {}
    updated = 0
    for pid, p in players.items():
        applied = False
        for kfield in ("apa_id", "member_id", "alias_id"):
            key_val = str(p.get(kfield) or "")
            if key_val and (kfield, key_val) in membership_map:
                p["membership_years"] = membership_map[(kfield, key_val)]
                applied = True
                break
        if applied:
            updated += 1
    backup = league_path.with_suffix(".bak.membership.json")
    backup.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # If you want to replace the main file, uncomment the next line:
    # league_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Mapped membership_years to {updated} players; backup at {backup}")
    return updated


def main():
    latest = find_latest_dump(CAPTURES_ROOT)
    if not latest:
        print("No api_dump_v2.json found.")
        sys.exit(1)
    entries = load_dump(latest)
    membership_map = build_membership_map(entries)
    if not membership_map:
        print("No membershipHistory found in dump.")
        sys.exit(1)
    updated = apply_membership(LEAGUE_FILE, membership_map)
    if updated == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
