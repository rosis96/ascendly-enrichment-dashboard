#!/usr/bin/env python3
"""One-time migration: copy your data from the OLD Neon database into the NEW
Railway Postgres. Safe to re-run: it's read-only on the source and only inserts
rows that aren't already in the target (ON CONFLICT DO NOTHING).

Copies workspaces, lists, leads (with enrichment results), custom variables,
hidden variables, and correction rules. Uses BATCHED inserts so it's fast even
over a remote connection.

------------------------------------------------------------------------------
HOW TO RUN (on your Mac, once Neon is reachable again):

  pip3 install "psycopg2-binary>=2.9"

  SOURCE_DB_URL="<old Neon connection string>" \
  TARGET_DB_URL="<Railway Postgres PUBLIC url, the ...proxy.rlwy.net one>" \
  python3 migrate_from_neon.py
------------------------------------------------------------------------------
"""
import os
import sys
import psycopg2
from psycopg2.extras import Json, RealDictCursor, execute_values

# Copy order respects foreign keys (lead_lists before leads).
TABLES = ["workspaces", "lead_lists", "leads",
          "custom_variables", "hidden_variables", "enrich_rules"]
BATCH = 200


def norm(url):
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def main():
    src = os.getenv("SOURCE_DB_URL")
    dst = os.getenv("TARGET_DB_URL")
    if not src or not dst:
        sys.exit("Set SOURCE_DB_URL (old Neon) and TARGET_DB_URL (Railway public URL).")

    s = psycopg2.connect(norm(src))
    s.set_session(readonly=True)
    t = psycopg2.connect(norm(dst))
    sc = s.cursor(cursor_factory=RealDictCursor)
    tc = t.cursor()

    for tbl in TABLES:
        try:
            sc.execute(f"SELECT * FROM {tbl}")
            rows = sc.fetchall()
        except Exception as e:
            print(f"skip {tbl}: {e}")
            s.rollback()
            continue
        if not rows:
            print(f"{tbl}: 0 rows in source")
            continue

        cols = list(rows[0].keys())
        collist = ",".join(f'"{c}"' for c in cols)
        sql = f'INSERT INTO {tbl} ({collist}) VALUES %s ON CONFLICT DO NOTHING'

        def tup(r):
            return tuple(Json(r[c]) if isinstance(r[c], (dict, list)) else r[c] for c in cols)

        data = [tup(r) for r in rows]
        done = 0
        for i in range(0, len(data), BATCH):
            chunk = data[i:i + BATCH]
            try:
                execute_values(tc, sql, chunk, page_size=BATCH)
                t.commit()
            except Exception as e:
                # one bad row shouldn't lose the batch: retry the chunk row-by-row
                t.rollback()
                onerow = f'INSERT INTO {tbl} ({collist}) VALUES ({",".join(["%s"] * len(cols))}) ON CONFLICT DO NOTHING'
                for row in chunk:
                    try:
                        tc.execute(onerow, row)
                        t.commit()
                    except Exception as e2:
                        t.rollback()
                        print(f"  row skipped in {tbl}: {e2}")
            done = min(i + BATCH, len(data))
            if len(data) > BATCH:
                print(f"  {tbl}: {done}/{len(data)}")

        # Bump the id sequence so future inserts don't collide with copied ids.
        try:
            tc.execute(
                f"SELECT setval(pg_get_serial_sequence('{tbl}','id'), "
                f"(SELECT COALESCE(MAX(id), 1) FROM {tbl}))"
            )
            t.commit()
        except Exception as e:
            print(f"  (sequence note for {tbl}: {e})")
            t.rollback()

        print(f"{tbl}: {len(rows)} copied")

    sc.close(); tc.close(); s.close(); t.close()
    print("\nDone. Reload the dashboard - your workspace and leads should be back.")


if __name__ == "__main__":
    main()
