import sqlite3

NAME = "Hacker News /newest (relative dates)"
c = sqlite3.connect("data/pipeline.db")
n1 = c.execute("DELETE FROM records WHERE record_type='NEWS' AND source_name=?", (NAME,)).rowcount
n2 = c.execute("DELETE FROM seen_urls WHERE source=?", (NAME,)).rowcount
c.commit()
print(f"removed {n1} records and {n2} seen-URL claims for: {NAME}")