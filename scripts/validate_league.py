import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INPUT = DATA_DIR / "league_data_final_week11.json"


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[fail] read {path}: {e}")
        return {}


def main():
    data = load(INPUT)
    divisions = data.get("divisions", {})
    teams = data.get("teams", {})
    players = data.get("players", {})
    matches = data.get("matches", {})

    errs = []

    # Check basics
    for key in ("divisions", "teams", "players", "matches"):
        if key not in data:
            errs.append(f"missing top key {key}")

    # Teams -> division exists
    for tid, t in teams.items():
        div_id = t.get("division_id")
        if div_id and str(div_id) not in divisions:
            errs.append(f"team {tid} references missing division {div_id}")

    # Roster players exist
    for tid, t in teams.items():
        roster = t.get("roster") or t.get("roster_alias_ids") or t.get("player_ids") or []
        for ref in roster:
            pid = ref.get("player_id") if isinstance(ref, dict) else ref
            if pid and str(pid) not in players:
                errs.append(f"team {tid} roster missing player {pid}")

    # Matches teams exist
    for mid, m in matches.items():
        for role in ("home_team_id", "away_team_id"):
            tid = m.get(role)
            if tid and str(tid) not in teams:
                errs.append(f"match {mid} references missing team {tid} ({role})")

    # Formats
    valid_fmt = {"8-ball", "9-ball", ""}
    for tid, t in teams.items():
        if t.get("format", "") not in valid_fmt:
            errs.append(f"team {tid} has unknown format {t.get('format')}")
    for mid, m in matches.items():
        if m.get("format", "") not in valid_fmt:
            errs.append(f"match {mid} has unknown format {m.get('format')}")
    for did, d in divisions.items():
        if d.get("format", "") not in valid_fmt:
            errs.append(f"division {did} has unknown format {d.get('format')}")

    if errs:
        print("[warn] validation found issues:")
        for e in errs:
            print(" -", e)
    else:
        print("[ok] validation passed for", INPUT.name)


if __name__ == "__main__":
    main()
