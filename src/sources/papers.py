"""Research papers vertical: PwC archive (paper->repo links) + arXiv API (metadata) + GitHub API (stars)."""
import asyncio
import glob
import gzip
import json
import logging
import os
import random
import re
import statistics

import feedparser
from pydantic import ValidationError

from src.db import Database, utc_now_iso
from src.fetcher import Fetcher
from src.schemas import PaperContent, ResearchPaperRecord, Source

log = logging.getLogger("papers")

SOURCE_NAME = "arXiv API + Papers with Code archive + GitHub API"
GH_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
ARXIV_RE = re.compile(r"(\d{4}\.\d{4,5})")
AI_CATS = {"cs.AI", "cs.LG", "cs.CL", "cs.CV", "cs.NE", "cs.IR", "cs.MA", "cs.RO", "stat.ML"}


def find_links_file(folder: str = "data/pwc") -> str | None:
    for pattern in ("*.parquet", "*.json.gz", "*.json", "*.jsonl"):
        files = sorted(glob.glob(os.path.join(folder, pattern)))
        if files:
            return files[0]
    return None


def load_json_any(path: str):
    """Loads .parquet, .json, .json.gz or JSON Lines into a list of dicts."""
    if path.endswith(".parquet"):
        import pandas as pd
        df = pd.read_parquet(path)
        df = df.astype(object).where(df.notna(), None)     # NaN -> None
        return df.to_dict("records")
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        text = f.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def extract_arxiv_id(rec: dict) -> str | None:
    for key in ("paper_arxiv_id", "arxiv_id"):
        v = rec.get(key)
        if isinstance(v, str):
            m = ARXIV_RE.search(v)
            if m:
                return m.group(1)
    for key in ("paper_url_abs", "paper_url", "paper_url_pdf"):
        v = rec.get(key)
        if isinstance(v, str) and "arxiv.org" in v:       # only trust URLs that are really arXiv
            m = ARXIV_RE.search(v)
            if m:
                return m.group(1)
    return None


def parse_repo(url: str | None) -> tuple[str, str] | None:
    m = GH_RE.search(url or "")
    if not m:
        return None
    owner, repo = m.groups()
    repo = re.sub(r"\.git$", "", repo)
    return owner, repo


def pick_candidates(links: list, n: int, min_prefix: str = "2301", seed: int = 42,
                    exclude: set | None = None) -> list[dict]:
    """One candidate per arXiv paper, official repos preferred, only papers from 2023 onward."""
    exclude = exclude or set()
    best: dict[str, dict] = {}
    for rec in links:
        if not isinstance(rec, dict):
            continue
        aid = extract_arxiv_id(rec)
        parsed = parse_repo(rec.get("repo_url"))
        if not aid or not parsed or aid < min_prefix or aid in exclude:
            continue
        official = rec.get("is_official") in (True, "True", "true", 1)
        if not official:
            continue                      # only links the archive marks as the paper's official code
        cur = best.get(aid)
        if cur is None or (official and not cur["official"]):
            best[aid] = {"arxiv_id": aid, "owner": parsed[0], "repo": parsed[1], "official": official}
    pool = sorted(best.values(), key=lambda x: x["arxiv_id"])
    random.Random(seed).shuffle(pool)
    pool.sort(key=lambda x: not x["official"])             # stable: official first, random inside groups
    return pool[:n]


async def _arxiv_batch(fetcher: Fetcher, ids: list[str]) -> dict[str, dict]:
    url = f"https://export.arxiv.org/api/query?id_list={','.join(ids)}&max_results={len(ids)}"
    res = await fetcher.fetch(url, robots=False)       # official API meant for programs; 3s gap enforced
    if not res.ok:
        log.warning("arXiv batch failed: %s", res.error)
        return {}
    out: dict[str, dict] = {}
    for e in feedparser.parse(res.text).entries:
        m = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", e.get("id", ""))
        title = " ".join(e.get("title", "").split())
        if not m or not title or title.lower() == "error":
            continue
        out[m.group(1)] = {
            "title": title,
            "authors": [a.get("name", "").strip() for a in e.get("authors", []) if a.get("name")],
            "published": e.get("published"),
            "categories": [t.get("term") for t in e.get("tags", []) if t.get("term")],
        }
    return out


