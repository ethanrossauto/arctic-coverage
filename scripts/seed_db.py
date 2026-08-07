#!/usr/bin/env python3
"""Apply the schema and seed the world. Idempotent, and meant to be re-run.

    .venv/bin/python scripts/seed_db.py

Re-running refreshes every `last_heard`, which is the point: the seeded world has
deliberately stale and silent assets, and those states are only interesting while
they are recent. It doubles as reset-to-known-state before a demo.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env.local")

from api._lib import assets, db  # noqa: E402

schema = (ROOT / "db" / "schema.sql").read_text()
db.apply_schema(schema)
print("schema applied")

rows = assets.seed_rows()
for row in rows:
    db.insert_entity(row)

counts: dict[str, int] = {}
for row in rows:
    counts[row["kind"]] = counts.get(row["kind"], 0) + 1
print(f"seeded {len(rows)} entities: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

if counts != assets.KIND_COUNTS:
    print(f"⚠️  expected {assets.KIND_COUNTS}, got {counts}", file=sys.stderr)
    sys.exit(1)
