#!/usr/bin/env python
"""
Merge session highlights (per-session break/run/rackless/skunk counts) from the latest api_dump_v2.json
into league_data_final_week11.json. Matches players by apa_id, member_id, then alias_id.
Writes a backup alongside the league file; does not overwrite the main file unless you choose to.
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


def extract_session_highlights(entries):
    """
    Returns a map keyed by (kind, id) -> list of session records:
    {
      ('apa_id','123'): [
         {'session_id':137,'format':'8-ball','eight_on_breaks':1, ...}
      ]
    }
    """
    highlights = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rb = entry.get("response_body")
        if not rb or not isinstance(rb, list):
            continue
        for part in rb:
            if not isinstance(part, dict):
                continue
            alias_data = part.get("data") if isinstance(part.get("data"), dict) else None
            if not alias_data:
                continue
            alias = alias_data.get("alias")
            if not isinstance(alias, dict):
                continue
            member = alias.get("member") if isinstance(alias.get("member"), dict) else None
            alias_id = alias.get("id")
            member_id = member.get("id") if member else None
            apa_id = member.get("apaId") if member else None
            name_key = normalize_name(alias.get("displayName") or alias.get("name") or "")
            players_list = alias.get("players") if isinstance(alias.get("players"), list) else []
            for p in players_list:
                if not isinstance(p, dict):
                    continue
                sess = p.get("session") if isinstance(p.get("session"), dict) else None
                sess_id = sess.get("id") if sess else None
                if not sess_id:
                    continue
                entry_fmt = p.get("__typename") or ""
                fmt = "8-ball" if "EightBall" in entry_fmt else "9-ball" if "NineBall" in entry_fmt else None
                if not fmt:
                    continue
                record = {
                    "session_id": sess_id,
                    "format": fmt,
                    "nine_on_snaps": p.get("nineOnSnaps") or 0,
                    "eight_on_breaks": p.get("eightOnBreaks") or 0,
                    "nine_ball_break_and_runs": p.get("nineBallBreakAndRuns") or 0,
                    "eight_ball_break_and_runs": p.get("eightBallBreakAndRuns") or 0,
                    "rackless": p.get("rackless") or 0,
                    "skunks": p.get("skunks") or 0,
                    "miniSlams": p.get("miniSlams") or 0,
                }
                for kind, kid in (("apa_id", apa_id), ("member_id", member_id), ("alias_id", alias_id)):
                    if kid:
                        highlights.setdefault((kind, str(kid)), []).append(record)
                if name_key:
                    highlights.setdefault(("name", name_key), []).append(record)
    return highlights


def apply_highlights(league_path: Path, highlights: dict) -> int:
    league = json.loads(league_path.read_text(encoding="utf-8"))
    players = league.get("players", {}) or {}
    updated = 0
    for pid, p in players.items():
        applied = False
        for kfield in ("apa_id", "member_id", "alias_id"):
            k = str(p.get(kfield) or "")
            if k and (kfield, k) in highlights:
                p["session_highlights"] = highlights[(kfield, k)]
                applied = True
                break
        if not applied:
            name_key = normalize_name(p.get("full_name") or p.get("short_name") or "")
            if name_key and ("name", name_key) in highlights:
                p["session_highlights"] = highlights[("name", name_key)]
                applied = True
        if applied:
            updated += 1
    backup = league_path.with_suffix(".bak.session_highlights.json")
    backup.write_text(json.dumps(league, indent=2), encoding="utf-8")
    # To overwrite the main file, uncomment:
    # league_path.write_text(json.dumps(league, indent=2), encoding="utf-8")
    print(f"session_highlights mapped to {updated} players; backup at {backup}")
    return updated


def main():
    latest = find_latest_dump(CAPTURES_ROOT)
    if not latest:
        print("No api_dump_v2.json found.")
        return 1
    entries = load_dump(latest)
    hl = extract_session_highlights(entries)
    if not hl:
        print("No session highlights found in dump.")
        return 1
    updated = apply_highlights(LEAGUE_FILE, hl)
    return 0 if updated else 1


if __name__ == "__main__":
    raise SystemExit(main())
