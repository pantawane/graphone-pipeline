"""Re-checks every rules-based PRODUCT against its saved page text with the CURRENT rules.
Removes records that no longer pass. No refetching needed."""
import re
import sqlite3
from urllib.parse import urlsplit

from src.db import url_hash
from src.sources.products import classify, mentions_company

c = sqlite3.connect("data/pipeline.db")
rows = c.execute(
    "SELECT id, dedupe_key, source_url, json_extract(data_json,'$.content.startupName'), "
    "json_extract(data_json,'$.content.pricingModel'), json_extract(data_json,'$.content.method') "
    "FROM records WHERE record_type='PRODUCT'"
).fetchall()

kept = skipped = 0
removed = []
for rid, key, url, name, label, method in rows:
    if method != "rules":
        skipped += 1
        continue
    body = c.execute("SELECT body FROM raw_pages WHERE url_hash=? ORDER BY id DESC LIMIT 1", (url_hash(url),)).fetchone()
    reason = None
    if not body or not body[0]:
        reason = "no_saved_page"
    elif not re.search(r"pricing|plans|price", urlsplit(url).path.lower()):
        reason = "homepage_evidence"
    else:
        new_label, _ = classify(body[0])
        if new_label != label:
            reason = f"label_changed_{label}_to_{new_label}"
        elif not mentions_company(name, body[0], url):
            reason = "company_not_on_page"
    if reason:
        c.execute("DELETE FROM records WHERE id=?", (rid,))
        c.execute(
            "INSERT INTO crawl_state (source, key, value, updated_at) VALUES ('products', ?, ?, datetime('now')) "
            "ON CONFLICT(source, key) DO UPDATE SET value=excluded.value",
            (key, f"revalidated_removed:{reason}"),
        )
        removed.append((name, reason))
    else:
        kept += 1
c.commit()

print(f"kept {kept}, removed {len(removed)}, skipped (LLM records) {skipped}")
reasons = {}
for _, r in removed:
    reasons[r] = reasons.get(r, 0) + 1
for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
    print(f"  {r:45} {n}")
print("first removed:", removed[:12])