from __future__ import annotations

import re
from dataclasses import dataclass


TITLE_PRIMARY_PATTERN = re.compile(r"\b(java|spring)\b", re.IGNORECASE)
TITLE_SECONDARY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("backend", re.compile(r"\bbackend\b", re.IGNORECASE)),
    ("full stack", re.compile(r"\bfull[\s-]*stack\b", re.IGNORECASE)),
    ("software engineer", re.compile(r"\bsoftware\s+engineer\b", re.IGNORECASE)),
    ("software developer", re.compile(r"\bsoftware\s+developer\b", re.IGNORECASE)),
)
JAVA_PATTERN = re.compile(r"\bjava\b(?![\s-]*script\b)", re.IGNORECASE)
SPRING_PATTERN = re.compile(r"\bspring\b", re.IGNORECASE)


@dataclass(slots=True)
class ScoreResult:
    score: int
    matched_keywords: list[str]


def _normalize(value: str | None) -> str:
    return value or ""


def calculate_job_score(title: str | None, description: str | None) -> ScoreResult:
    title_text = _normalize(title)
    description_text = _normalize(description)
    matched: list[str] = []

    title_has_java = bool(JAVA_PATTERN.search(title_text))
    title_has_spring = bool(SPRING_PATTERN.search(title_text))

    title_score = 0
    if title_has_java or title_has_spring:
        title_score = 60
        if title_has_java:
            matched.append("title:java")
        if title_has_spring:
            matched.append("title:spring")
    else:
        secondary_matches: list[str] = []
        for label, pattern in TITLE_SECONDARY_PATTERNS:
            if pattern.search(title_text):
                secondary_matches.append(label)
        if secondary_matches:
            title_score = 30
            for label in secondary_matches:
                matched.append(f"title:{label}")

    desc_java = bool(JAVA_PATTERN.search(description_text))
    desc_spring = bool(SPRING_PATTERN.search(description_text))
    desc_score = 0
    if desc_java:
        desc_score += 15
        matched.append("description:java")
    if desc_spring:
        desc_score += 15
        matched.append("description:spring")

    score = min(90, title_score + desc_score)
    return ScoreResult(score=score, matched_keywords=matched)
