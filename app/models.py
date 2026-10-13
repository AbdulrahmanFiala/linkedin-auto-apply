import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional


class JobStatus(StrEnum):
    NEW = "NEW"
    SENT_TO_RELEVANT_JOBS = "SENT_TO_RELEVANT_JOBS"
    SENT_TO_NON_RELEVANT_JOBS = "SENT_TO_NON_RELEVANT_JOBS"
    QUEUED = "QUEUED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ApplicationStatus(StrEnum):
    QUEUED = "QUEUED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ApplyMethod(StrEnum):
    EASY_APPLY = "EASY_APPLY"
    EXTERNAL = "EXTERNAL"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class SearchQuery:
    keyword: str
    location: str


@dataclass(slots=True)
class DiscoveredJob:
    source: str
    external_id: Optional[str]
    url: str
    title: str
    company: str
    location: str
    description: Optional[str]
    posted_at: Optional[str]
    discovered_at: datetime
    apply_method: str = ApplyMethod.UNKNOWN.value


def normalize_url(url: str) -> str:
    value = url.strip()
    if "?" in value:
        value = value.split("?", 1)[0]
    if "#" in value:
        value = value.split("#", 1)[0]
    return value.rstrip("/")


def fallback_external_id(url: str) -> str:
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
