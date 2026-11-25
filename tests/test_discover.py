from src.sources.products_discover import LOC_RE, PRICING_PATH_RE, pick_candidates


def test_sitemap_locs_handle_whitespace_and_cdata():
    xml = "<urlset><url><loc> https://x.com/pricing </loc></url><url><loc><![CDATA[https://x.com/plans]]></loc></url></urlset>"
    assert LOC_RE.findall(xml) == ["https://x.com/pricing", "https://x.com/plans"]


def test_pricing_path_regex():
    assert PRICING_PATH_RE.search("/pricing/") and PRICING_PATH_RE.search("/en/plans")
    assert not PRICING_PATH_RE.search("/pricing-strategy-guide")


def test_candidates_prefer_shallow_paths_and_skip_blog_and_other_sites():
    locs = ["https://x.com/blog/pricing", "https://x.com/en/pricing", "https://x.com/pricing",
            "https://other.com/pricing", "https://x.com/about"]
    assert pick_candidates(locs, "https://x.com") == ["https://x.com/pricing", "https://x.com/en/pricing"]