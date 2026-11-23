"""Jobs vertical (Step 11): fresh (<24h) jobs from 5 sources with real dates, full text, canonical companies,
a remote flag and a role family."""
import asyncio
import calendar
import logging
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape

import feedparser
from pydantic import ValidationError

from src.dates import ParsedDate, is_fresh, parse_absolute, parse_datetime
from src.db import Database, url_hash, utc_now_iso
from src.fetcher import Fetcher
from src.resolver import EntityResolver
from src.schemas import JobContent, JobRecord, Source
from src.sources.news import fetch_article, html_to_text

log = logging.getLogger("jobs")

# Board token -> company name. Ones marked "verify" were guessed from the token: run `run_jobs.py --boards`.
GREENHOUSE = {
    "anthropic": "Anthropic", "databricks": "Databricks", "scaleai": "Scale AI", "xai": "xAI",
    "togetherai": "Together AI", "stabilityai": "Stability AI", "labelbox": "Labelbox", "snorkelai": "Snorkel AI",
    "lightningai": "Lightning AI", "assemblyai": "AssemblyAI", "inflectionai": "Inflection AI",
    "descript": "Descript", "brightai": "Bright AI",                     # verify: brightai
}
ASHBY = {
    "elevenlabs": "ElevenLabs", "cohere": "Cohere", "perplexity": "Perplexity AI", "replit": "Replit",
    "cognition": "Cognition", "cerebras": "Cerebras", "langchain": "LangChain", "llamaindex": "LlamaIndex",
    "pinecone": "Pinecone", "weaviate": "Weaviate", "anyscale": "Anyscale", "modal": "Modal", "writer": "Writer",
    "pika": "Pika", "ideogram": "Ideogram", "suno": "Suno", "baseten": "Baseten", "sierra": "Sierra",
    "synthesia": "Synthesia", "poolside": "Poolside", "reka": "Reka", "deepgram": "Deepgram",
    "character": "Character.AI",   # verify: character, magic, gradient
}

AI_RE = re.compile(
    r"\b(?:ai|a\.i\.|artificial intelligence|machine learning|ml|mlops|deep learning|llms?|nlp|computer vision|"
    r"generative|genai|gen ai|data scientist|prompt|agentic|rag|neural|reinforcement learning)\b", re.I)

ROLE_RULES = [
    ("Sales", r"\bsales\b|account executive|\bae\b|business development|\bbdr\b|\bsdr\b|solutions? (?:engineer|architect|consultant)|partnerships?\b"),
    ("Design", r"\bdesign(?:er)?\b|\bux\b|\bui\b|creative"),
    ("Data", r"\bdata (?:scientist|analyst)\b|analytics|business intelligence|\bbi\b"),
    ("Research", r"\bresearch(?:er)?\b|\bscientist\b"),
    ("Security", r"\bsecurity\b|\binfosec\b|trust (?:&|and) safety"),
    ("Product", r"product (?:manager|management|lead|owner)|head of product|\bpm\b"),
    ("Engineering", r"engineer|developer|\bsre\b|devops|software|backend|front[- ]?end|full[- ]?stack|infrastructure|architect|firmware|\bswe\b|\bmlops\b|machine learning|platform|entwickler|ingenieur|programmierer"),
    ("Marketing", r"marketing|\bseo\b|content (?:writer|strategist|lead)|brand|communications|growth"),
    ("Customer Success", r"customer (?:success|support|experience)|\bsupport\b|implementation|onboarding"),
    ("Recruiting", r"recruit|talent|\bpeople\b|human resources|\bhr\b"),
    ("Finance", r"financ|accountant|accounting|controller|payroll|treasury|\btax\b"),
    ("Legal", r"\blegal\b|counsel|compliance|paralegal|privacy"),
    ("Operations", r"operations|program manager|project manager|chief of staff|office manager|strategy|procurement|supply chain|\bops\b|administrative|executive assistant"),
]
ROLE_RULES = [(fam, re.compile(rx, re.I)) for fam, rx in ROLE_RULES]


def role_family(title: str, department: str = "") -> str:
    for text in (title, department):                       # the title decides first, the department is a fallback
        for fam, rx in ROLE_RULES:
            if text and rx.search(text):
                return fam
    return "Other"


def is_ai_job(title: str, tags: str = "") -> bool:
    return bool(AI_RE.search(f"{title} {tags}"))


def looks_remote(*texts) -> bool:
    return any(re.search(r"\bremote\b|work from home|\bwfh\b", t or "", re.I) for t in texts)


