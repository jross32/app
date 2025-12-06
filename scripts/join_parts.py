"""
join_parts.py

Recombine .partNN files (created from large JSONs) back into the original file.

Usage:
  python scripts/join_parts.py <basefile>

Example:
  python scripts/join_parts.py data/backups/raw/apa_raw_latest.json
    -> reads data/backups/raw/apa_raw_latest.json.part01, .part02, ...
    -> writes data/backups/raw/apa_raw_latest.json

Notes:
  - Parts must be in the same directory as the target base file.
  - Parts are detected as <base>.partNN and concatenated in numeric order.
"""
import sys
from pathlib import Path

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    base = Path(sys.argv[1])
    parts = sorted(base.parent.glob(base.name + ".part*"))
    if not parts:
        print(f"No parts found for {base}")
        sys.exit(1)
    print(f"Recombining {len(parts)} parts into {base}")
    with base.open("wb") as out:
        for part in parts:
            out.write(part.read_bytes())
    print("Done.")

if __name__ == "__main__":
    main()
