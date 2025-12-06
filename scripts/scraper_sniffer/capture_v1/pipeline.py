"""
Raw-only APA data pipeline.

Steps:
1) URL discovery
2) API sniffer (creates a new capture run with api_dump_v2.json)
3) Snapshot raw dump with timestamp
4) Copy raw into Flask app data folder (stable, archive, symlink, metadata)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Base directories
BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent

# Capture runs live here (e.g., data/apa_api_captures/2025-12-01_14-46-33)
CAPTURE_ROOT = REPO_ROOT / "data" / "apa_api_captures"

# Flask app data directory
FLASK_DATA_DIR = REPO_ROOT / "data"
FLASK_RAW_STABLE_FILENAME = "apa_raw_latest.json"   # stable name
FLASK_META_FILENAME = "apa_data_current.json"       # metadata for current raw file

# Commands for discovery/sniffer (run in BASE_DIR)
URL_DISCOVERY_CMD = [sys.executable, "apa_url_discovery.py"]
API_SNIFFER_CMD = [sys.executable, "apa_api_sniffer.py"]


# -----------------------------
# Helpers
# -----------------------------

def run_step(name: str, cmd, cwd: Path | None = None) -> None:
    """Run a subprocess step with logging."""
    print("\n==============================")
    print(f"-> {name}")
    print(f"   CMD: {' '.join(cmd)}")
    print(f"   CWD: {cwd or BASE_DIR}")
    print("==============================\n")

    result = subprocess.run(cmd, cwd=str(cwd or BASE_DIR))
    if result.returncode != 0:
        raise RuntimeError(f"{name} failed with exit code {result.returncode}")
    print(f"OK. {name} completed.\n")


def get_latest_capture_dir() -> Path:
    """Pick the latest run dir in apa_api_captures."""
    if not CAPTURE_ROOT.is_dir():
        raise RuntimeError(f"Capture root not found: {CAPTURE_ROOT}")

    entries = [d for d in CAPTURE_ROOT.iterdir() if d.is_dir()]
    if not entries:
        raise RuntimeError(f"No capture runs found in {CAPTURE_ROOT}")

    latest_path = sorted(entries)[-1]  # run folders are like 2025-12-03_21-17-08
    print(f"Using latest capture run: {latest_path}")
    return latest_path


def snapshot_raw_dump(run_dir: Path, timestamp: str) -> Path:
    """
    Copy the sniffer's raw API dump (api_dump_v2.json) into a timestamped snapshot:
        apa_raw_latest_YYYY-MM-DD_HH-MM-SS.json
    """
    src = run_dir / "api_dump_v2.json"
    if not src.exists():
        raise FileNotFoundError(f"api_dump_v2.json not found in {run_dir}")

    new_name = f"apa_raw_latest_{timestamp}.json"
    new_path = run_dir / new_name

    shutil.copy2(src, new_path)
    print(f"Raw dump snapshot created in capture run: {new_path}")

    return new_path


def copy_raw_to_flask_app(raw_path: Path, capture_run_dir: Path, timestamp: str) -> None:
    """
    Copy the raw APA dump into the Flask app data folder:
      1) Stable name:   apa_raw_latest.json
      2) Timestamped:   apa_raw_latest_YYYY-MM-DD_HH-MM-SS.json
      3) Symlink:       apa_raw_latest.symlink.json -> timestamped file (best-effort)
      4) Metadata:      apa_data_current.json describing the current raw file.
    """
    if not FLASK_DATA_DIR:
        print("FLASK_DATA_DIR not set; skipping Flask copy.")
        return

    FLASK_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1) Stable file
    raw_stable = FLASK_DATA_DIR / FLASK_RAW_STABLE_FILENAME
    shutil.copy2(raw_path, raw_stable)
    print(f"RAW -> Flask (stable): {raw_stable}")

    # 2) Timestamped archived copy in Flask data dir
    raw_ts_name = raw_path.name
    raw_archive = FLASK_DATA_DIR / raw_ts_name
    if not raw_archive.exists():
        shutil.copy2(raw_path, raw_archive)
        print(f"RAW -> Flask (archive): {raw_archive}")
    else:
        print(f"RAW archive already exists in Flask data: {raw_archive}")

    # 3) Symlink (best-effort)
    symlink_path = FLASK_DATA_DIR / "apa_raw_latest.symlink.json"
    symlink_status = "not created"
    try:
        if symlink_path.exists() or symlink_path.is_symlink():
            symlink_path.unlink()
        os.symlink(raw_ts_name, symlink_path)
        symlink_status = "created"
        print(f"Symlink created: {symlink_path} -> {raw_ts_name}")
    except (OSError, NotImplementedError) as e:
        symlink_status = f"failed: {e}"
        print(f"Symlink not created (OS limitation or permissions): {e}")

    # 4) Metadata
    meta_path = FLASK_DATA_DIR / FLASK_META_FILENAME

    history: list[str] = []
    if meta_path.exists():
        try:
            with meta_path.open("r", encoding="utf-8") as f:
                existing_meta = json.load(f)
            prev = existing_meta.get("current_raw_file")
            if prev:
                history = existing_meta.get("history", [])
                if prev not in history:
                    history.append(prev)
            history = history[-10:]
        except Exception:
            history = []

    meta = {
        "mode": "raw-only",
        "timestamp": timestamp,
        "current_raw_file": raw_ts_name,
        "stable_raw_file": raw_stable.name,
        "symlink": symlink_path.name,
        "symlink_status": symlink_status,
        "capture_run_dir": os.path.relpath(str(capture_run_dir), str(BASE_DIR)),
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "history": history,
    }

    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"Wrote Flask meta file: {meta_path}")


# -----------------------------
# Main pipeline
# -----------------------------

def main() -> int:
    # 1) URL Discovery
    run_step("URL Discovery", URL_DISCOVERY_CMD)

    # 2) API Sniffer - should generate a new capture run under apa_api_captures
    run_step("API Sniffer", API_SNIFFER_CMD)

    # 3) Determine latest run folder
    latest_run_dir = get_latest_capture_dir()

    # Single timestamp used for snapshot + Flask copies
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # 4) Snapshot the raw dump (api_dump_v2.json)
    raw_snapshot_path = snapshot_raw_dump(latest_run_dir, timestamp)

    # 5) Copy raw snapshot into Flask app data folder and write metadata / symlink
    try:
        copy_raw_to_flask_app(raw_snapshot_path, latest_run_dir, timestamp)
    except Exception as e:
        print(f"Could not copy raw dump to Flask app: {e}")

    print("\nRAW-ONLY pipeline complete.")
    print(f"   Capture Run:         {latest_run_dir}")
    print(f"   Raw snapshot (run):  {raw_snapshot_path}")
    print(f"   Flask data dir:      {FLASK_DATA_DIR}")
    print(f"   Flask stable raw:    {FLASK_DATA_DIR / FLASK_RAW_STABLE_FILENAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
