from src.sources.products_llm import exact_slices, make_validator

PAGE = "Free plan $0 per month. Pro plan $29 per month. Enterprise contact sales."


def test_accepts_grounded_freemium():
    v = make_validator(PAGE)
    assert v({"pricing_model": "FREEMIUM", "evidence": ["Free plan $0 per month", "Pro plan $29 per month"]}) is None


def test_rejects_quote_not_on_page():
    assert make_validator(PAGE)({"pricing_model": "PAID", "evidence": ["Pro plan costs $99 per month"]}) == "evidence_not_in_page"


def test_rejects_free_when_page_shows_prices():
    assert make_validator(PAGE)({"pricing_model": "FREE", "evidence": ["Free plan $0 per month"]}) == "free_inconsistent"


def test_rejects_enterprise_when_prices_visible():
    assert make_validator(PAGE)({"pricing_model": "ENTERPRISE", "evidence": ["Enterprise contact sales"]}) == "enterprise_inconsistent"


def test_unclear_answer_is_allowed():
    assert make_validator(PAGE)({"pricing_model": None, "evidence": []}) is None


def test_exact_slices_use_the_pages_own_wording():
    assert exact_slices(["pro plan $29   per month"], PAGE) == ["Pro plan $29 per month"]