import sqlite3

c = sqlite3.connect("data/pipeline.db")
for model in ("ENTERPRISE", "FREE"):
    rows = c.execute(
        "SELECT json_extract(data_json,'$.content.startupName'), json_extract(data_json,'$.content.pricingUrl'), "
        "json_extract(data_json,'$.content.evidence') FROM records "
        "WHERE record_type='PRODUCT' AND json_extract(data_json,'$.content.pricingModel')=?", (model,)
    ).fetchall()
    print(f"\n== {model}: {len(rows)}")
    for r in rows:
        print(f"- {r[0]}  {r[1]}\n  {r[2]}")