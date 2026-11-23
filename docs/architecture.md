# FrontierAtlas / GraphOne: Ingestion Pipeline Architecture

_Generated 2026-11-23 11:55 UTC. All counts are read from the pipeline database and export files._

## 1. Overview

An **asynchronous Python pipeline (asyncio + aiohttp)** built only from free tools. Flow: sources -> **fetcher** (robots.txt, per-domain adaptive rate limit, retries with jitter) -> **raw store** (SQLite) -> **deterministic extractors** (APIs, RSS, JSON-LD, rules) -> **LLM chain only for unstructured text** (Gemini Flash-Lite, Groq Qwen, Groq GPT-OSS, Gemma) -> **validators** (Pydantic schema, evidence-in-source, label consistency, 24-hour freshness) -> **entity resolver** (exact, alias, domain, core name, limited fuzzy; every decision logged) -> records, Entity Mapping Log and the 6-tab export.

**The rule that decides everything: nothing is stored unless it traces to a fetched source.** `source.url` is mandatory (the database refuses records without it), LLM answers are accepted only if each quoted evidence snippet exists verbatim in the page text, and an unclear answer produces no record.

| Tab | Rows | How it was obtained |
|---|---|---|
| Startups | 4,350 | YC public directory via the yc-oss API; employeeCount = team_size (null if absent); no LLM |
| Products | 780 | company pricing pages: 505 by rules, 275 by the LLM chain with evidence checks |
| Research Papers | 1,200 | Papers with Code archive (official links) + arXiv API + GitHub API stars; no LLM |
| Jobs (last 24 h) | 65 | Greenhouse, Ashby, We Work Remotely, Himalayas, Arbeitnow (real date fields) |
| News (last 24 h) | 21 | TechCrunch, The Verge, Wired, Ars Technica RSS + Hacker News API (publisher dates only) |
| Entity Mapping Log | 4,481 | raw name, canonical name, method and score for every resolved name |

## 2. Scale strategy: 500,000 startups, products and papers without manual intervention

- **Bulk-first sources.** Papers come from an archive of 300,161 paper-to-code links (loaded in seconds) plus the arXiv API in batches of 50 ids at one request per 3 s as arXiv asks: 500,000 papers = 10,000 requests = about 8.3 h on one node. GitHub stars are the bottleneck (5,000 REST calls per hour per token), so production batches many repositories per GraphQL request and rotates tokens (production design).
- **Frontier plus stateless workers.** URLs go to a queue sharded by domain; workers are stateless, idempotent and resumable (progress lives in `crawl_state`, every table has a unique key). Scaling means more workers and a shared Postgres/Redis, not code changes; per-domain token buckets keep politeness constant as workers grow.
- **Cheapest extractor first: API or file, then rules, then LLM.** Of 4,350 startup sites attempted, 505 produced a product by rules alone and 275 more through the LLM chain; the rest had no public pricing page, were blocked or were unclear.
- **LLM cost control.** LLM calls grow with the share of ambiguous pages, not with total pages (1,130 requests, 1,402,156 tokens in this run, on free tiers capped at about 500 requests per day on the first tier). At 500,000 scale the same orchestrator fronts a self-hosted open model with batching; tiers are configuration (`config/llm.json`).
- **Refresh and monitoring (production).** Conditional requests (ETag, If-Modified-Since), per-source `first_seen` watermarks and TTL-based re-checks; per-source yield, 403/429 rates and tier usage are already queryable from `crawl_state` and `llm_usage`, so alerts can fire when a source's yield drops.

## 3. Handling 413s and 429s across thousands of concurrent extractions

- **413 prevention and recovery (`src/llm.py`).** `config/llm.json` holds each model's input budget. `smart_truncate` keeps the opening plus the highest-density chunks (most pricing or entity keywords) in original order, so a 160,000-character page still reaches the model with its price table. A real 413 halves the budget and retries on the same tier (stopping below 200 tokens), then falls to the next tier.
- **429: exponential backoff with full jitter.** The server hint (`retry in 12s`) is used when present, otherwise delay = random(0, min(30 s, 1 s x 2^attempt)). If the wait would exceed 30 s, or the error says the daily quota is gone, the tier is skipped (or marked exhausted for the day) and the next tier answers. Seen in this run: Gemini 429 -> Groq answered; Groq 429 -> next tier.
- **Quota awareness before calling.** Each tier has a sliding one-minute window (requests and tokens, behind a shared lock) and daily counters persisted in `llm_usage`, so many concurrent callers cannot oversubscribe a free tier and a restart does not forget today's usage.
- **Circuit breaker.** Four consecutive 429/5xx/timeouts open a tier for 60 s (seen with Gemma: repeated 503 high-demand errors), so failing tiers stop costing time.
- **Output gates.** A tier's answer must be valid JSON, quote evidence that exists verbatim in the page, and carry a label consistent with that evidence; otherwise the next tier is tried and, if none passes, no record is created.
- **HTTP side (`src/fetcher.py`).** Retries 429/5xx and network errors with the same jittered backoff, honours `Retry-After`, and widens a domain's request interval 1.5x after a 429 (narrowing 10% after each success). Example from the logs: one domain went from 1.0 s to 3.4 s between requests.
- **At thousands of concurrent extractions (production).** Replace the in-process windows by a shared Redis token bucket per tier, put extraction jobs on a queue with backpressure and a dead-letter queue, and keep every task idempotent (unique keys) so retries and duplicate deliveries are safe.

