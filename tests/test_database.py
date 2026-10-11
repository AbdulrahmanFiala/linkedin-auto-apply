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
        search_queries=[SearchQuery("Java", "Egypt")],
    )

    with pytest.raises(ValueError, match="localhost"):
        validate_config(cfg)


def test_load_config_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CHROME_DEBUG_PORT", "9333")
    monkeypatch.setenv("CHROME_CDP_URL", "http://127.0.0.1:9333")
    monkeypatch.setenv("CHROME_USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("SEARCH_INTERVAL_MINUTES", "15")
    monkeypatch.setenv("SEARCH_LAST_24H_ONLY", "true")
    monkeypatch.setenv("SEARCH_QUERIES", "Java Backend Engineer|Egypt;Java Software Engineer|Remote")

    cfg = load_config()

    assert cfg.chrome_debug_port == 9333
    assert cfg.chrome_cdp_url == "http://127.0.0.1:9333"
    assert cfg.search_last_24h_only is True
    assert len(cfg.search_queries) == 2
    assert cfg.search_queries[0].keyword == "Java Backend Engineer"
