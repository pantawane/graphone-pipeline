import json
import sqlite3

from src.sources.news import is_ai_article

c = sqlite3.connect("data/pipeline.db")
rows = c.execute("SELECT id, data_json FROM records WHERE record_type='NEWS' AND source_name LIKE 'Hacker News%'").fetchall()
removed = 0
for rid, dj in rows:
    cnt = json.loads(dj)["content"]
    if not is_ai_article(cnt["title"], cnt["full_text"]):
        c.execute("DELETE FROM records WHERE id=?", (rid,))
        removed += 1
c.commit()
print(f"checked {len(rows)} Hacker News articles, removed {removed} that are not about AI")