def _item(company, title, url, date, location="", is_remote=None, department="", description="", tags="", detail=None):
    return {"company": company, "title": title, "url": url, "date": date, "location": location or "",
            "is_remote": is_remote, "department": department or "", "description": description or "",
            "tags": tags or "", "detail": detail}


def greenhouse_item(j: dict, token: str, company: str):
    url, title = j.get("absolute_url"), j.get("title")
    if not url or not title:
        return None
    p = parse_absolute(j.get("first_published"))            # never updated_at: boards touch it constantly
    pd = ParsedDate(p.dt, p.precision, "greenhouse:first_published") if p else None
    loc = j.get("location")
    loc = loc.get("name", "") if isinstance(loc, dict) else (loc or "")
    # return _item(company, title, url, pd, loc, detail=("greenhouse", token, j.get("id")))
    meta = j.get("metadata") or []
    loc_type = next((m.get("value") for m in meta if isinstance(m, dict) and m.get("name") == "Location Type"), None)
    remote = None if loc_type is None else ("remote" in str(loc_type).lower())
    return _item(company, title, url, pd, loc, remote, detail=("greenhouse", token, j.get("id")))


def ashby_item(j: dict, token: str, company: str):
    if j.get("isListed") is False:
        return None
    url, title = j.get("jobUrl") or j.get("applyUrl"), j.get("title")
    if not url or not title:
        return None
    p = parse_absolute(j.get("publishedAt"))
    pd = ParsedDate(p.dt, p.precision, "ashby:publishedAt") if p else None
    loc = j.get("location")
    loc = loc.get("name", "") if isinstance(loc, dict) else (loc or "")
    desc = j.get("descriptionPlain") or html_to_text(unescape(j.get("descriptionHtml") or ""))
    remote = j.get("isRemote")
    return _item(company, title, url, pd, loc, None if remote is None else bool(remote),
                 str(j.get("department") or j.get("team") or ""), desc)


def split_wwr_title(raw: str):
    raw = " ".join((raw or "").split())
    if ":" in raw:
        company, title = raw.split(":", 1)
        return company.strip(), title.strip()
    return "", raw


def wwr_item(e):
    company, title = split_wwr_title(e.get("title"))
    url = e.get("link")
    if not url or not title or not company:
        return None
    st = e.get("published_parsed")
    pd = ParsedDate(datetime.fromtimestamp(calendar.timegm(st), timezone.utc), "exact", "rss:published") if st else None
    content = e.get("content")
    html = content[0].get("value", "") if content else (e.get("summary") or "")
    tags = " ".join(t.get("term", "") for t in e.get("tags", []) if isinstance(t, dict))
    return _item(company, title, url, pd, str(e.get("region") or ""), True, "", html_to_text(unescape(html)), tags)


def himalayas_item(j: dict):
    url = next((u for u in (j.get("guid"), j.get("url"), j.get("link"), j.get("applicationLink"))
                if isinstance(u, str) and u.startswith("http")), None)
    title, company = j.get("title"), j.get("companyName") or j.get("company")
    if not url or not title or not company:
        return None
    p = parse_datetime(j.get("pubDate") or j.get("publishedAt"))
    pd = ParsedDate(p.dt, p.precision, "himalayas:pubDate") if p else None
    cats = j.get("categories") or []
    tags = " ".join(str(c) for c in cats) if isinstance(cats, list) else str(cats)
    loc = j.get("locationRestrictions") or []
    loc = ", ".join(str(x) for x in loc) if isinstance(loc, list) else str(loc)
    return _item(company, title, url, pd, loc, True, "",
                 html_to_text(unescape(j.get("description") or j.get("excerpt") or "")), tags)


def arbeitnow_item(j: dict):
    url, title, company = j.get("url"), j.get("title"), j.get("company_name")
    if not url or not title or not company:
        return None
    p = parse_absolute(j.get("created_at"))
    pd = ParsedDate(p.dt, p.precision, "arbeitnow:created_at") if p else None
    tags = " ".join(str(t) for t in (j.get("tags") or []) + (j.get("job_types") or []))
    remote = j.get("remote")
    return _item(company, title, url, pd, j.get("location") or "", None if remote is None else bool(remote), "",
                 html_to_text(unescape(j.get("description") or "")), tags)


# ----------------------------------------------------------------------------- collectors
async def _board(f: Fetcher, kind: str, token: str, company: str):
    if kind == "greenhouse":
        url, make = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", greenhouse_item
    else:
        url, make = f"https://api.ashbyhq.com/posting-api/job-board/{token}", ashby_item
    r, data = await f.fetch_json(url, robots=False)           # official public job-board APIs
    if not isinstance(data, dict):
        return []
    return [it for j in data.get("jobs", []) if isinstance(j, dict) and (it := make(j, token, company))]


