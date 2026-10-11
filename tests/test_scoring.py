import pytest

from app.scoring import calculate_job_score


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("Java Backend Engineer", "Java, Spring Boot", 90),
        ("Java Developer", "No required technologies listed", 60),
        ("Full Stack Developer", "Java only", 45),
        ("Software Engineer", "Spring only", 45),
        ("Backend Engineer", "Neither", 30),
        ("Frontend Developer", "JavaScript only", 0),
    ],
)
def test_scoring_examples(title: str, description: str, expected: int) -> None:
    assert calculate_job_score(title, description).score == expected


def test_case_insensitive_matching() -> None:
    result = calculate_job_score("sPrInG Engineer", "jAvA")
    assert result.score == 75


def test_javascript_does_not_match_java() -> None:
    result = calculate_job_score("Frontend Developer", "JavaScript and TypeScript")
    assert result.score == 0
    assert "description:java" not in result.matched_keywords


def test_title_tiers_do_not_stack() -> None:
    result = calculate_job_score("Backend Full Stack Software Engineer", "")
    assert result.score == 30


def test_repeated_mentions_count_once_per_technology() -> None:
    result = calculate_job_score("Java Developer", "Java java SPRING spring")
    assert result.score == 90
    assert result.matched_keywords.count("description:java") == 1
    assert result.matched_keywords.count("description:spring") == 1


def test_missing_fields_safe() -> None:
    assert calculate_job_score(None, None).score == 0
    assert calculate_job_score("", "").score == 0


def test_spring_boot_and_framework_count_as_spring() -> None:
    result = calculate_job_score("Frontend Developer", "Spring Boot and Spring Framework")
    assert result.score == 15
    assert "description:spring" in result.matched_keywords
