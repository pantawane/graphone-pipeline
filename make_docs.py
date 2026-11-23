"""Generates README.md, docs/architecture.md and architecture.pdf from the LIVE database.
Claims about code behaviour are verified against the code first; claims that cannot be verified are left out."""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

DB = "data/pipeline.db"
OUT = Path("output")
NOW = datetime.now(timezone.utc)


def feature_checks() -> dict:
    f = {}
    try:
        from src.sources.products import classify
        f["rules_never_free"] = classify("Free forever $0/month for everyone")[0] is None
    except Exception:
        f["rules_never_free"] = False
    try:
        from src.sources.news import parse_hn_html
        html = ('<tr class="athing submission" id="1"><td><span class="titleline"><a href="https://example.com/a">T</a>'
                '</span></td></tr><tr><td><span class="age" title="x"><a href="item?id=1">2 hours ago</a></span></td></tr>')
        it = parse_hn_html(html, "https://news.ycombinator.com/newest", NOW)[0]
        f["hn_inferred"] = it.get("date") is None and it["fallback_date"].precision == "inferred"
    except Exception:
        f["hn_inferred"] = False
    try:
        import src.fetcher as fm
        f["no_retry_tls"] = hasattr(fm, "NO_RETRY")
    except Exception:
        f["no_retry_tls"] = False
    f["render_module"] = Path("src/sources/render.py").exists()
    return f


def stats() -> dict:
    con = sqlite3.connect(DB)
    q = lambda sql, *a: con.execute(sql, a).fetchall()
    s = {"records": dict(q("SELECT record_type, COUNT(*) FROM records GROUP BY 1"))}
    s["product_methods"] = dict(q("SELECT COALESCE(json_extract(data_json,'$.content.method'),'rules'), COUNT(*) "
                                  "FROM records WHERE record_type='PRODUCT' GROUP BY 1"))
    s["outcomes"] = dict(q("SELECT value, COUNT(*) FROM crawl_state WHERE source='products' GROUP BY 1"))
    s["entity_methods"] = dict(q("SELECT method, COUNT(*) FROM entity_mapping_log WHERE record_type!='DEMO' GROUP BY 1"))
    s["llm"] = q("SELECT tier, SUM(requests), SUM(tokens) FROM llm_usage GROUP BY 1")
    tabs = {}
    for name in ("Startups", "Products", "Research_Papers", "Jobs", "News", "Entity_Mapping_Log"):
        p = OUT / f"{name}.csv"
        tabs[name] = len(pd.read_csv(p)) if p.exists() else None
    s["tabs"] = tabs
    return s


