"""
merge_dump_to_structured.py

Minimal transformer to take a captured api_dump_v2.json and emit
an updated structured league JSON (players-only) using the mapping rules.

This does NOT overwrite the live data. It writes to:
  data/backups/session/league_data_from_dump.json

Usage:
  python merge_dump_to_structured.py

Assumptions:
  - dump file: scripts/scraper_sniffer/capture_v2/apa_api_captures/<run>/api_dump_v2.json
    (auto-picks the latest run)
  - current structured file: data/league_data_final_week11.json

Scope:
  - updates player objects with lifetime stats, lifetime_extended,
    session highlights, membership years/leagues, consecutive years, and session list.
  - does not modify divisions/teams/matches.
  - merges by alias_id (apa_id) if possible; otherwise by name match.
"""
import json
import os
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
STRUCTURED_FILE = DATA_DIR / "league_data_final_week11.json"
CAPTURE_BASE = Path(__file__).resolve().parent / "apa_api_captures"
OUTPUT_FILE = DATA_DIR / "backups" / "session" / "league_data_from_dump.json"

def latest_dump():
    runs = sorted([p for p in CAPTURE_BASE.iterdir() if p.is_dir()])
    if not runs:
        raise FileNotFoundError("No capture runs found under apa_api_captures/")
    latest = runs[-1]
    dump = latest / "api_dump_v2.json"
    if not dump.exists():
        raise FileNotFoundError(f"No api_dump_v2.json in {latest}")
    return dump

def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def safe_write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

def normalize_name(name: str) -> str:
    return "".join(ch.lower() for ch in name or "" if ch.isalnum())

def merge_players(structured: dict, dump_recs: list):
    players = structured.get("players", {}) or {}

    def find_player_by_alias(alias_id, display_name):
        if alias_id:
            for key, pdata in players.items():
                if str(pdata.get("apa_id") or "").strip() == str(alias_id):
                    return key
        if display_name:
            target = normalize_name(display_name)
            for key, pdata in players.items():
                nm = normalize_name(pdata.get("full_name") or pdata.get("short_name") or "")
                if nm == target:
                    return key
        return None

    stat_labels = {
        "eight_ball_break_and_runs": "eightBallBreakAndRuns",
        "nine_ball_break_and_runs": "nineBallBreakAndRuns",
        "eight_on_breaks": "eightOnBreaks",
        "nine_on_snaps": "nineOnSnaps",
        "rackless": "rackless",
        "skunks": "skunks",
        "miniSlams": "miniSlams",
    }

    for rec in dump_recs:
        body = rec.get("response_body") or []
        chunks = body if isinstance(body, list) else [body]
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            alias = (chunk.get("data") or {}).get("alias")
            if not alias:
                continue
            alias_id = alias.get("id")
            display_name = alias.get("displayName")
            player_key = find_player_by_alias(alias_id, display_name)
            if not player_key:
                continue  # skip unknown players
            pdata = players.get(player_key, {})

            # Membership
            member = alias.get("member") or {}
            hist = member.get("membershipHistory") or []
            years = [h.get("year") for h in hist if isinstance(h, dict)]
            leagues = [
                {
                    "league_id": (h.get("leaguePaidIn") or {}).get("id"),
                    "league_name": (h.get("leaguePaidIn") or {}).get("name"),
                }
                for h in hist if isinstance(h, dict) and h.get("leaguePaidIn")
            ]
            if years:
                pdata["membership_years"] = sorted({y for y in years if isinstance(y, int)}, reverse=True)
            if leagues:
                pdata["membership_leagues"] = leagues
            if member.get("consecutiveYearsPlayed") is not None:
                pdata["consecutive_years_played"] = member.get("consecutiveYearsPlayed")

            # Sessions list
            sessions = alias.get("sessions") or []
            if sessions:
                pdata.setdefault("session_list", {}).setdefault("8-ball", [])
                pdata.setdefault("session_list", {}).setdefault("9-ball", [])
                for s in sessions:
                    fmt = (s.get("__typename") or "").lower()
                    entry = {"id": s.get("id"), "name": s.get("name")}
                    if "eight" in fmt:
                        pdata["session_list"]["8-ball"].append(entry)
                    elif "nine" in fmt:
                        pdata["session_list"]["9-ball"].append(entry)

            # Lifetime stats
            for key_fmt, target_fmt in [("EightBallStats", "8-ball"), ("NineBallStats", "9-ball")]:
                blocks = alias.get(key_fmt) or []
                if isinstance(blocks, list):
                    for bl in blocks:
                        if bl.get("__typename", "").endswith("LifetimeStatistics"):
                            pdata.setdefault("lifetime_stats", {})[target_fmt] = {
                                "matchesPlayed": bl.get("matchesPlayed"),
                                "matchesWon": bl.get("matchesWon"),
                                "CLA": bl.get("CLA"),
                                "defensiveShotAvg": bl.get("defensiveShotAvg"),
                                "matchCountForLastTwoYrs": bl.get("matchCountForLastTwoYrs"),
                                "lastPlayed": bl.get("lastPlayed"),
                            }

            # Session highlights
            highlights = pdata.get("session_highlights") or []
            fmt = (alias.get("players") or [{}])[0].get("__typename", "").lower()
            session_id = (alias.get("players") or [{}])[0].get("session", {}).get("id")
            session_name = (alias.get("players") or [{}])[0].get("session", {}).get("name")
            if fmt and session_id:
                fmt_label = "8-ball" if "eight" in fmt else "9-ball"
                agg = {k: (alias.get("players")[0].get(k) or 0) for k in stat_labels.values()}
                highlights.append({
                    "session_id": session_id,
                    "session_name": session_name,
                    "format": fmt_label,
                    **agg,
                })
                pdata["session_highlights"] = highlights

            players[player_key] = pdata

    structured["players"] = players
    return structured

def main():
    dump_path = latest_dump()
    print(f"[INFO] Using dump: {dump_path}")
    dump_recs = load_json(dump_path)

    structured = load_json(STRUCTURED_FILE)
    structured = merge_players(structured, dump_recs)

    safe_write(OUTPUT_FILE, structured)
    print(f"[DONE] Wrote merged file to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
