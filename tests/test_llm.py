import asyncio
import re
from types import SimpleNamespace

from src.llm import LLMOrchestrator, Tier, classify_error, full_jitter, parse_retry_delay, smart_truncate

PRICE = re.compile(r"[$€£₹]\s?\d|\bfree\b|\bplan\b", re.I)
GOOD = '{"pricing_model": "PAID", "evidence": ["x"]}'


class FakeModel:
    def __init__(self, behaviours):
        self.behaviours = list(behaviours)
        self.prompts = []

    async def ainvoke(self, prompt):
        self.prompts.append(prompt)
        b = self.behaviours.pop(0) if len(self.behaviours) > 1 else self.behaviours[0]
        if isinstance(b, Exception):
            raise b
        return SimpleNamespace(content=b)


def tier(name, model, **kw):
    return Tier(name=name, model=model, max_input_tokens=kw.pop("max_input_tokens", 2000),
                timeout_s=5, max_attempts=kw.pop("max_attempts", 2), **kw)


def orch(tiers):
    return LLMOrchestrator(tiers, backoff_base=0.01, max_sleep=1.0)


def ok(d):
    return None


def needs_model(d):
    return None if "pricing_model" in d else "missing_pricing_model"


def test_smart_truncate_keeps_price_and_respects_budget():
    filler = "This is a long paragraph about nothing in particular. " * 500
    text = "Pricing overview for our product. " + filler + "Pro plan $29 per month for teams. " + filler
    out = smart_truncate(text, 400, PRICE)                      # 400 tokens = about 1600 chars
    assert len(out) <= 1700
    assert "Pro plan $29 per month" in out
    assert out.startswith("Pricing overview")


def test_classify_error_kinds():
    kind, hint = classify_error(Exception("429 RESOURCE_EXHAUSTED. Please retry in 12.5s."))
    assert kind == "rate_limited" and hint == 12.5
    assert classify_error(Exception("Error code: 413 - Request too large for model"))[0] == "too_large"
    assert classify_error(Exception("429 quota exceeded for metric, requests per day"))[0] == "quota_day"
    assert classify_error(asyncio.TimeoutError())[0] == "timeout"
    assert classify_error(Exception("503 UNAVAILABLE high demand"))[0] == "server"
    assert classify_error(Exception("403 API key not valid"))[0] == "auth"


def test_parse_retry_delay():
    assert parse_retry_delay("please retry in 7.5s") == 7.5
    assert parse_retry_delay("try again in 150ms") == 0.15
    assert parse_retry_delay("try again in 1m3.5s") == 63.5
    assert parse_retry_delay("nothing here") is None


def test_full_jitter_bounds():
    for attempt in range(10):
        assert 0 <= full_jitter(attempt, base=1.0, cap=30.0) <= 30.0


def test_falls_back_after_rate_limits():
    t1 = tier("t1", FakeModel([Exception("429 rate limit, please retry in 0.01s")]))
    t2 = tier("t2", FakeModel([GOOD]))
    res = asyncio.run(orch([t1, t2]).extract("{text}", "hello", ok))
    assert res.tier == "t2" and len(t1.model.prompts) == 2 and "t1:rate_limited" in res.events


def test_413_halves_the_input_budget():
    long_text = "Pro plan $29 per month. " * 2000
    t1 = tier("t1", FakeModel([Exception("Error code: 413 - Request too large"), GOOD]), max_input_tokens=4000)
    res = asyncio.run(orch([t1]).extract("{text}", long_text, ok, focus=PRICE))
    p = t1.model.prompts
    assert res.tier == "t1" and len(p) == 2 and len(p[1]) < len(p[0])


def test_invalid_output_goes_to_next_tier():
    t1 = tier("t1", FakeModel(['{"x": 1}']))
    t2 = tier("t2", FakeModel([GOOD]))
    res = asyncio.run(orch([t1, t2]).extract("{text}", "hello", needs_model))
    assert res.tier == "t2" and "t1:invalid(missing_pricing_model)" in res.events


def test_daily_quota_tier_is_skipped_next_time():
    t1 = tier("t1", FakeModel([Exception("429 RESOURCE_EXHAUSTED quota exceeded, requests per day")]))
    t2 = tier("t2", FakeModel([GOOD]))
    o = orch([t1, t2])

    async def run():
        a = await o.extract("{text}", "hello", ok)
        b = await o.extract("{text}", "hello", ok)
        return a, b

    a, b = asyncio.run(run())
    assert a.tier == "t2" and b.tier == "t2" and len(t1.model.prompts) == 1


def test_all_tiers_failing_returns_no_data():
    t1 = tier("t1", FakeModel([Exception("boom")]))
    res = asyncio.run(orch([t1]).extract("{text}", "hello", ok))
    assert res.data is None and res.reason == "all_tiers_failed"