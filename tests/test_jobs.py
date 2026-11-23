from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.schemas import JobContent
from src.sources.jobs import (arbeitnow_item, ashby_item, greenhouse_item, is_ai_job, looks_remote, role_family,
                              split_wwr_title)


@pytest.mark.parametrize("title,expected", [
    ("Senior Software Engineer", "Engineering"), ("Machine Learning Engineer", "Engineering"),
    ("Research Scientist", "Research"), ("Research Engineer", "Research"), ("Data Scientist", "Data"),
    ("Product Designer", "Design"), ("Senior Product Manager, AI", "Product"), ("Account Executive", "Sales"),
    ("Security Engineer", "Security"), ("Technical Program Manager", "Operations"), ("Recruiter", "Recruiting"),
    ("Head of Legal", "Legal"), ("Marketing Manager", "Marketing"), ("Customer Success Manager", "Customer Success"),
    ("Barista", "Other"),
])
def test_role_family_from_title(title, expected):
    assert role_family(title) == expected


def test_role_family_falls_back_to_department():
    assert role_family("Member of Technical Staff", "Engineering") == "Engineering"


def test_ai_filter_and_remote_detection():
    assert is_ai_job("Senior Machine Learning Engineer") and is_ai_job("Sales Manager", "AI products")
    assert not is_ai_job("Account Executive", "saas sales")
    assert looks_remote("Remote - US") and not looks_remote("London, UK")


def test_greenhouse_item_uses_first_published_not_updated_at():
    j = {"id": 123, "title": "Research Engineer", "absolute_url": "https://job-boards.greenhouse.io/anthropic/jobs/123",
         "first_published": "2026-11-22T05:30:00-05:00", "updated_at": "2026-11-22T11:00:00-05:00",
         "location": {"name": "Remote - US"}}
    it = greenhouse_item(j, "anthropic", "Anthropic")
    assert it["date"].dt == datetime(2026, 11, 22, 10, 30, tzinfo=timezone.utc)
    assert it["date"].source == "greenhouse:first_published" and it["location"] == "Remote - US"


def test_ashby_item_reads_remote_flag_and_description():
    j = {"title": "ML Engineer", "jobUrl": "https://jobs.ashbyhq.com/x/1", "publishedAt": "2026-11-22T09:00:00.000+00:00",
         "isRemote": True, "location": "London", "department": "Engineering", "descriptionPlain": "A" * 400}
    it = ashby_item(j, "x", "X Corp")
    assert it["is_remote"] is True and len(it["description"]) == 400 and it["date"].source == "ashby:publishedAt"


def test_arbeitnow_item_epoch_date():
    j = {"url": "https://www.arbeitnow.com/jobs/1", "title": "AI Engineer", "company_name": "Acme GmbH",
         "created_at": 1795258805, "remote": True, "description": "<p>Build things</p>", "tags": ["ai"], "job_types": []}
    it = arbeitnow_item(j)
    assert it["date"].dt == datetime(2026, 11, 21, 11, 0, 5, tzinfo=timezone.utc)
    assert it["is_remote"] is True and it["description"] == "Build things"


def test_wwr_title_split():
    assert split_wwr_title("Acme Inc: Senior AI Engineer") == ("Acme Inc", "Senior AI Engineer")
    assert split_wwr_title("No company here") == ("", "No company here")


def test_job_schema_rejects_unknown_role_family():
    with pytest.raises(ValidationError):
        JobContent(company="X", date="2026-11-22T10:00:00Z", is_remote=True, role_family="Wizard", title="t",
                   description="d" * 200, text_length=200, date_source="s", date_confidence="exact")

def test_greenhouse_location_type_metadata_sets_remote():
    base = {"id": 1, "title": "Engineer", "absolute_url": "https://x.com/1",
            "first_published": "2026-11-22T10:00:00Z", "location": {"name": "SF"}}
    on_site = greenhouse_item({**base, "metadata": [{"name": "Location Type", "value": "On-Site"}]}, "t", "T")
    remote = greenhouse_item({**base, "metadata": [{"name": "Location Type", "value": "Remote"}]}, "t", "T")
    assert on_site["is_remote"] is False and remote["is_remote"] is True
    assert greenhouse_item(base, "t", "T")["is_remote"] is None