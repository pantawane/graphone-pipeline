"""Date normalization: structured fields, relative dates ("2 hours ago"), page metadata and freshness checks.
Every result carries a precision so a vague date can never pass the strict 24-hour test by accident."""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape


@dataclass
class ParsedDate:
    dt: datetime            # timezone-aware, UTC
    precision: str          # "exact" | "day" | "inferred"
    source: str             # which rule produced it


NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_UNIT = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
         "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
         "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
         "d": 86400, "day": 86400, "days": 86400,
         "w": 604800, "week": 604800, "weeks": 604800,
         "month": 2592000, "months": 2592000, "year": 31536000, "years": 31536000}
REL_RE = re.compile(
    r"\b(?:(?P<d>\d+)\s*|(?P<w>an?|one|two|three|four|five|six|seven|eight|nine|ten)\s+)"
    r"(?P<u>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|[smhdw])\s+ago\b",
    re.I,
)
JUST_NOW_RE = re.compile(r"\b(?:just now|moments? ago|a moment ago)\b", re.I)
LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
TIME_RE = re.compile(r'<time\b[^>]*\bdatetime=["\']([^"\']+)["\']', re.I)
URL_DATE_RE = re.compile(r"/(20\d{2})/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?:/|$)")
META_KEYS = {"article:published_time", "og:article:published_time", "datepublished", "pubdate", "publishdate",
             "publish_date", "date", "dc.date.issued", "dc.date", "parsely-pub-date", "sailthru.date"}


def _epoch(v: float) -> "ParsedDate | None":
    if v > 1e12:
        v /= 1000.0
    if v < 1e9:
        return None
    return ParsedDate(datetime.fromtimestamp(v, timezone.utc), "exact", "epoch")


def parse_absolute(value) -> "ParsedDate | None":
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _epoch(float(value))
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,13}(?:\.\d+)?", s):
        return _epoch(float(s))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return ParsedDate(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc), "day", "iso_date")
        except ValueError:
            return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)          # assumption: no timezone means UTC
        return ParsedDate(d.astimezone(timezone.utc), "exact", "iso")
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(s)
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return ParsedDate(d.astimezone(timezone.utc), "exact", "rfc822")
    except (TypeError, ValueError, IndexError):
        pass
    try:
        import dateparser
        d = dateparser.parse(s, settings={"TIMEZONE": "UTC", "RETURN_AS_TIMEZONE_AWARE": True, "TO_TIMEZONE": "UTC"})
    except Exception:
        d = None
    if d is None:
        return None
    return ParsedDate(d, "exact" if re.search(r"\d{1,2}:\d{2}", s) else "day", "dateparser")


def parse_relative(text: str, now: datetime) -> "ParsedDate | None":
    if JUST_NOW_RE.search(text):
        return ParsedDate(now, "exact", "relative:now")
    m = REL_RE.search(text)
    if m:
        n = int(m.group("d")) if m.group("d") else NUM_WORDS[m.group("w").lower()]
        secs = _UNIT[m.group("u").lower()] * n
        # a day or more is too coarse to prove "within 24 hours", so it only gets day precision
        return ParsedDate(now - timedelta(seconds=secs), "exact" if secs < 86400 else "day", "relative")
    if re.search(r"\byesterday\b", text, re.I):
        return ParsedDate(now - timedelta(days=1), "day", "relative:yesterday")
    if re.search(r"\btoday\b", text, re.I):
        return ParsedDate(now, "day", "relative:today")
    return None


def parse_datetime(value, now: "datetime | None" = None) -> "ParsedDate | None":
    now = now or datetime.now(timezone.utc)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return parse_absolute(value)
    s = str(value).strip()
    if not s:
        return None
    return parse_relative(s, now) or parse_absolute(s)


def _walk(obj, key: str):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str):
                yield v
            else:
                yield from _walk(v, key)
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk(x, key)


def meta_dates(html: str, url: str = "") -> list[ParsedDate]:
    """Publication-date candidates found in a page, best first: JSON-LD, meta tags, <time>, URL pattern."""
    out: list[ParsedDate] = []
    for block in LD_RE.findall(html or ""):
        try:
            data = json.loads(unescape(block).strip())
        except Exception:
            continue
        for v in _walk(data, "datePublished"):
            p = parse_absolute(v)
            if p:
                out.append(ParsedDate(p.dt, p.precision, "jsonld"))
    for tag in re.findall(r"<meta\b[^>]*>", html or "", re.I):
        attrs = {k.lower(): v for k, v in re.findall(r'([\w:.-]+)\s*=\s*["\']([^"\']*)["\']', tag)}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key in META_KEYS and attrs.get("content"):
            p = parse_absolute(attrs["content"])
            if p:
                out.append(ParsedDate(p.dt, p.precision, f"meta:{key}"))
    m = TIME_RE.search(html or "")
    if m:
        p = parse_absolute(m.group(1))
        if p:
            out.append(ParsedDate(p.dt, p.precision, "time_tag"))
    um = URL_DATE_RE.search(url or "")
    if um:
        try:
            d = datetime(int(um.group(1)), int(um.group(2)), int(um.group(3)), tzinfo=timezone.utc)
            out.append(ParsedDate(d, "day", "url_pattern"))
        except ValueError:
            pass
    out.sort(key=lambda p: 0 if p.precision == "exact" else 1)          # stable: exact dates first
    return out


def is_fresh(p: "ParsedDate | None", now: datetime, hours: int = 24, margin_min: int = 5) -> bool:
    if p is None:
        return False
    if p.precision == "day":
        return p.dt.date() == now.date()                                  # published today (UTC)
    age = now - p.dt
    return -timedelta(minutes=margin_min) <= age <= timedelta(hours=hours)