from src.sources.render import detect_block


def test_cloudflare_challenge_header():
    assert detect_block(403, {"cf-mitigated": "challenge"}, "") == "cloudflare_challenge"


def test_challenge_page_text_on_a_short_page():
    assert detect_block(200, {}, "<html><title>Just a moment...</title></html>") == "challenge_page"


def test_plain_forbidden_is_reported():
    assert detect_block(403, {}, "denied") == "http_403"


def test_normal_page_is_not_blocked():
    assert detect_block(200, {}, "<p>" + "pricing plans " * 2000 + "</p>") is None


def test_captcha_widget_on_a_big_page_is_not_a_block():
    assert detect_block(200, {}, "<div class='g-recaptcha'></div>" + "text " * 3000) is None