async def collect_greenhouse(f: Fetcher, now: datetime):
    boards = await asyncio.gather(*[_board(f, "greenhouse", t, n) for t, n in GREENHOUSE.items()])
    return [it for b in boards for it in b], None


async def collect_ashby(f: Fetcher, now: datetime):
    boards = await asyncio.gather(*[_board(f, "ashby", t, n) for t, n in ASHBY.items()])
    return [it for b in boards for it in b], None


async def collect_wwr(f: Fetcher, now: datetime):
    r = await f.fetch("https://weworkremotely.com/remote-jobs.rss")
    if not r.ok:
        return [], r.error or f"http_{r.status}"
    return [it for e in feedparser.parse(r.text).entries if (it := wwr_item(e))], None


async def collect_himalayas(f: Fetcher, now: datetime):
    out = []
    for offset in range(0, 100, 20):
        r, data = await f.fetch_json(f"https://himalayas.app/jobs/api?limit=20&offset={offset}")
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        if not jobs:
            if offset == 0:
                return [], r.error or "no_jobs"
            break
        out += [it for j in jobs if isinstance(j, dict) and (it := himalayas_item(j))]
    return out, None


async def collect_arbeitnow(f: Fetcher, now: datetime):
    out = []
    for page in (1, 2, 3):
        r, data = await f.fetch_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        jobs = data.get("data", []) if isinstance(data, dict) else []
        if not jobs:
            if page == 1:
                return [], r.error or "no_jobs"
            break
        out += [it for j in jobs if isinstance(j, dict) and (it := arbeitnow_item(j))]
    return out, None


SOURCES = [
    {"name": "Greenhouse (AI company boards)", "collect": collect_greenhouse, "ai_filter": False},
    {"name": "Ashby (AI company boards)", "collect": collect_ashby, "ai_filter": False},
    {"name": "We Work Remotely", "collect": collect_wwr, "ai_filter": True},
    {"name": "Himalayas", "collect": collect_himalayas, "ai_filter": True},
    {"name": "Arbeitnow", "collect": collect_arbeitnow, "ai_filter": True},
]


# ----------------------------------------------------------------------------- processing
@dataclass
class Ctx:
    f: Fetcher
    db: Database
    now: datetime
    resolver: EntityResolver
    stats: Counter


async def ensure_description(ctx: Ctx, item: dict):
    text, dept = item["description"], item["department"]
    detail = item.get("detail")
    if len(text) < 300 and detail and detail[0] == "greenhouse":
        r, data = await ctx.f.fetch_json(
            f"https://boards-api.greenhouse.io/v1/boards/{detail[1]}/jobs/{detail[2]}", robots=False)
        if isinstance(data, dict):
            text = html_to_text(unescape(data.get("content") or ""))
            names = [d.get("name", "") for d in (data.get("departments") or []) if isinstance(d, dict)]
            dept = ", ".join(n for n in names if n) or dept
    if len(text) < 300:
        _html, page = await fetch_article(ctx.f, item["url"])
        if len(page) > len(text):
            text = page
    return text, dept


async def process_job(ctx: Ctx, src: dict, item: dict) -> None:
    stats, pd = ctx.stats, item["date"]
    if pd is None:
        stats["no_date"] += 1
        return
    if not is_fresh(pd, ctx.now):
        stats["old"] += 1
        return
    if src["ai_filter"] and not is_ai_job(item["title"]):
        stats["not_ai"] += 1
        return
    url = item["url"]
    if not await ctx.db.claim_url(url, src["name"]):               # never process the same posting twice
        stats["seen_before"] += 1
        return
    item["_claimed"] = True
    text, dept = await ensure_description(ctx, item)
    if len(text) < 100:
        await ctx.db.release_url(url)
        stats["no_full_text"] += 1
        return
    res = await ctx.resolver.resolve_and_log(ctx.db, item["company"], "JOB", allow_fuzzy=True)
    remote = item["is_remote"] if item["is_remote"] is not None else looks_remote(item["location"], item["title"])
    try:
        rec = JobRecord(
            source=Source(name=src["name"], url=url),
            collectedAt=utc_now_iso(),
            content=JobContent(
                company=res.canonical,
                date=pd.dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                is_remote=bool(remote), role_family=role_family(item["title"], dept), title=item["title"],
                location=item["location"] or None, description=text[:45000], text_length=len(text),
                date_source=pd.source, date_confidence=pd.precision),
        )
    except ValidationError as e:
        stats["invalid_record"] += 1
        log.warning("invalid job %s: %s", url, str(e)[:120])
        return
    if await ctx.db.insert_record("JOB", url_hash(url), src["name"], url, rec.model_dump()):
        stats["stored"] += 1
    else:
        stats["duplicate_record"] += 1


