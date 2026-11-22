from src.sources.products import classify, pricing_candidates
from src.sources.products import classify, mentions_company, pricing_candidates

def test_freemium_has_evidence_inside_text():
    text = "Free plan $0 per month. Pro plan $29 per month billed yearly."
    label, ev = classify(text)
    assert label == "FREEMIUM" and all(e in text for e in ev)


def test_paid_only():
    label, ev = classify("Starter $49/month Business $199/month")
    assert label == "PAID" and ev


def test_enterprise_only():
    assert classify("Contact sales for custom pricing")[0] == "ENTERPRISE"


def test_free_with_upsell_is_not_guessed():
    assert classify("Free forever. Upgrade to Pro for more.")[0] is None


def test_funding_amount_is_not_a_price():
    assert classify("We raised $10M in seed funding")[0] is None


def test_candidates_skip_blog_and_anchors():
    html = ('<a href="/blog/pricing-strategy">Pricing strategy</a><a href="#pricing">Pricing</a>'
            '<a href="/pricing">Pricing</a>')
    cands = pricing_candidates(html, "https://example.com/")
    assert cands[0] == ("https://example.com/pricing", True)
    assert not any("blog" in u for u, _ in cands)

def test_wage_is_not_a_product_price():
    assert classify("Workers earn $20/month bonus per shift")[0] is None


def test_generic_demo_button_is_not_enterprise_pricing():
    assert classify("Pricing: book a demo today")[0] is None

def test_feature_row_is_not_enterprise_pricing():
    assert classify("Product bundles | Custom quote templates | External quotes")[0] is None


def test_homepage_cannot_be_enterprise():
    assert classify("Contact sales today", allow_enterprise=False)[0] is None


def test_company_must_be_on_page():
    assert mentions_company("Adri AI", "Pricing for Adri plans", "https://x.com/pricing")
    assert not mentions_company("Amorphic Labs", "Top up by card from $10", "https://agentmuxer.com/")

def test_comparison_page_is_not_attributed_to_the_company():
    text = "Us vs Shopify: Shopify costs $29/month. Alternatives and competitors charge $39/month."
    assert classify(text)[0] is None

def test_rules_never_assign_free():
    assert classify("Free forever $0/month for everyone")[0] is None


def test_create_free_account_is_not_a_free_plan():
    assert classify("Create a free account. Pro $29/month")[0] == "PAID"