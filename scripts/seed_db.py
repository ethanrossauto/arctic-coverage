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

# 🔴 REMOVE SEED ROWS THAT ARE NO LONGER IN THE SEED, BEFORE INSERTING THE ONES THAT ARE.
#
# This script upserts, so it used to leave orphans behind forever: when the ten hydrophones
# moved from the narrows into one array their ids changed, and the first reseed produced a
# world with TWENTY hydrophones, ten of them at positions the code no longer knew about and
# ageing steadily into "overdue" because nothing refreshed them.
#
# ⚠️ SCOPED TO `created_by = 'seed'` DELIBERATELY. Anything a person placed through the
# command layer is theirs and survives a reseed; the five-minute idle reset in
# api/_lib/lifecycle.py is the one that clears everything, and that difference is the whole
# distinction between "refresh the world" and "reset it".
keep = tuple(row["id"] for row in rows)
with db.connect() as conn, conn.cursor() as cur:
    cur.execute(
        "delete from entities where created_by = 'seed' and not (id = any(%s)) returning id",
        (list(keep),),
    )
    stale = [r["id"] for r in cur.fetchall()]
    conn.commit()
if stale:
    print(f"removed {len(stale)} stale seed entities: {', '.join(sorted(stale))}")

db.insert_entities(rows)

counts: dict[str, int] = {}
for row in rows:
    counts[row["kind"]] = counts.get(row["kind"], 0) + 1
print(f"seeded {len(rows)} entities: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

if counts != assets.KIND_COUNTS:
    print(f"⚠️  expected {assets.KIND_COUNTS}, got {counts}", file=sys.stderr)
    sys.exit(1)
