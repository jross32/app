"""
Update the app data file (e.g., league_data_final_week11.json) from a new API
dump (api_dump_v2.json). This script:
- Reads the existing app data file.
- Maps alias_id -> player slug from that file.
- Streams the dump to harvest stats keyed by alias_id:
  * EightBallStats / NineBallStats lifetime blocks
  * EightBallPlayer / NineBallPlayer entries (session scoped)
- Updates lifetime_stats for each matched player (8-ball / 9-ball).
- Writes a backup of the target file, then writes the updated file.

Usage:
    python scripts/update_from_dump.py --dump data/apa_api_captures/.../api_dump_v2.json \
        --target data/league_data_final_week11.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


FMT_MAP = {
    "EightBallPlayer": "8-ball",
    "EightBallLifetimeStatistics": "8-ball",
    "NineBallPlayer": "9-ball",
    "NineBallLifetimeStatistics": "9-ball",
}

STAT_KEYS = [
    "matchesPlayed",
    "matchesWon",
    "matchCountForLastTwoYrs",
    "defensiveShotAvg",
    "CLA",
    "lastPlayed",
]


def parse_response_body(resp_body: Any) -> Any:
    if isinstance(resp_body, str):
        try:
            return json.loads(resp_body)
        except json.JSONDecodeError:
            return resp_body
    return resp_body


def normalize_alias_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def harvest(dest: Dict[str, Any], stat: Dict[str, Any]) -> None:
    for key in STAT_KEYS:
        if key in stat:
            dest[key] = stat[key]


def walk(node: Any, alias_set: set[int], lifetime: Dict[int, Dict[str, Dict[str, Any]]], current_alias: int | None = None) -> None:
    if isinstance(node, dict):
        # If this node itself matches an alias id and has a known __typename, harvest
        node_id = normalize_alias_id(node.get("id"))
        if node_id is not None and node_id in alias_set:
            fmt = FMT_MAP.get(node.get("__typename"))
            if fmt:
                harvest(lifetime[node_id][fmt], node)

        # If this dict is an Alias with stats blocks, harvest those for the alias
        if node.get("__typename") == "Alias" and "id" in node:
            candidate = normalize_alias_id(node["id"])
            if candidate is not None and candidate in alias_set:
                current_alias = candidate
            if current_alias in alias_set:
                for key, fmt in (("EightBallStats", "8-ball"), ("NineBallStats", "9-ball")):
                    if key in node and node[key]:
                        for stat in node[key]:
                            harvest(lifetime[current_alias][fmt], stat)
                # players array may contain session-scoped blocks
                if "players" in node:
                    for pl in node["players"]:
                        fmt = FMT_MAP.get(pl.get("__typename"))
                        if fmt and current_alias is not None:
                            harvest(lifetime[current_alias][fmt], pl)

        # Recurse values
        for v in node.values():
            walk(v, alias_set, lifetime, current_alias)

    elif isinstance(node, list):
        for item in node:
            walk(item, alias_set, lifetime, current_alias)


def main() -> int:
    parser = argparse.ArgumentParser(description="Update app data from an APA API dump.")
    parser.add_argument("--dump", required=True, help="Path to api_dump_v2.json")
    parser.add_argument("--target", required=True, help="Path to app data JSON (e.g., league_data_final_week11.json)")
    parser.add_argument("--backup-dir", default="data/backups", help="Directory to store backups")
    args = parser.parse_args()

    dump_path = Path(args.dump)
    target_path = Path(args.target)

    if not dump_path.exists():
        print(f"Dump not found: {dump_path}", file=sys.stderr)
        return 1
    if not target_path.exists():
        print(f"Target data file not found: {target_path}", file=sys.stderr)
        return 1

    # Load target data
    week = json.load(target_path.open())
    players = week.get("players", {})
    alias_set = {int(v["alias_id"]) for v in players.values() if "alias_id" in v}
    slug_by_alias = {int(v["alias_id"]): slug for slug, v in players.items() if "alias_id" in v}

    lifetime: Dict[int, Dict[str, Dict[str, Any]]] = defaultdict(lambda: {"8-ball": {}, "9-ball": {}})

    # Stream the dump (it is a list of entries, each with response_body as a list/dict)
    dump = json.load(dump_path.open())
    for entry in dump:
        bodies = parse_response_body(entry.get("response_body"))
        if bodies:
            walk(bodies, alias_set, lifetime, None)

    # Merge into week data
    updated = 0
    for aid, stats in lifetime.items():
        slug = slug_by_alias.get(aid)
        if not slug:
            continue
        p = players.get(slug, {})
        if "lifetime_stats" not in p:
            p["lifetime_stats"] = {}
        changed = False
        for fmt, stat in stats.items():
            if not stat:
                continue
            dest = p["lifetime_stats"].setdefault(fmt, {})
            before = dict(dest)
            dest.update(stat)
            if dest != before:
                changed = True
        if changed:
            players[slug] = p
            updated += 1

    week["players"] = players

    # Backup and write
    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{target_path.stem}_{ts}.bak.json"
    shutil.copy2(target_path, backup_path)
    json.dump(week, target_path.open("w"), indent=2)

    print(f"Aliases seen in dump: {len(lifetime)}")
    print(f"Players updated: {updated}")
    print(f"Backup written to: {backup_path}")
    print(f"Updated file: {target_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
