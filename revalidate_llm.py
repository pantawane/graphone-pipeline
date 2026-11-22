import json
import sqlite3

from src.db import url_hash
from src.sources.products_llm import make_validator

c = sqlite3.connect("data/pipeline.db")
rows = c.execute(
    "SELECT id, dedupe_key, source_url, data_json FROM records "
    "WHERE record_type='PRODUCT' AND json_extract(data_json,'$.content.method') LIKE 'llm:%'"
).fetchall()
removed = []
for rid, key, url, dj in rows:
    d = json.loads(dj)["content"]
    body = c.execute("SELECT body FROM raw_pages WHERE url_hash=? ORDER BY id DESC LIMIT 1", (url_hash(url),)).fetchone()
    reason = "no_saved_page" if not body or not body[0] else make_validator(body[0])(
        {"pricing_model": d["pricingModel"], "evidence": d["evidence"]})
    if reason:
        c.execute("DELETE FROM records WHERE id=?", (rid,))
        c.execute("INSERT INTO crawl_state (source, key, value, updated_at) VALUES ('products', ?, ?, datetime('now')) "
                  "ON CONFLICT(source, key) DO UPDATE SET value=excluded.value", (key, f"llm_revalidated_removed:{reason}"))
        removed.append((d["startupName"], d["pricingModel"], reason))
c.commit()
print(f"checked {len(rows)} LLM records, removed {len(removed)}")
print(removed[:15])