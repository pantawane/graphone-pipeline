from src.sources.jobs import greenhouse_item, is_ai_job, role_family


def test_greenhouse_location_type_metadata_sets_remote():
    base = {"id": 1, "title": "Engineer", "absolute_url": "https://x.com/1",
            "first_published": "2026-11-22T10:00:00Z", "location": {"name": "SF"}}
    on_site = greenhouse_item({**base, "metadata": [{"name": "Location Type", "value": "On-Site"}]}, "t", "T")
    remote = greenhouse_item({**base, "metadata": [{"name": "Location Type", "value": "Remote"}]}, "t", "T")
    assert on_site["is_remote"] is False and remote["is_remote"] is True
    assert greenhouse_item(base, "t", "T")["is_remote"] is None


def test_german_titles_are_engineering():
    assert role_family("AI Entwickler") == "Engineering"
    assert role_family("Backend Developer (m/w/d)") == "Engineering"


def test_general_board_filter_is_title_based():
    assert is_ai_job("Senior Machine Learning Engineer")
    assert not is_ai_job("Director, Product Management - Search & Match")