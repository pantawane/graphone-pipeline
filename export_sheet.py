"""Step 12: builds the 6-tab workbook (Startups, Products, Research Papers, Jobs, News, Entity Mapping Log)."""
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

DB = "data/pipeline.db"
OUT = Path("output")
NOW = datetime.now(timezone.utc)
CUT = NOW - timedelta(hours=24)
KEEP_INFERRED = "--keep-inferred" in sys.argv
MAX_CELL = 32000
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def load(con, rtype):
    return [json.loads(r[0]) for r in con.execute(
        "SELECT data_json FROM records WHERE record_type=? ORDER BY id", (rtype,))]


def base(d):
    return {"schemaVersion": d["schemaVersion"], "recordType": d["recordType"],
            "source.name": d["source"]["name"], "source.url": d["source"]["url"]}


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def startups(con):
    out = []
    for d in load(con, "STARTUP"):
        c = d["content"]
        data = c["data"]
        out.append({**base(d), "content.entityName": c["entityName"],
                    "content.data.employeeCount": data.get("employeeCount"), "website": data.get("website"),
                    "description": data.get("description"), "yc_ai_tag": data.get("aiRelated"),
                    "collectedAt": d["collectedAt"]})
    return pd.DataFrame(out)


def products(con):
    out = []
    for d in load(con, "PRODUCT"):
        c = d["content"]
        out.append({**base(d), "content.startupName": c["startupName"], "content.pricingModel": c["pricingModel"],
                    "description": c.get("description"), "pricingUrl": c["pricingUrl"],
                    "evidence": " | ".join(c["evidence"]), "method": c.get("method") or "rules",
                    "collectedAt": d["collectedAt"]})
    return pd.DataFrame(out)


def papers(con):
    out = []
    for d in load(con, "RESEARCH_PAPER"):
        c = d["content"]
        out.append({**base(d), "content.title": c["title"], "content.authors": "; ".join(c["authors"]),
                    "content.paper_url": c["paper_url"], "content.github_url": c.get("github_url"),
                    "content.github_stars": c.get("github_stars"), "content.published_date": c["published_date"],
                    "collectedAt": d["collectedAt"]})
    return pd.DataFrame(out)


def jobs(con):
    out, dropped = [], 0
    for d in load(con, "JOB"):
        c = d["content"]
        if iso(c["date"]) < CUT:
            dropped += 1
            continue
        out.append({**base(d), "content.company": c["company"], "content.date": c["date"],
                    "content.is_remote": c["is_remote"], "content.role_family": c["role_family"],
                    "title": c["title"], "location": c.get("location"), "date_source": c["date_source"],
                    "date_confidence": c["date_confidence"], "text_length": c["text_length"],
                    "description": c["description"], "collectedAt": d["collectedAt"]})
    print(f"  jobs older than 24h at export time, dropped: {dropped}")
    return pd.DataFrame(out)


def news(con):
    out, old, inferred = [], 0, 0
    for d in load(con, "NEWS"):
        c = d["content"]
        if iso(c["published_date"]) < CUT:
            old += 1
            continue
        if c["date_confidence"] == "inferred" and not KEEP_INFERRED:
            inferred += 1
            continue
        out.append({**base(d), "content.title": c["title"], "content.published_date": c["published_date"],
                    "date_source": c["date_source"], "date_confidence": c["date_confidence"],
                    "companies": "; ".join(c.get("companies", [])), "text_length": c["text_length"],
                    "full_text": c["full_text"], "collectedAt": d["collectedAt"]})
    print(f"  news older than 24h at export time, dropped: {old}; inferred dates left out: {inferred}")
    return pd.DataFrame(out)


def mapping_log(con):
    rows = con.execute(
        "SELECT record_type, raw_name, canonical_name, method, ROUND(AVG(score), 1), COUNT(*), MIN(created_at) "
        "FROM entity_mapping_log WHERE record_type != 'DEMO' "
        "GROUP BY record_type, raw_name, canonical_name, method "
        "ORDER BY (method = 'new_entity'), record_type, raw_name").fetchall()
    return pd.DataFrame(rows, columns=["recordType", "raw_name", "canonical_name", "method", "score",
                                       "occurrences", "first_seen"])


def clean(df):
    def fix(v):
        if isinstance(v, str):
            return CONTROL.sub("", v)[:MAX_CELL]
        return v
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(fix)
    return df


def main():
    con = sqlite3.connect(DB)
    print(f"export time (UTC): {NOW:%Y-%m-%d %H:%M}   freshness cut-off: {CUT:%Y-%m-%d %H:%M}")
    tabs = {"Startups": startups(con), "Products": products(con), "Research Papers": papers(con),
            "Jobs": jobs(con), "News": news(con), "Entity Mapping Log": mapping_log(con)}

    OUT.mkdir(exist_ok=True)
    path = OUT / "frontier_atlas_output.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, df in tabs.items():
            df = clean(df)
            df.to_excel(writer, sheet_name=name, index=False)
            df.to_csv(OUT / f"{name.replace(' ', '_')}.csv", index=False, encoding="utf-8-sig")

    print("\nrows per tab:")
    warnings = []
    for name, df in tabs.items():
        print(f"  {name:20} {len(df)}")
    for name in ("Startups", "Products", "Research Papers"):
        if len(tabs[name]) < 1000:
            warnings.append(f"{name} has {len(tabs[name])} rows (minimum 1,000)")
    stars = tabs["Research Papers"]["content.github_stars"].notna().sum() if len(tabs["Research Papers"]) else 0
    if stars < 1000:
        warnings.append(f"only {stars} papers have a GitHub star count (minimum 1,000)")
    for name, df in tabs.items():
        if "source.url" in df and len(df):
            if (df["source.url"].isna() | (df["source.url"] == "")).any():
                warnings.append(f"{name}: rows without a source URL")
            dup = int(df["source.url"].duplicated().sum())
            if dup and name != "Products":
                warnings.append(f"{name}: {dup} duplicate source URLs")
    print("\nWARNINGS:" if warnings else "\nno warnings")
    for w in warnings:
        print("  -", w)
    print(f"\nwritten: {path}  (plus one CSV per tab in {OUT}/)")


main()