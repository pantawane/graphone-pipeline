"""Pydantic schemas = the 'Expected Schemas' from the task. Invalid records can't be saved."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator


class Source(BaseModel):
    name: str
    url: str

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("source.url must be an http(s) URL")
        return v


class PaperContent(BaseModel):
    title: str
    authors: list[str]
    paper_url: str
    github_url: str | None = None
    github_stars: int | None = None
    published_date: str

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty title")
        return v

    @field_validator("authors")
    @classmethod
    def _authors(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("no authors")
        return v

    @field_validator("paper_url")
    @classmethod
    def _paper_url(cls, v: str) -> str:
        if not v.startswith("https://arxiv.org/abs/"):
            raise ValueError("paper_url must be an arXiv abs URL")
        return v

    @field_validator("github_url")
    @classmethod
    def _gh_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("https://github.com/"):
            raise ValueError("github_url must be a github.com URL")
        return v

    @field_validator("github_stars")
    @classmethod
    def _stars(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("negative stars")
        return v

    @field_validator("published_date")
    @classmethod
    def _date(cls, v: str) -> str:
        datetime.fromisoformat(v.replace("Z", "+00:00"))   # raises if not ISO-8601
        return v


class ResearchPaperRecord(BaseModel):
    schemaVersion: str = "1.0"
    recordType: Literal["RESEARCH_PAPER"] = "RESEARCH_PAPER"
    source: Source
    collectedAt: str
    content: PaperContent