async def safe_process(ctx: Ctx, src: dict, item: dict) -> None:
    try:
        await process_job(ctx, src, item)
    except Exception as exc:
        ctx.stats["item_error"] += 1
        log.warning("job failed %s: %s", item.get("url"), str(exc)[:120])
        if item.get("_claimed"):
            await ctx.db.release_url(item["url"])


async def run(db: Database, fetcher: Fetcher, resolver: EntityResolver) -> None:
    now = datetime.now(timezone.utc)
    report: dict[str, Counter] = {}
    for src in SOURCES:
        stats: Counter = Counter()
        report[src["name"]] = stats
        try:
            items, err = await src["collect"](fetcher, now)
        except Exception as exc:
            log.warning("collector crashed for %s: %s", src["name"], exc)
            stats["collector_crashed"] = 1
            continue
        if err:
            stats["source_error"] = 1
            log.warning("%s: %s", src["name"], err)
            continue
        stats["collected"] = len(items)
        ctx = Ctx(fetcher, db, now, resolver, stats)
        fresh = [it for it in items if it["date"] is None or is_fresh(it["date"], now)]
        stats["old"] += len(items) - len(fresh)                      # cheap rejection before any work
        for i in range(0, len(fresh), 8):
            await asyncio.gather(*[safe_process(ctx, src, it) for it in fresh[i:i + 8]])

    print(f"\nrun time (UTC): {now:%Y-%m-%d %H:%M}")
    for name, st in report.items():
        print(f"  {name:34} " + "  ".join(f"{k}={v}" for k, v in sorted(st.items())))
    rows = await db.fetch_records("JOB")
    print(f"\ntotal JOB records in database: {len(rows)}")
    print("role families:", dict(Counter(r["content"]["role_family"] for r in rows).most_common()))
    print("remote:", dict(Counter(r["content"]["is_remote"] for r in rows)))
    print("\n5 random jobs to verify by hand:")
    for r in random.sample(rows, min(5, len(rows))):
        c = r["content"]
        print(f"- {c['company']}: {c['title']}  [{c['role_family']}, remote={c['is_remote']}]\n"
              f"  {c['date']} ({c['date_source']}, {c['date_confidence']})  text={c['text_length']} chars\n  {r['source_url']}")


# ----------------------------------------------------------------------------- diagnostics
def _first_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("jobs", "data", "hits", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


async def board_check(f: Fetcher) -> None:
    """Prints what each board really contains, so you can confirm the company behind each token."""
    async def one(kind, token, company):
        url = (f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs" if kind == "greenhouse"
               else f"https://api.ashbyhq.com/posting-api/job-board/{token}")
        r, data = await f.fetch_json(url, robots=False)
        jobs = _first_list(data)
        first = jobs[0] if jobs and isinstance(jobs[0], dict) else {}
        return kind, token, company, len(jobs), [str(j.get("title")) for j in jobs[:3] if isinstance(j, dict)], \
            first.get("jobUrl") or first.get("absolute_url")
    tasks = [one("greenhouse", t, n) for t, n in GREENHOUSE.items()] + [one("ashby", t, n) for t, n in ASHBY.items()]
    for kind, token, company, n, titles, url in await asyncio.gather(*tasks):
        print(f"{kind:10} {token:14} -> {company:16} jobs={n:<4} {titles}\n{'':12}{url}")


async def peek(f: Fetcher) -> None:
    """Prints the real fields of one item per source, to confirm the field names used above."""
    targets = {
        "greenhouse/anthropic": "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs",
        "ashby/elevenlabs": "https://api.ashbyhq.com/posting-api/job-board/elevenlabs",
        "himalayas": "https://himalayas.app/jobs/api?limit=2",
        "arbeitnow": "https://www.arbeitnow.com/api/job-board-api",
    }
    for name, url in targets.items():
        r, data = await f.fetch_json(url, robots=False)
        items = _first_list(data)
        print(f"\n== {name}: {len(items)} items (error={r.error})")
        if items and isinstance(items[0], dict):
            for k, v in items[0].items():
                print(f"   {k:24} {str(v)[:70]!r}")
    r = await f.fetch("https://weworkremotely.com/remote-jobs.rss")
    if r.ok and feedparser.parse(r.text).entries:
        e = feedparser.parse(r.text).entries[0]
        print("\n== weworkremotely first entry:", {k: str(e[k])[:50] for k in sorted(e.keys())})