async def fetch_arxiv_metadata(fetcher: Fetcher, ids: list[str], batch: int = 50) -> dict[str, dict]:
    chunks = [ids[i:i + batch] for i in range(0, len(ids), batch)]
    results = await asyncio.gather(*[_arxiv_batch(fetcher, c) for c in chunks])
    merged: dict[str, dict] = {}
    for r in results:
        merged.update(r)
    return merged


async def fetch_stars(fetcher: Fetcher, owner: str, repo: str, token: str) -> tuple[int | None, str | None, str]:
    """Returns (stars, html_url, reason). reason is 'ok' or why it failed."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "Authorization": f"Bearer {token}"}
    res, data = await fetcher.fetch_json(f"https://api.github.com/repos/{owner}/{repo}",
                                         robots=False, headers=headers)
    if isinstance(data, dict) and isinstance(data.get("stargazers_count"), int) and data.get("html_url"):
        return data["stargazers_count"], data["html_url"], "ok"
    remaining = {k.lower(): v for k, v in res.headers.items()}.get("x-ratelimit-remaining")
    if res.status in (403, 429) and remaining == "0":
        return None, None, "rate_limited"
    return None, None, res.error or "no_star_field"


async def run(db: Database, fetcher: Fetcher, target: int, token: str | None) -> None:
    if not token:
        raise SystemExit("GITHUB_TOKEN missing in .env (a token with no scopes is enough).")
    links_path = find_links_file()
    if not links_path:
        raise SystemExit("No links file in data/pwc/. Download it first (see Step 4.1).")

    existing = await db.existing_keys("RESEARCH_PAPER")
    need = target - len(existing)
    log.info("papers already stored: %d, target: %d", len(existing), target)
    if need <= 0:
        log.info("target already reached")
        return

    links = load_json_any(links_path)
    log.info("loaded %d link rows from %s", len(links), links_path)
    cands = pick_candidates(links, n=int(need * 2.2) + 80, exclude=existing)
    log.info("candidates (2023+ papers with a GitHub repo): %d", len(cands))

    meta = await fetch_arxiv_metadata(fetcher, [c["arxiv_id"] for c in cands])
    cands = [c for c in cands if c["arxiv_id"] in meta]
    log.info("arXiv confirmed %d papers", len(cands))
    ai = [c for c in cands if AI_CATS & set(meta[c["arxiv_id"]]["categories"])]
    log.info("AI-category papers: %d of %d", len(ai), len(cands))
    cands = ai

    results = await asyncio.gather(*[fetch_stars(fetcher, c["owner"], c["repo"], token) for c in cands])

    reasons: dict[str, int] = {}
    inserted, stars_seen = 0, []
    for c, (stars, gh_url, reason) in zip(cands, results):
        reasons[reason] = reasons.get(reason, 0) + 1
        if reason != "ok" or inserted >= need:
            continue
        m = meta[c["arxiv_id"]]
        abs_url = f"https://arxiv.org/abs/{c['arxiv_id']}"
        try:
            rec = ResearchPaperRecord(
                source=Source(name=SOURCE_NAME, url=abs_url),
                collectedAt=utc_now_iso(),
                content=PaperContent(title=m["title"], authors=m["authors"], paper_url=abs_url,
                                     github_url=gh_url, github_stars=stars,
                                     published_date=m["published"] or ""),
            )
        except ValidationError as e:
            reasons["invalid_record"] = reasons.get("invalid_record", 0) + 1
            log.warning("invalid record %s: %s", c["arxiv_id"], str(e)[:150])
            continue
        if await db.insert_record("RESEARCH_PAPER", c["arxiv_id"], SOURCE_NAME, abs_url, rec.model_dump()):
            inserted += 1
            stars_seen.append(stars)

    total = await db.count_records("RESEARCH_PAPER")
    log.info("GitHub outcomes: %s", reasons)
    print(f"\nnew records inserted: {inserted}   total RESEARCH_PAPER records: {total}")
    if stars_seen:
        print(f"stars  min={min(stars_seen)}  median={int(statistics.median(stars_seen))}  max={max(stars_seen)}")
    print("\n5 random records to check by hand (open the links!):")
    rows = await db.fetch_records("RESEARCH_PAPER")
    for r in random.sample(rows, min(5, len(rows))):
        c = r["content"]
        print(f"- {c['title'][:70]}\n  {c['paper_url']}  {c['github_url']}  stars={c['github_stars']}")