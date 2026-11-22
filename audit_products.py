import random
import sqlite3

c = sqlite3.connect("data/pipeline.db")
rows = c.execute(
    "SELECT json_extract(data_json,'$.content.startupName'), json_extract(data_json,'$.content.pricingModel'), "
    "json_extract(data_json,'$.content.pricingUrl'), json_extract(data_json,'$.content.evidence') "
    "FROM records WHERE record_type='PRODUCT'"
).fetchall()
by: dict = {}
for r in rows:
    by.setdefault(r[1], []).append(r)
for model, items in by.items():
    print(f"\n== {model}: {len(items)}")
    for r in random.sample(items, min(4, len(items))):
        print(f"- {r[0]}  {r[2]}\n  evidence: {r[3]}")