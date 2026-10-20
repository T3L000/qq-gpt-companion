"""Create a consistent local backup. Treat resulting files as private."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from companion import core


def main():
    core.init()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = core.ROOT / "backups" / stamp
    dest.mkdir(parents=True, exist_ok=False)

    # SQLite online backup includes WAL data without copying an inconsistent WAL file.
    with core.open_db() as src:
        backup = sqlite3.connect(str(dest / "companion.sqlite3"))
        try:
            src.backup(backup)
        finally:
            backup.close()

    shutil.copy2(core.ROOT / "config.json", dest / "config.json")
    shutil.copy2(core.ROOT / "persona" / "persona.md", dest / "persona.md")
    snapshots = core.ROOT / "data" / "handoffs"
    if snapshots.exists():
        shutil.copytree(snapshots, dest / "handoffs")

    records = {}
    for p in sorted(dest.rglob("*")):
        if p.is_file():
            records[str(p.relative_to(dest)).replace("\\", "/")] = hashlib.sha256(p.read_bytes()).hexdigest()
    (dest / "manifest.json").write_text(
        json.dumps({"created_utc": stamp, "sha256": records, "note": "PRIVATE; encrypt before transfer"},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"Backup created: {dest}")
    print("Contains private conversations and configuration. Encrypt before moving off this PC.")


if __name__ == "__main__":
    main()
