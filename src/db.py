"""SQLite storage. Single place that decides 'have we seen this URL before?'.

In production the same schema runs on Postgres (UNIQUE constraints + INSERT ... ON CONFLICT DO NOTHING).
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiosqlite

TRACKING_PARAM = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|ref_src$|igshid$)", re.I)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_url(url: str) -> str:
    """Normalize a URL so trivially different forms of the same page get the same key.

    Lowercases the host, drops 'www.', treats http and https as the same, removes the fragment,
    default ports and tracking params, sorts query params and strips a trailing slash.
    """
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    try:
        port = p.port
    except ValueError:
        port = None
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAM.match(k)
    )
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_urls (
    url_hash      TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    source        TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_pages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash     TEXT NOT NULL,
    url          TEXT NOT NULL,
    final_url    TEXT,
    status       INTEGER,
    fetched_at   TEXT NOT NULL,
    content_hash TEXT,
    body         TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_pages_url ON raw_pages(url_hash);
CREATE TABLE IF NOT EXISTS records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type  TEXT NOT NULL,
    dedupe_key   TEXT NOT NULL,
    source_name  TEXT,
    source_url   TEXT NOT NULL,
    data_json    TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    UNIQUE (record_type, dedupe_key)
);
CREATE TABLE IF NOT EXISTS entity_mapping_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type    TEXT,
    raw_name       TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    method         TEXT,
    score          REAL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crawl_state (
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, key)
);
"""


class Database:
    def __init__(self, path: str = "data/pipeline.db"):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def __aenter__(self) -> "Database":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def existing_keys(self, record_type: str) -> set[str]:
        cur = await self.conn.execute("SELECT dedupe_key FROM records WHERE record_type=?", (record_type,))
        return {r[0] for r in await cur.fetchall()}

    # ---- "never process the same URL twice" -------------------------------------------------
    async def claim_url(self, url: str, source: str) -> bool:
        """Atomically mark a URL as seen. True = first time (go ahead), False = already seen."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO seen_urls (url_hash, canonical_url, source, first_seen_at) VALUES (?,?,?,?)",
            (url_hash(url), canonical_url(url), source, utc_now_iso()),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def has_seen(self, url: str) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM seen_urls WHERE url_hash=?", (url_hash(url),))
        return await cur.fetchone() is not None

    # ---- raw pages (evidence that every record came from a real fetch) -----------------------
    async def save_raw_page(self, url: str, final_url: str, status: int, body: str) -> None:
        await self.conn.execute(
            "INSERT INTO raw_pages (url_hash, url, final_url, status, fetched_at, content_hash, body) "
            "VALUES (?,?,?,?,?,?,?)",
            (url_hash(url), url, final_url, status, utc_now_iso(), content_hash(body or ""), body),
        )
        await self.conn.commit()

    async def count_raw_pages(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) FROM raw_pages")
        return (await cur.fetchone())[0]

    # ---- final records -----------------------------------------------------------------------
    async def insert_record(self, record_type: str, dedupe_key: str, source_name: str,
                            source_url: str, data: dict) -> bool:
        """Insert a record. False if the same (type, key) already exists.
        Refuses records without a source URL: every record must trace back to a real source."""
        if not source_url or not source_url.startswith(("http://", "https://")):
            raise ValueError("record needs a valid source_url")
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO records (record_type, dedupe_key, source_name, source_url, data_json, collected_at) "
            "VALUES (?,?,?,?,?,?)",
            (record_type, dedupe_key, source_name, source_url,
             json.dumps(data, ensure_ascii=False), utc_now_iso()),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def count_records(self, record_type: str | None = None) -> int:
        if record_type:
            cur = await self.conn.execute("SELECT COUNT(*) FROM records WHERE record_type=?", (record_type,))
        else:
            cur = await self.conn.execute("SELECT COUNT(*) FROM records")
        return (await cur.fetchone())[0]

    async def fetch_records(self, record_type: str) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT source_name, source_url, data_json, collected_at FROM records WHERE record_type=? ORDER BY id",
            (record_type,),
        )
        return [
            {"source_name": r[0], "source_url": r[1], "collected_at": r[3], **json.loads(r[2])}
            for r in await cur.fetchall()
        ]

    # ---- entity mapping log (used from Step 5 on) --------------------------------------------
    async def log_entity(self, record_type: str, raw_name: str, canonical_name: str,
                         method: str, score: float | None) -> None:
        await self.conn.execute(
            "INSERT INTO entity_mapping_log (record_type, raw_name, canonical_name, method, score, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (record_type, raw_name, canonical_name, method, score, utc_now_iso()),
        )
        await self.conn.commit()

    # ---- crawl state / checkpoints / watermarks ----------------------------------------------
    async def set_state(self, source: str, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO crawl_state (source, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(source, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (source, key, value, utc_now_iso()),
        )
        await self.conn.commit()

    async def get_state(self, source: str, key: str) -> str | None:
        cur = await self.conn.execute("SELECT value FROM crawl_state WHERE source=? AND key=?", (source, key))
        row = await cur.fetchone()
        return row[0] if row else None