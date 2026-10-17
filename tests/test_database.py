import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Config, load_config, validate_config
from app.database import Database
from app.models import (
    ApplicationStatus,
    ApplyMethod,
    DiscoveredJob,
    JobStatus,
    SearchQuery,
    fallback_external_id,
    normalize_url,
)


def make_job(
    url: str,
    external_id: str | None = None,
    title: str = "Backend Engineer",
    apply_method: str = ApplyMethod.UNKNOWN.value,
) -> DiscoveredJob:
    return DiscoveredJob(
        source="linkedin",
        external_id=external_id,
        url=url,
        title=title,
        company="Acme",
        location="Egypt",
        description="desc",
        posted_at="2026-10-10",
        discovered_at=datetime.now(UTC),
        apply_method=apply_method,
    )


def test_url_normalization() -> None:
    url = "https://www.linkedin.com/jobs/view/12345/?refId=abc#fragment"
    assert normalize_url(url) == "https://www.linkedin.com/jobs/view/12345"


def test_duplicate_job_prevention_with_external_id(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    first_id, first_is_new = db.upsert_job(make_job("https://www.linkedin.com/jobs/view/12345", "12345"))
    second_id, second_is_new = db.upsert_job(
        make_job("https://www.linkedin.com/jobs/view/12345?tracking=1", "12345", title="Updated")
    )

    assert first_is_new is True
    assert second_is_new is False
    assert first_id == second_id
    row = db.get_job(first_id)
    assert row is not None
    assert row["title"] == "Backend Engineer"


def test_duplicate_job_prevention_with_fallback_hash(tmp_path) -> None:
    db = Database(tmp_path / "test.db")

    job1 = make_job("https://www.linkedin.com/jobs/view/999/?x=1", None)
    job2 = make_job("https://www.linkedin.com/jobs/view/999/?x=2", None)

    first_id, first_new = db.upsert_job(job1)
    second_id, second_new = db.upsert_job(job2)

    assert first_new is True
    assert second_new is False
    assert first_id == second_id
    assert fallback_external_id(job1.url) == fallback_external_id(job2.url)


def test_database_crud_and_job_status_transitions(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    job_id, _ = db.upsert_job(make_job("https://www.linkedin.com/jobs/view/111", "111"))

    db.update_job_status(job_id, JobStatus.QUEUED)
    db.update_job_status(job_id, JobStatus.READY_FOR_REVIEW)
    db.update_job_status(job_id, JobStatus.APPLIED)

    app_id = db.create_application(job_id, ApplicationStatus.QUEUED, notes="manual queue")
    db.update_application_status(app_id, ApplicationStatus.APPLIED)

    row = db.get_job(job_id)
    assert row is not None
    assert row["status"] == JobStatus.APPLIED.value

    counts = db.status_counts()
    assert counts["TOTAL_JOBS"] == 1
    assert counts["TOTAL_APPLICATIONS"] == 1
    assert counts["JOB_APPLIED"] == 1
    assert counts["APPLICATION_APPLIED"] == 1


def test_apply_method_persist_and_fill_if_unknown(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    job_id, _ = db.upsert_job(
        make_job(
            "https://www.linkedin.com/jobs/view/222",
            "222",
            apply_method=ApplyMethod.UNKNOWN.value,
        )
    )

    db.upsert_job(
        make_job(
            "https://www.linkedin.com/jobs/view/222?trk=abc",
            "222",
            apply_method=ApplyMethod.EASY_APPLY.value,
        )
    )

    row = db.get_job(job_id)
    assert row is not None
    assert row["apply_method"] == ApplyMethod.EASY_APPLY.value


def test_scoring_persisted_and_ready_for_review_threshold(tmp_path) -> None:
    db = Database(tmp_path / "test.db", application_score_threshold=60)
    job = make_job(
        "https://www.linkedin.com/jobs/view/333",
        "333",
        title="Java Developer",
    )
    job.description = "JavaScript only"
    job_id, _ = db.upsert_job(job)

    row = db.get_job(job_id)
    assert row is not None
    assert int(row["score"]) == 60
    assert row["status"] == JobStatus.NEW.value
    keywords = json.loads(row["score_keywords"])
    assert "title:java" in keywords
    assert "description:java" not in keywords


def test_backfill_job_scores_updates_existing_rows(tmp_path) -> None:
    db = Database(tmp_path / "test.db", application_score_threshold=60)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                source, external_id, url, title, company, location, description,
                posted_at, discovered_at, apply_method, score, score_keywords, scored_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "linkedin",
                "444",
                "https://www.linkedin.com/jobs/view/444",
                "Backend Engineer",
                "Acme",
                "Egypt",
                "Java and Spring Framework",
                None,
                datetime.now(UTC).isoformat(),
                ApplyMethod.UNKNOWN.value,
                0,
                "[]",
                None,
                JobStatus.NEW.value,
            ),
        )
        job_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    updated = db.backfill_job_scores(limit=10)
    assert updated == 1

    row = db.get_job(job_id)
    assert row is not None
    assert int(row["score"]) == 60
    assert row["status"] == JobStatus.NEW.value
    assert row["scored_at"]


def test_configuration_validation_rejects_non_localhost_cdp() -> None:
    cfg = Config(
        automation_enabled=True,
        chrome_executable_path=None,
        chrome_debug_port=9222,
        chrome_cdp_url="http://example.com:9222",
        chrome_user_data_dir=Path("./data/chrome-profile"),
        keep_chrome_open=True,
        db_path=Path("./data/jobs.db"),
        search_interval_minutes=15,
        search_last_24h_only=True,
        application_score_threshold=60,
        telegram_enabled=False,
        telegram_bot_token=None,
        telegram_relevant_jobs_chat_id=None,
        telegram_non_relevant_jobs_chat_id=None,
        telegram_applied_jobs_chat_id=None,
        telegram_relevant_score_threshold=30,
        search_queries=[SearchQuery("Java", "Egypt")],
    )

    with pytest.raises(ValueError, match="localhost"):
        validate_config(cfg)


def test_configuration_validation_rejects_invalid_threshold() -> None:
    cfg = Config(
        automation_enabled=True,
        chrome_executable_path=None,
        chrome_debug_port=9222,
        chrome_cdp_url="http://127.0.0.1:9222",
        chrome_user_data_dir=Path("./data/chrome-profile"),
        keep_chrome_open=True,
        db_path=Path("./data/jobs.db"),
        search_interval_minutes=15,
        search_last_24h_only=True,
        application_score_threshold=120,
        telegram_enabled=False,
        telegram_bot_token=None,
        telegram_relevant_jobs_chat_id=None,
        telegram_non_relevant_jobs_chat_id=None,
        telegram_applied_jobs_chat_id=None,
        telegram_relevant_score_threshold=30,
        search_queries=[SearchQuery("Java", "Egypt")],
    )

    with pytest.raises(ValueError, match="APPLICATION_SCORE_THRESHOLD"):
        validate_config(cfg)


def test_load_config_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CHROME_DEBUG_PORT", "9333")
    monkeypatch.setenv("CHROME_CDP_URL", "http://127.0.0.1:9333")
    monkeypatch.setenv("CHROME_USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("SEARCH_INTERVAL_MINUTES", "15")
    monkeypatch.setenv("SEARCH_LAST_24H_ONLY", "true")
    monkeypatch.setenv("APPLICATION_SCORE_THRESHOLD", "60")
    monkeypatch.setenv("SEARCH_QUERIES", "Java Backend Engineer|Egypt;Java Software Engineer|Remote")

    cfg = load_config()

    assert cfg.chrome_debug_port == 9333
    assert cfg.chrome_cdp_url == "http://127.0.0.1:9333"
    assert cfg.search_last_24h_only is True
    assert cfg.application_score_threshold == 60
    assert len(cfg.search_queries) == 2
    assert cfg.search_queries[0].keyword == "Java Backend Engineer"
