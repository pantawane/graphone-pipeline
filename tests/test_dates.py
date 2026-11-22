from datetime import datetime, timedelta, timezone

from src.dates import is_fresh, meta_dates, parse_datetime

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def test_relative_dates():
    assert parse_datetime("2 hours ago", NOW).dt == datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
    assert parse_datetime("an hour ago", NOW).dt == datetime(2026, 11, 22, 11, 0, tzinfo=timezone.utc)
    assert parse_datetime("35 min ago", NOW).dt == datetime(2026, 11, 22, 11, 25, tzinfo=timezone.utc)
    assert is_fresh(parse_datetime("just now", NOW), NOW)


def test_a_day_or_more_cannot_prove_freshness():
    assert not is_fresh(parse_datetime("3d ago", NOW), NOW)
    assert not is_fresh(parse_datetime("24 hours ago", NOW), NOW)
    assert not is_fresh(parse_datetime("yesterday", NOW), NOW)


def test_absolute_formats():
    assert parse_datetime(1795258805).dt == datetime(2026, 11, 21, 11, 0, 5, tzinfo=timezone.utc)
    assert parse_datetime("2026-11-22T11:30:00Z").dt == datetime(2026, 11, 22, 11, 30, tzinfo=timezone.utc)
    assert parse_datetime("Sun, 22 Nov 2026 11:30:00 GMT").dt == datetime(2026, 11, 22, 11, 30, tzinfo=timezone.utc)
    assert parse_datetime("not a date") is None


def test_date_only_is_day_precision():
    assert is_fresh(parse_datetime("2026-11-22"), NOW)
    assert not is_fresh(parse_datetime("2026-11-21"), NOW)


def test_meta_date_sources():
    ld = '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-11-22T09:00:00+00:00"}</script>'
    assert meta_dates(ld)[0].source == "jsonld"
    og = '<meta property="article:published_time" content="2026-11-22T08:15:00Z">'
    assert meta_dates(og)[0].source == "meta:article:published_time"
    assert meta_dates('<time datetime="2026-11-22T07:00:00Z">x</time>')[0].source == "time_tag"
    day = meta_dates("<html></html>", "https://x.com/2026/11/22/story")[0]
    assert day.source == "url_pattern" and day.precision == "day"


def test_freshness_window_edges():
    assert is_fresh(parse_datetime((NOW - timedelta(hours=23, minutes=59)).isoformat()), NOW)
    assert not is_fresh(parse_datetime((NOW - timedelta(hours=24, minutes=1)).isoformat()), NOW)
    assert not is_fresh(parse_datetime((NOW + timedelta(hours=1)).isoformat()), NOW)