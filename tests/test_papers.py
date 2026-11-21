import pytest
from pydantic import ValidationError

from src.schemas import PaperContent
from src.sources.papers import extract_arxiv_id, parse_repo, pick_candidates


def test_extract_arxiv_id():
    assert extract_arxiv_id({"paper_arxiv_id": "2301.00001"}) == "2301.00001"
    assert extract_arxiv_id({"paper_url_abs": "https://arxiv.org/abs/2305.12345v2"}) == "2305.12345"
    assert extract_arxiv_id({"paper_url": "https://aclanthology.org/2021.emnlp-main.1"}) is None


def test_parse_repo():
    assert parse_repo("https://github.com/pytorch/pytorch.git") == ("pytorch", "pytorch")
    assert parse_repo("https://gitlab.com/a/b") is None


def test_pick_candidates_prefers_official_and_recent():
    links = [
        {"paper_arxiv_id": "2301.00001", "repo_url": "https://github.com/a/b", "is_official": False},
        {"paper_arxiv_id": "2301.00001", "repo_url": "https://github.com/c/d", "is_official": True},
        {"paper_arxiv_id": "1706.03762", "repo_url": "https://github.com/x/y", "is_official": True},
    ]
    out = pick_candidates(links, n=10)
    assert len(out) == 1 and out[0]["owner"] == "c"


def test_schema_rejects_bad_paper_url():
    with pytest.raises(ValidationError):
        PaperContent(title="t", authors=["a"], paper_url="https://example.com/x",
                     published_date="2024-01-01T00:00:00Z")
        