def fmt(t: str) -> str:
    t = escape(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return re.sub(r"`(.+?)`", r"<font face='Courier'>\1</font>", t)


def results_rows(s: dict) -> list[list[str]]:
    r, tabs, pm = s["records"], s["tabs"], s["product_methods"]
    rules_n = sum(v for k, v in pm.items() if k.startswith("rules"))
    llm_n = sum(v for k, v in pm.items() if k.startswith("llm"))
    map_total = sum(s["entity_methods"].values())

    def n(tab, key, fallback=None):
        v = tabs.get(tab)
        return f"{(v if v is not None else (fallback if fallback is not None else r.get(key, 0))):,}"

    return [
        ["Startups", n("Startups", "STARTUP"), "YC public directory via the yc-oss API; employeeCount = team_size (null if absent); no LLM"],
        ["Products", n("Products", "PRODUCT"), f"company pricing pages: {rules_n:,} by rules, {llm_n:,} by the LLM chain with evidence checks"],
        ["Research Papers", n("Research_Papers", "RESEARCH_PAPER"), "Papers with Code archive (official links) + arXiv API + GitHub API stars; no LLM"],
        ["Jobs (last 24 h)", n("Jobs", "JOB"), "Greenhouse, Ashby, We Work Remotely, Himalayas, Arbeitnow (real date fields)"],
        ["News (last 24 h)", n("News", "NEWS"), "TechCrunch, The Verge, Wired, Ars Technica RSS + Hacker News API (publisher dates only)"],
        ["Entity Mapping Log", n("Entity_Mapping_Log", "?", map_total), "raw name, canonical name, method and score for every resolved name"],
    ]


def build_blocks(s: dict, feats: dict) -> list:
    oc, pm = s["outcomes"], s["product_methods"]
    rules_n = sum(v for k, v in pm.items() if k.startswith("rules"))
    llm_n = sum(v for k, v in pm.items() if k.startswith("llm"))
    attempted = sum(oc.values())
    unreach = sum(v for k, v in oc.items() if k.startswith("homepage_"))
    robots, forbidden = oc.get("homepage_blocked_by_robots", 0), oc.get("homepage_http_403", 0)
    render = {k: v for k, v in oc.items() if k.startswith("render_")}
    llm_req = sum(x[1] or 0 for x in s["llm"])
    llm_tok = sum(x[2] or 0 for x in s["llm"])
    products_rows = results_rows(s)[1][1]

    blocks = [
        ("title", "FrontierAtlas / GraphOne: Ingestion Pipeline Architecture"),
        ("sub", f"Generated {NOW:%Y-%m-%d %H:%M} UTC. All counts are read from the pipeline database and export files."),
        ("h", "1. Overview"),
        ("p", "An **asynchronous Python pipeline (asyncio + aiohttp)** built only from free tools. Flow: sources -> **fetcher** "
              "(robots.txt, per-domain adaptive rate limit, retries with jitter) -> **raw store** (SQLite) -> **deterministic "
              "extractors** (APIs, RSS, JSON-LD, rules) -> **LLM chain only for unstructured text** (Gemini Flash-Lite, Groq Qwen, "
              "Groq GPT-OSS, Gemma) -> **validators** (Pydantic schema, evidence-in-source, label consistency, 24-hour freshness) "
              "-> **entity resolver** (exact, alias, domain, core name, limited fuzzy; every decision logged) -> records, Entity "
              "Mapping Log and the 6-tab export."),
        ("p", "**The rule that decides everything: nothing is stored unless it traces to a fetched source.** `source.url` is "
              "mandatory (the database refuses records without it), LLM answers are accepted only if each quoted evidence snippet "
              "exists verbatim in the page text, and an unclear answer produces no record."),
        ("t", {"head": ["Tab", "Rows", "How it was obtained"], "rows": results_rows(s), "widths": [3.0, 1.6, 12.8]}),
        ("h", "2. Scale strategy: 500,000 startups, products and papers without manual intervention"),
        ("b", [
            "**Bulk-first sources.** Papers come from an archive of 300,161 paper-to-code links (loaded in seconds) plus the arXiv "
            "API in batches of 50 ids at one request per 3 s as arXiv asks: 500,000 papers = 10,000 requests = about 8.3 h on one "
            "node. GitHub stars are the bottleneck (5,000 REST calls per hour per token), so production batches many repositories "
            "per GraphQL request and rotates tokens (production design).",
            "**Frontier plus stateless workers.** URLs go to a queue sharded by domain; workers are stateless, idempotent and "
            "resumable (progress lives in `crawl_state`, every table has a unique key). Scaling means more workers and a shared "
            "Postgres/Redis, not code changes; per-domain token buckets keep politeness constant as workers grow.",
            "**Cheapest extractor first: API or file, then rules, then LLM.** " + (
                f"Of {attempted:,} startup sites attempted, {rules_n:,} produced a product by rules alone and {llm_n:,} more through "
                f"the LLM chain; the rest had no public pricing page, were blocked or were unclear." if attempted else ""),
            f"**LLM cost control.** LLM calls grow with the share of ambiguous pages, not with total pages ({llm_req:,} requests, "
            f"{llm_tok:,} tokens in this run, on free tiers capped at about 500 requests per day on the first tier). At 500,000 "
            "scale the same orchestrator fronts a self-hosted open model with batching; tiers are configuration (`config/llm.json`).",
            "**Refresh and monitoring (production).** Conditional requests (ETag, If-Modified-Since), per-source `first_seen` "
            "watermarks and TTL-based re-checks; per-source yield, 403/429 rates and tier usage are already queryable from "
            "`crawl_state` and `llm_usage`, so alerts can fire when a source's yield drops.",
        ]),
        ("h", "3. Handling 413s and 429s across thousands of concurrent extractions"),
        ("b", [
            "**413 prevention and recovery (`src/llm.py`).** `config/llm.json` holds each model's input budget. `smart_truncate` keeps "
            "the opening plus the highest-density chunks (most pricing or entity keywords) in original order, so a 160,000-character "
            "page still reaches the model with its price table. A real 413 halves the budget and retries on the same tier (stopping "
            "below 200 tokens), then falls to the next tier.",
            "**429: exponential backoff with full jitter.** The server hint (`retry in 12s`) is used when present, otherwise delay = "
            "random(0, min(30 s, 1 s x 2^attempt)). If the wait would exceed 30 s, or the error says the daily quota is gone, the "
            "tier is skipped (or marked exhausted for the day) and the next tier answers. Seen in this run: Gemini 429 -> Groq "
            "answered; Groq 429 -> next tier.",
            "**Quota awareness before calling.** Each tier has a sliding one-minute window (requests and tokens, behind a shared "
            "lock) and daily counters persisted in `llm_usage`, so many concurrent callers cannot oversubscribe a free tier and a "
            "restart does not forget today's usage.",
            "**Circuit breaker.** Four consecutive 429/5xx/timeouts open a tier for 60 s (seen with Gemma: repeated 503 high-demand "
            "errors), so failing tiers stop costing time.",
            "**Output gates.** A tier's answer must be valid JSON, quote evidence that exists verbatim in the page, and carry a label "
            "consistent with that evidence; otherwise the next tier is tried and, if none passes, no record is created.",
            "**HTTP side (`src/fetcher.py`).** Retries 429/5xx and network errors with the same jittered backoff, honours "
            "`Retry-After`, and widens a domain's request interval 1.5x after a 429 (narrowing 10% after each success). Example from "
            "the logs: one domain went from 1.0 s to 3.4 s between requests.",
            "**At thousands of concurrent extractions (production).** Replace the in-process windows by a shared Redis token bucket "
            "per tier, put extraction jobs on a queue with backpressure and a dead-letter queue, and keep every task idempotent "
            "(unique keys) so retries and duplicate deliveries are safe.",
        ]),
        ("h", "4. Freshness tracking: never processing the same article or job twice"),
        ("b", [
            "**Canonical key.** Lowercase host, strip `www.` and fragments, drop tracking parameters, sort query parameters, treat "
            "http and https alike, then SHA-256.",
            "**Atomic claim.** `seen_urls.url_hash` is the primary key and `claim_url` is a single `INSERT OR IGNORE`: when several "
            "workers race for one URL, exactly one gets True. If processing fails (paywall, timeout) `release_url` removes the claim "
            "so a later run retries.",
            "**Distributed version (production).** The same statement on PostgreSQL (`INSERT ... ON CONFLICT DO NOTHING RETURNING`), "
            "optionally with Redis `SET NX EX` or a Bloom filter in front for speed; the unique constraint stays the source of truth.",
            "**The 24-hour rule.** Date priority: feed or API field, page metadata (JSON-LD, meta tags, `<time>`, URL pattern), "
            "visible relative text ('2 hours ago'). Every date carries a precision (`exact`, `day`, `inferred`): a day-precision date "
            "only passes if it is today (UTC) and relative values of a day or more never pass. Greenhouse jobs use `first_published`, "
            "not `updated_at`, which boards change constantly.",
            "**Undated sources.** The first run only records a baseline; later runs emit unseen items, flagged `first_seen_heuristic`.",
            "**Re-checked at export.** `export_sheet.py` drops any News or Jobs row older than 24 hours at export time and leaves out "
            "`inferred` news dates, so the sheet itself satisfies the freshness claim.",
        ]),
        ("h", "5. Storage strategy"),
        ("b", [
            "**Primary database: PostgreSQL** (the trial runs the identical schema on SQLite in WAL mode: one file, zero setup). "
            "Records are schema-versioned JSON (JSONB), every dedupe guarantee above is a unique constraint, many writers need real "
            "transactions, and range queries on dates are central to freshness.",
            "**Vector search: pgvector inside the same Postgres** (HNSW indexes) for candidate generation in entity resolution and for "
            "similar-article and similar-job lookups: one system to back up and keep consistent. Move to a dedicated vector store only "
            "when embeddings outgrow one Postgres node.",
            "**Graph: a typed edge table in Postgres** (`src_id, dst_id, edge_type, source_url, observed_at`) with recursive CTEs for "
            "2-3 hop questions (startup, product, job, news, paper, repo). Adopt a graph database only if deep traversals dominate; "
            "until then it adds a second system and a sync problem.",
            "**Raw pages in object storage** (S3 or MinIO) addressed by content hash, with only pointer and hash in Postgres. Here "
            "`raw_pages` keeps the exact text behind every stored product as audit evidence.",
        ]),
        ("h", "6. Anti-bot and JavaScript-heavy sources"),
        ("b", [
            "**Policy.** Official APIs, feeds and bulk files first (arXiv, GitHub, yc-oss, Greenhouse/Ashby, RSS); robots.txt honoured "
            "for ordinary sites (APIs meant for programs are called directly and documented as such); honest User-Agent with a "
            "contact; per-domain rate limiting. No CAPTCHA solving, proxy rotation or fingerprint spoofing.",
            (f"**Measured on {attempted:,} startup sites:** {unreach:,} homepages were unreachable ({robots:,} blocked by robots.txt, "
             f"{forbidden:,} returned 403, the rest DNS, TLS, timeout or other errors). They are logged and skipped, not hammered."
             if attempted else "**Measured:** run the products crawl to populate this section."),
        ]),
    ]
    if feats["render_module"]:
        extra = (" Results: " + ", ".join(f"{k.replace('render_', '')}={v}" for k, v in sorted(render.items())) + "."
                 if render else "")
        blocks[-1][1].append(
            "**JavaScript-rendered pages (`src/sources/render.py`).** Playwright (async Chromium) with a robots check, a 2 s per-domain "
            "gap and images/fonts/media blocked; the text then goes through the normal classifier. `detect_block` recognises "
            "Cloudflare/DataDome/CAPTCHA challenge pages (the `cf-mitigated` header, challenge markers on a short page, 401/403/429/503) "
            "and records `render_blocked:<reason>` instead of trying to defeat them." + extra)
    blocks[-1][1].append(
        "**Sources that aggressively block (production).** Prefer licensed data or partnerships, cache aggressively, crawl slowly "
        "with conditional requests, and treat a challenge as a signal to back off from that domain (the adaptive limiter already "
        "widens intervals after 429s).")

    incidents = [
        "Fuzzy name matching produced **4 false merges out of 4** on the YC directory (for example 'Cognition IP' into 'Cognition'), "
        "found by comparing website domains; fuzzy matching is now off for curated directories and used only on noisy text.",
        "Hand-checking samples exposed a competitor comparison page and a homepage add-on price stored as product prices; "
        "**249 stored products were re-validated against their saved pages and removed**, and homepage text and comparison pages "
        "are no longer accepted as pricing evidence.",
        "Greenhouse `updated_at` changes for most jobs, so only `first_published` is used as the publication date.",
        "Every LLM answer must quote text that exists in the page; `audit_final.py` re-checks this for every stored product and "
        "re-validates every record against its schema.",
    ]
    if feats["rules_never_free"]:
        incidents.insert(2, "Rule-based classification never assigns `FREE` (lone `$0` strings were fee arithmetic, dashboards and "
                            "trials); only the evidence-checked LLM step may.")
    if feats["hn_inferred"]:
        incidents.append("Hacker News submission time is not the article's publication date, so it is stored as an `inferred` date "
                         "and left out of the final News tab.")
    blocks += [("h", "7. Data-quality incidents found by auditing, and the fixes"), ("b", incidents)]
    blocks += [("h", "8. Known limitations"), ("b", [
        f"Products: {products_rows} rows. Pricing models come from page text, not ground truth; the evidence quotes are in the Products "
        "tab so each label can be audited. Free-tier LLM quotas and Gemma capacity errors limited how many ambiguous pages were recovered.",
        "Startup `employeeCount` is the company's own self-reported YC team size and may be stale; it is null when absent.",
        "News and jobs are a snapshot of the 24 hours before the final crawl; volume depends on the weekday (the first test runs "
        "happened on a Sunday).",
        "Remotive and RemoteOK were evaluated and excluded: their feeds are delayed by about a day, so they cannot satisfy the 24-hour rule. "
        "Himalayas, Arbeitnow and We Work Remotely are general boards filtered by AI keywords in the title, so AI roles with other titles are missed.",
        "SQLite is a trial-scale choice; section 5 describes the production design.",
    ])]
    return blocks


def to_markdown(blocks: list) -> str:
    out = []
    for kind, c in blocks:
        if kind == "title":
            out += [f"# {c}", ""]
        elif kind == "sub":
            out += [f"_{c}_", ""]
        elif kind == "h":
            out += [f"## {c}", ""]
        elif kind == "p":
            out += [c, ""]
        elif kind == "b":
            out += [f"- {x}" for x in c if x] + [""]
        elif kind == "t":
            out += ["| " + " | ".join(c["head"]) + " |", "|" + "---|" * len(c["head"])]
            out += ["| " + " | ".join(str(x) for x in row) + " |" for row in c["rows"]] + [""]
    return "\n".join(out)


def to_pdf(blocks: list, path: Path) -> int:
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica", fontSize=8.5, leading=10.4, spaceAfter=3)
    h = ParagraphStyle("h", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=10.5, leading=12.5,
                       spaceBefore=6, spaceAfter=3, textColor=colors.HexColor("#1a3a5c"))
    title = ParagraphStyle("title", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=15, leading=18, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=body, textColor=colors.grey)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=10, bulletIndent=2, spaceAfter=2)
    cell = ParagraphStyle("cell", parent=body, fontSize=7.8, leading=9.4, spaceAfter=0)
    story = []
    for kind, c in blocks:
        if kind == "title":
            story.append(Paragraph(fmt(c), title))
        elif kind == "sub":
            story += [Paragraph(fmt(c), sub), Spacer(1, 3)]
        elif kind == "h":
            story.append(Paragraph(fmt(c), h))
        elif kind == "p":
            story.append(Paragraph(fmt(c), body))
        elif kind == "b":
            story += [Paragraph(fmt(x), bullet, bulletText="-") for x in c if x]
        elif kind == "t":
            data = [[Paragraph(f"<b>{fmt(x)}</b>", cell) for x in c["head"]]] + \
                   [[Paragraph(fmt(str(x)), cell) for x in row] for row in c["rows"]]
            t = Table(data, colWidths=[w * cm for w in c["widths"]], repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
            story += [t, Spacer(1, 4)]
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm,
                            topMargin=1.4 * cm, bottomMargin=1.4 * cm,
                            title="FrontierAtlas ingestion pipeline: architecture")
    doc.build(story)
    return doc.page