## 4. Freshness tracking: never processing the same article or job twice

- **Canonical key.** Lowercase host, strip `www.` and fragments, drop tracking parameters, sort query parameters, treat http and https alike, then SHA-256.
- **Atomic claim.** `seen_urls.url_hash` is the primary key and `claim_url` is a single `INSERT OR IGNORE`: when several workers race for one URL, exactly one gets True. If processing fails (paywall, timeout) `release_url` removes the claim so a later run retries.
- **Distributed version (production).** The same statement on PostgreSQL (`INSERT ... ON CONFLICT DO NOTHING RETURNING`), optionally with Redis `SET NX EX` or a Bloom filter in front for speed; the unique constraint stays the source of truth.
- **The 24-hour rule.** Date priority: feed or API field, page metadata (JSON-LD, meta tags, `<time>`, URL pattern), visible relative text ('2 hours ago'). Every date carries a precision (`exact`, `day`, `inferred`): a day-precision date only passes if it is today (UTC) and relative values of a day or more never pass. Greenhouse jobs use `first_published`, not `updated_at`, which boards change constantly.
- **Undated sources.** The first run only records a baseline; later runs emit unseen items, flagged `first_seen_heuristic`.
- **Re-checked at export.** `export_sheet.py` drops any News or Jobs row older than 24 hours at export time and leaves out `inferred` news dates, so the sheet itself satisfies the freshness claim.

## 5. Storage strategy

- **Primary database: PostgreSQL** (the trial runs the identical schema on SQLite in WAL mode: one file, zero setup). Records are schema-versioned JSON (JSONB), every dedupe guarantee above is a unique constraint, many writers need real transactions, and range queries on dates are central to freshness.
- **Vector search: pgvector inside the same Postgres** (HNSW indexes) for candidate generation in entity resolution and for similar-article and similar-job lookups: one system to back up and keep consistent. Move to a dedicated vector store only when embeddings outgrow one Postgres node.
- **Graph: a typed edge table in Postgres** (`src_id, dst_id, edge_type, source_url, observed_at`) with recursive CTEs for 2-3 hop questions (startup, product, job, news, paper, repo). Adopt a graph database only if deep traversals dominate; until then it adds a second system and a sync problem.
- **Raw pages in object storage** (S3 or MinIO) addressed by content hash, with only pointer and hash in Postgres. Here `raw_pages` keeps the exact text behind every stored product as audit evidence.

## 6. Anti-bot and JavaScript-heavy sources

- **Policy.** Official APIs, feeds and bulk files first (arXiv, GitHub, yc-oss, Greenhouse/Ashby, RSS); robots.txt honoured for ordinary sites (APIs meant for programs are called directly and documented as such); honest User-Agent with a contact; per-domain rate limiting. No CAPTCHA solving, proxy rotation or fingerprint spoofing.
- **Measured on 4,350 startup sites:** 350 homepages were unreachable (107 blocked by robots.txt, 74 returned 403, the rest DNS, TLS, timeout or other errors). They are logged and skipped, not hammered.
- **JavaScript-rendered pages (`src/sources/render.py`).** Playwright (async Chromium) with a robots check, a 2 s per-domain gap and images/fonts/media blocked; the text then goes through the normal classifier. `detect_block` recognises Cloudflare/DataDome/CAPTCHA challenge pages (the `cf-mitigated` header, challenge markers on a short page, 401/403/429/503) and records `render_blocked:<reason>` instead of trying to defeat them. Results: empty=94, homepage_failed=8, ok=12, unclear=36.
- **Sources that aggressively block (production).** Prefer licensed data or partnerships, cache aggressively, crawl slowly with conditional requests, and treat a challenge as a signal to back off from that domain (the adaptive limiter already widens intervals after 429s).

## 7. Data-quality incidents found by auditing, and the fixes

- Fuzzy name matching produced **4 false merges out of 4** on the YC directory (for example 'Cognition IP' into 'Cognition'), found by comparing website domains; fuzzy matching is now off for curated directories and used only on noisy text.
- Hand-checking samples exposed a competitor comparison page and a homepage add-on price stored as product prices; **249 stored products were re-validated against their saved pages and removed**, and homepage text and comparison pages are no longer accepted as pricing evidence.
- Rule-based classification never assigns `FREE` (lone `$0` strings were fee arithmetic, dashboards and trials); only the evidence-checked LLM step may.
- Greenhouse `updated_at` changes for most jobs, so only `first_published` is used as the publication date.
- Every LLM answer must quote text that exists in the page; `audit_final.py` re-checks this for every stored product and re-validates every record against its schema.
- Hacker News submission time is not the article's publication date, so it is stored as an `inferred` date and left out of the final News tab.

## 8. Known limitations

- Products: 780 rows. Pricing models come from page text, not ground truth; the evidence quotes are in the Products tab so each label can be audited. Free-tier LLM quotas and Gemma capacity errors limited how many ambiguous pages were recovered.
- Startup `employeeCount` is the company's own self-reported YC team size and may be stale; it is null when absent.
- News and jobs are a snapshot of the 24 hours before the final crawl; volume depends on the weekday (the first test runs happened on a Sunday).
- Remotive and RemoteOK were evaluated and excluded: their feeds are delayed by about a day, so they cannot satisfy the 24-hour rule. Himalayas, Arbeitnow and We Work Remotely are general boards filtered by AI keywords in the title, so AI roles with other titles are missed.
- SQLite is a trial-scale choice; section 5 describes the production design.
