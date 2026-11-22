from datetime import datetime, timezone
from src.sources.news import html_to_text, is_ai_article, make_company_validator, parse_hn_html
import pytest
from pydantic import ValidationError

from src.schemas import NewsContent
from src.sources.news import html_to_text, make_company_validator, parse_hn_html

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
HN = (
    '<tr class="athing submission" id="1"><td><span class="titleline">'
    '<a href="https://example.com/a">Cool AI thing</a></span></td></tr>'
    '<tr><td class="subtext"><span class="subline"><span class="age" title="2026-11-22T10:00:00 1795341600">'
    '<a href="item?id=1">2 hours ago</a></span></span></td></tr>'
    '<tr class="athing submission" id="2"><td><span class="titleline">'
    '<a href="item?id=2">Ask HN: something</a></span></td></tr>'
)


def test_hn_html_relative_text_is_a_flagged_fallback():
    items = parse_hn_html(HN, "https://news.ycombinator.com/newest", NOW)
    assert len(items) == 1
    it = items[0]
    assert it["url"] == "https://example.com/a" and it["title"] == "Cool AI thing"
    assert it["date"] is None
    fb = it["fallback_date"]
    assert fb.dt == datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
    assert fb.source == "relative_text" and fb.precision == "inferred"


def test_ai_article_filter():
    assert is_ai_article("Show HN: Agent Doctor for LLM agents", "x")
    assert not is_ai_article("RayClay: an open source C app framework", "A small C framework for desktop apps.")
    assert is_ai_article("Some title", "We use an LLM. The LLM calls an agent. Another AI model.")


def test_company_names_must_appear_in_text():
    v = make_company_validator("OpenAI and Anthropic announced a partnership with Nvidia.")
    assert v({"companies": ["OpenAI", "Anthropic"]}) is None
    assert v({"companies": ["OpenAI", "Google"]}) == "company_not_in_text"
    assert v({"names": []}) == "bad_companies"


def test_html_to_text():
    assert html_to_text("<p>Hello <b>world</b></p><script>x()</script>") == "Hello world"


def test_news_schema_needs_full_text():
    with pytest.raises(ValidationError):
        NewsContent(title="t", url="https://x.com/a", published_date="2026-11-22T10:00:00Z",
                    date_source="rss:published", date_confidence="exact", full_text="too short", text_length=9)