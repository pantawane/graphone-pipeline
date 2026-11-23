import json
import sqlite3

from src.sources.jobs import is_ai_job

GENERAL = ("We Work Remotely", "Himalayas", "Arbeitnow")
c = sqlite3.connect("data/pipeline.db")
rows = c.execute("SELECT id, data_json FROM records WHERE record_type='JOB'").fetchall()
removed = 0
for rid, dj in rows:
    d = json.loads(dj)
    if d["source"]["name"] in GENERAL and not is_ai_job(d["content"]["title"]):
        c.execute("DELETE FROM records WHERE id=?", (rid,))
        removed += 1
c.commit()
print(f"checked {len(rows)} jobs, removed {removed} general-board jobs without an AI keyword in the title")