
import sqlite3
import json

DB_PATH = "data/pipeline.db"

PAPER_URL = "https://arxiv.org/abs/2412.08635"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Fetch the original record
row = cursor.execute(
    "SELECT data_json FROM records WHERE source_url = ?",
    (PAPER_URL,)
).fetchone()

if row is None:
    print("Paper record not found. No changes made.")
else:
    data = json.loads(row[0])

    content = data.get("content", {})
    old_repo = content.get("github_url")

    print("Paper:", content.get("title"))
    print("Old repository:", old_repo)

    confirm = input("Remove this unverified GitHub association? (yes/no): ")

    if confirm.lower() == "yes":
        content.pop("github_url", None)

        cursor.execute(
            "UPDATE records SET data_json = ? WHERE source_url = ?",
            (json.dumps(data, ensure_ascii=False), PAPER_URL)
        )

        conn.commit()
        print("Association removed successfully.")
    else:
        print("No changes made.")

conn.close()