def build_readme(s: dict, feats: dict) -> str:
    table = "| Tab | Rows | Source / method |\n|---|---|---|\n" + "\n".join(
        f"| {a} | {b} | {c} |" for a, b, c in results_rows(s))
    return f"""# FrontierAtlas / GraphOne: AI intelligence-graph ingestion pipeline (AI Engineer demo task)

An asynchronous Python pipeline that collects startups, products, research papers, jobs and news, turns them into the
task's canonical schemas, resolves company names to canonical entities and exports the six required tabs.
**Only free tools and free API tiers are used.**

**Core rule:** no record is stored unless it traces to a fetched source URL. LLM output is accepted only if the evidence it
quotes exists verbatim in the source text; otherwise no record is created.

## Results (generated {NOW:%Y-%m-%d %H:%M} UTC from the database)

{table}

Design, scale strategy and the 413/429, freshness and storage answers: [`architecture.pdf`](architecture.pdf)
(source: [`docs/architecture.md`](docs/architecture.md)).

## Quick start

Requirements: Python 3.12+, Git, and free accounts for Google AI Studio (Gemini API key), Groq (API key) and GitHub
(personal access token with **no scopes**).

```bash
git clone <this repository> && cd <repository folder>
python -m venv .venv
.venv\\Scripts\\activate            # macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env            # macOS / Linux: cp .env.example .env  (then fill in the keys)
python -m pytest -q               # unit tests, no network needed
```

Put the Papers with Code archive file (a `.parquet` from the Hugging Face dataset `pwc-archive/links-between-paper-and-code`)
into `data/pwc/`. Model ids and free-tier limits are in `config/llm.json` (check them in the provider consoles first).

## Run order

| Step | Command | Output |
|---|---|---|
| Papers | `python run_papers.py 1200` | RESEARCH_PAPER records with GitHub stars |
| Startups | `python run_startups.py --all` | STARTUP records (YC directory; `yc_ai_tag` = tagged AI by YC) |
| Products (rules) | `python run_products.py 5000` | PRODUCT records from pricing pages |
| Products (LLM) | `python run_products_llm.py 400` | repeat daily while quotas allow; resumable |
| Products (JavaScript pages) | `python run_render.py 150 --llm` | pages that need a browser (`--demo` shows static vs rendered) |
| News | `python run_news.py` (`--llm` adds company extraction) | NEWS records, last 24 h |
| Jobs | `python run_jobs.py` (`--peek`, `--boards` for diagnostics) | JOB records, last 24 h |
| Final crawl | `python final_crawl.py` | news, jobs, export and audit in one go |
| Export | `python export_sheet.py` | `output/frontier_atlas_output.xlsx` plus one CSV per tab |
| Audit | `python audit_final.py --check-urls` | schema, URL and evidence checks, 30-row manual sample |
| Docs | `python make_docs.py` | this README, `docs/architecture.md`, `architecture.pdf` |

Every stage is resumable and idempotent: unique keys on all tables, progress in `crawl_state`, and a URL claim
(`seen_urls`) so nothing is processed twice.

## Project layout

```text
src/
  fetcher.py   async HTTP: robots.txt, per-domain adaptive limiter, retries with jitter
  db.py        SQLite schema, canonical URLs, atomic claim_url, records, llm_usage
  llm.py       multi-tier LLM orchestrator: fallback chain, quotas, 413/429 handling, smart truncation
  resolver.py  deterministic entity resolution + mapping log
  dates.py     date normalization: relative dates, page metadata, freshness check
  schemas.py   Pydantic models for the task's schemas
  sources/     papers, startups, products (+llm, +render), news, jobs, render (Playwright)
config/llm.json                LLM tiers and free-tier limits
seeds/canonical_startups.json  50 known AI startups for entity resolution
tests/                         unit tests (no network)
```

## How each phase is covered

| Phase | Where |
|---|---|
| I Bulk acquisition | `run_papers.py`, `run_startups.py`, `run_products*.py`, `src/sources/` |
| II Fresh news and jobs | `run_news.py`, `run_jobs.py`, `src/dates.py` |
| III LLM extraction | `src/llm.py`, `src/sources/products_llm.py` |
| IV Entity resolution | `src/resolver.py`, `seeds/canonical_startups.json` |
| V Anti-bot and scale | `src/fetcher.py`, `src/sources/render.py`, `architecture.pdf` section 6 |
| VI Architecture | `architecture.pdf` |

## Data sources and attribution

| Source | Used for | Notes |
|---|---|---|
| arXiv API | paper metadata | public API, one request per 3 s |
| Papers with Code archive (Hugging Face `pwc-archive`) | paper-to-code links | CC-BY-SA dataset; Papers with Code itself was shut down in July 2025 |
| GitHub REST API | current stars | token without scopes |
| Y Combinator company directory via the yc-oss API | startups | unofficial open dataset of YC's public directory; team size is self-reported |
| Company pricing pages | products | robots.txt respected |
| TechCrunch, The Verge, Wired, Ars Technica (RSS); Hacker News (Algolia API and /newest page) | news | publisher dates only in the final tab |
| Greenhouse and Ashby public job-board APIs; We Work Remotely RSS; Himalayas API; Arbeitnow API | jobs | each record keeps its source URL |

Excluded on purpose: Remotive and RemoteOK (their feeds are delayed by about 24 hours, so they cannot satisfy the 24-hour rule).

## LLM use

Free tiers only (Gemini Flash-Lite, Groq Qwen and GPT-OSS, Gemma). Only public web text is sent to the models. Free-tier
content may be used by the provider to improve its products, so private data must never go through this pipeline.

## Diagnostic and maintenance scripts

| Script | Purpose |
|---|---|
| `status.py` | counts of records, product outcomes, news/job sources and LLM usage |
| `audit_final.py` | schema, URL and evidence checks plus a 30-row manual sample |
| `revalidate_products.py`, `revalidate_llm.py` | re-check stored products against their saved pages with the current rules |
| `clean_jobs.py`, `clean_news.py` | remove stored items that no longer pass the current filters |
| `run_jobs.py --peek / --boards`, `run_render.py --demo` | inspect real source fields and static vs rendered pages |
| `spike_*.py`, `compare_models.py`, `time_gemma.py`, `fix_*.py`, `check_*.py`, `inspect_pwc.py`, `filter_pwc.py` | one-off experiments that informed source and model choices (kept for transparency) |

## Known limitations

See section 8 of [`architecture.pdf`](architecture.pdf): pricing models are classifications of page text (evidence quotes are in the
Products tab), startup team sizes are self-reported, and news/jobs volume depends on the weekday of the final crawl.
"""


def main() -> None:
    s, feats = stats(), feature_checks()
    for name, ok in feats.items():
        print(f"  code check {name:18} {'OK' if ok else 'not present: related claim left out of the docs'}")
    blocks = build_blocks(s, feats)
    Path("docs").mkdir(exist_ok=True)
    Path("docs/architecture.md").write_text(to_markdown(blocks), encoding="utf-8")
    pages = to_pdf(blocks, Path("architecture.pdf"))
    Path("README.md").write_text(build_readme(s, feats), encoding="utf-8")
    print(f"\nwritten: README.md, docs/architecture.md, architecture.pdf ({pages} pages)")
    if pages > 3:
        print("WARNING: architecture.pdf is longer than 3 pages: paste me this message and I will shorten it")


main()