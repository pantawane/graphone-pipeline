import sqlite3

c = sqlite3.connect("data/pipeline.db")
for method in ("fuzzy_edit_distance", "alias", "core_name", "domain"):
    rows = c.execute(
        "SELECT raw_name, canonical_name, score FROM entity_mapping_log WHERE record_type='STARTUP' AND method=?",
        (method,),
    ).fetchall()
    print(f"\n== {method}: {len(rows)}")
    for r in rows:
        print("  ", r)

rows = c.execute(
    "SELECT raw_name, canonical_name FROM entity_mapping_log WHERE record_type='STARTUP' AND method='exact_normalized'"
).fetchall()
print(f"\n== exact_normalized: {len(rows)} (matched a seed or an earlier startup with the same name)")
for r in rows:
    print("  ", r)