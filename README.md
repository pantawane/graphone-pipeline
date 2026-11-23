# FrontierAtlas / GraphOne: AI intelligence-graph ingestion pipeline (AI Engineer demo task)

An asynchronous Python pipeline that collects startups, products, research papers, jobs and news, turns them into the
task's canonical schemas, resolves company names to canonical entities and exports the six required tabs.
**Only free tools and free API tiers are used.**

**Core rule:** no record is stored unless it traces to a fetched source URL. LLM output is accepted only if the evidence it
quotes exists verbatim in the source text; otherwise no record is created.

## Results (generated 2026-11-23 03:29 UTC from the database)

| Tab | Rows | Source / method |
|---|---|---|
| Startups | 4,350 | YC public directory via the yc-oss API; employeeCount = team_size (null if absent); no LLM |
| Products | 779 | company pricing pages: 504 by rules, 275 by the LLM chain with evidence checks |
| Research Papers | 1,200 | Papers with Code archive (official links) + arXiv API + GitHub API stars; no LLM |
| Jobs (last 24 h) | 49 | Greenhouse, Ashby, We Work Remotely, Himalayas, Arbeitnow (real date fields) |
| News (last 24 h) | 17 | TechCrunch, The Verge, Wired, Ars Technica RSS + Hacker News API (publisher dates only) |
| Entity Mapping Log | 4,460 | raw name, canonical name, method and score for every resolved name |

Design, scale strategy and the 413/429, freshness and storage answers: [`architecture.pdf`](architecture.pdf)
(source: [`docs/architecture.md`](docs/architecture.md)).

## Quick start

Requirements: Python 3.12+, Git, and free accounts for Google AI Studio (Gemini API key), Groq (API key) and GitHub
(personal access token with **no scopes**).

```bash
git clone <this repository> && cd <repository folder>
python -m venv .venv
.venv\Scripts\activate            # macOS / Linux: source .venv/bin/activate
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
| Startups | `python run_startups.py --all` | STARTUP records (YC directory, `aiRelated` flag) |
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

## Known limitations

See section 8 of [`architecture.pdf`](architecture.pdf): pricing models are classifications of page text (evidence quotes are in the
Products tab), startup team sizes are self-reported, and news/jobs volume depends on the weekday of the final crawl.
