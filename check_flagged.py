import json

names = {"Cognition IP", "Cognitio Labs", "Poka Labs", "Lumari", "Jasper.ai"}
rows = json.load(open("data/yc_ai_startups.json", encoding="utf-8"))
for c in rows:
    if c.get("name") in names:
        print(c["name"], "|", c.get("website"), "|", c.get("one_liner"), "|", c.get("url"))