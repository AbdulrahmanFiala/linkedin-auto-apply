from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from .models import ApplicationStatus, ApplyMethod, DiscoveredJob, JobStatus, fallback_external_id

T = TypeVar("T")
logger = logging.getLogger(__name__)


class Database:
    LOCK_RETRY_ATTEMPTS = 4
    LOCK_RETRY_DELAY_SECONDS = 0.35

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._schema_exists():
            self._run_with_lock_retry(self._initialize)
        self._run_with_lock_retry(self._ensure_jobs_apply_method_column)
        self._run_with_lock_retry(self._normalize_posted_at_placeholders)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _run_with_lock_retry(self, action: Callable[[], T]) -> T:
        last_error: Exception | None = None
        for attempt in range(1, self.LOCK_RETRY_ATTEMPTS + 1):
            try:
                return action()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                last_error = exc
                if attempt == self.LOCK_RETRY_ATTEMPTS:
                    break
                delay = self.LOCK_RETRY_DELAY_SECONDS * attempt
                logger.warning(
                    "SQLite locked (attempt %d/%d). Retrying in %.2fs",
                    attempt,
                    self.LOCK_RETRY_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
        if last_error:
            raise sqlite3.OperationalError(
                "SQLite database remained locked after retries. "
                "Close other DB tools or write sessions and retry."
            ) from last_error
        raise RuntimeError("Unexpected retry state")

    def _initialize(self) -> None:
        with self.transaction() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    description TEXT,
                    posted_at TEXT,
                    discovered_at TEXT NOT NULL,
                    apply_method TEXT NOT NULL DEFAULT 'UNKNOWN',
                    status TEXT NOT NULL,
                    UNIQUE(source, external_id)
                );

                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    notes TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                """
            )

    def _ensure_jobs_apply_method_column(self) -> None:
        with self.transaction() as conn:
            columns = conn.execute("PRAGMA table_info(jobs)").fetchall()
            names = {str(row["name"]) for row in columns}
            if "apply_method" not in names:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN apply_method TEXT NOT NULL DEFAULT 'UNKNOWN'"
                )

    def _normalize_posted_at_placeholders(self) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET posted_at = NULL
                WHERE posted_at IS NOT NULL
                  AND TRIM(posted_at) != ''
                  AND posted_at = discovered_at
                """
            )

    def _schema_exists(self) -> bool:
        if not self.db_path.exists():
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM sqlite_master
                WHERE type='table' AND name IN ('jobs', 'applications')
                """
            ).fetchone()
            return bool(row and int(row[0]) == 2)

    def _upsert_job_in_conn(self, conn: sqlite3.Connection, job: DiscoveredJob) -> tuple[int, bool]:
        ext_id = job.external_id or fallback_external_id(job.url)
        existing = conn.execute(
            """
            SELECT id, title, company, posted_at, discovered_at, apply_method
            FROM jobs
            WHERE source = ? AND external_id = ?
            """,
            (job.source, ext_id),
        ).fetchone()
        if existing:
            updates: list[str] = []
            values: list[str] = []

            existing_title = (existing["title"] or "").strip()
            if existing_title in {"", "Unknown title"} and job.title and job.title != "Unknown title":
                updates.append("title = ?")
                values.append(job.title)

            existing_company = (existing["company"] or "").strip()
            if existing_company in {"", "Unknown company"} and job.company and job.company != "Unknown company":
                updates.append("company = ?")
                values.append(job.company)

            existing_posted = (existing["posted_at"] or "").strip()
            existing_discovered = (existing["discovered_at"] or "").strip()
            if (not existing_posted or existing_posted == existing_discovered) and job.posted_at:
                updates.append("posted_at = ?")
                values.append(job.posted_at)

            existing_apply = (existing["apply_method"] or "").strip()
            if (
                existing_apply in {"", ApplyMethod.UNKNOWN.value}
                and job.apply_method
                and job.apply_method != ApplyMethod.UNKNOWN.value
            ):
                updates.append("apply_method = ?")
                values.append(job.apply_method)

            if updates:
                values.append(existing["id"])
                conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", tuple(values))
            return int(existing["id"]), False

        cursor = conn.execute(
            """
            INSERT INTO jobs (
                source, external_id, url, title, company, location,
                description, posted_at, discovered_at, apply_method, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.source,
                ext_id,
                job.url,
                job.title,
                job.company,
                job.location,
                job.description,
                job.posted_at,
                job.discovered_at.isoformat(),
                job.apply_method or ApplyMethod.UNKNOWN.value,
                JobStatus.NEW.value,
            ),
        )
        return int(cursor.lastrowid), True

    def upsert_jobs_bulk(self, jobs: list[DiscoveredJob]) -> int:
        inserted, _, _ = self.upsert_jobs_bulk_detailed(jobs)
        return inserted

    def upsert_jobs_bulk_detailed(self, jobs: list[DiscoveredJob]) -> tuple[int, list[str], list[str]]:
        def action() -> tuple[int, list[str], list[str]]:
            inserted = 0
            new_jobs: list[str] = []
            existing_jobs: list[str] = []
            with self.transaction() as conn:
                for job in jobs:
                    ref = job.external_id or fallback_external_id(job.url)
                    marker = f"{ref} | {job.title} | {job.company}"
                    _, is_new = self._upsert_job_in_conn(conn, job)
                    if is_new:
                        inserted += 1
                        new_jobs.append(marker)
                    else:
                        existing_jobs.append(marker)
            return inserted, new_jobs, existing_jobs

        return self._run_with_lock_retry(action)

    def upsert_job(self, job: DiscoveredJob) -> tuple[int, bool]:
        def action() -> tuple[int, bool]:
            with self.transaction() as conn:
                return self._upsert_job_in_conn(conn, job)

        return self._run_with_lock_retry(action)

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def update_job_status(self, job_id: int, status: JobStatus) -> None:
        def action() -> None:
            with self.transaction() as conn:
                conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status.value, job_id))

        self._run_with_lock_retry(action)

    def create_application(self, job_id: int, status: ApplicationStatus, notes: str | None = None) -> int:
        def action() -> int:
            now = datetime.now(UTC).isoformat()
            with self.transaction() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO applications (job_id, status, created_at, updated_at, notes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (job_id, status.value, now, now, notes),
                )
                return int(cursor.lastrowid)

        return self._run_with_lock_retry(action)

    def update_application_status(self, application_id: int, status: ApplicationStatus) -> None:
        def action() -> None:
            with self.transaction() as conn:
                conn.execute(
                    "UPDATE applications SET status = ?, updated_at = ? WHERE id = ?",
                    (status.value, datetime.now(UTC).isoformat(), application_id),
                )

        self._run_with_lock_retry(action)

    def status_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            counts: dict[str, int] = {}
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall():
                counts[f"JOB_{row['status']}"] = int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM applications GROUP BY status"
            ).fetchall():
                counts[f"APPLICATION_{row['status']}"] = int(row["count"])
            counts["TOTAL_JOBS"] = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            counts["TOTAL_APPLICATIONS"] = int(
                conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
            )
            return counts

    def list_jobs_needing_metadata(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, source, external_id, url, title, company, location, description, posted_at, discovered_at, apply_method
                FROM jobs
                WHERE (posted_at IS NULL OR TRIM(posted_at) = '')
                   OR (title IS NULL OR TRIM(title) = '' OR title = 'Unknown title')
                   OR (company IS NULL OR TRIM(company) = '' OR company = 'Unknown company')
                   OR (apply_method IS NULL OR TRIM(apply_method) = '' OR apply_method = 'UNKNOWN')
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def update_job_metadata_if_missing(
        self,
        job_id: int,
        title: str | None,
        company: str | None,
        posted_at: str | None,
        apply_method: str | None,
    ) -> None:
        def action() -> None:
            with self.transaction() as conn:
                if title and title.strip() and title != "Unknown title":
                    conn.execute(
                        """
                        UPDATE jobs
                        SET title = ?
                        WHERE id = ?
                          AND (title IS NULL OR TRIM(title) = '' OR title = 'Unknown title')
                        """,
                        (title.strip(), job_id),
                    )
                if company and company.strip() and company != "Unknown company":
                    conn.execute(
                        """
                        UPDATE jobs
                        SET company = ?
                        WHERE id = ?
                          AND (company IS NULL OR TRIM(company) = '' OR company = 'Unknown company')
                        """,
                        (company.strip(), job_id),
                    )
                if posted_at and posted_at.strip():
                    conn.execute(
                        """
                        UPDATE jobs
                        SET posted_at = ?
                        WHERE id = ?
                          AND (posted_at IS NULL OR TRIM(posted_at) = '')
                        """,
                        (posted_at.strip(), job_id),
                    )
                if (
                    apply_method
                    and apply_method.strip()
                    and apply_method.strip() != ApplyMethod.UNKNOWN.value
                ):
                    conn.execute(
                        """
                        UPDATE jobs
                        SET apply_method = ?
                        WHERE id = ?
                          AND (apply_method IS NULL OR TRIM(apply_method) = '' OR apply_method = 'UNKNOWN')
                        """,
                        (apply_method.strip(), job_id),
                    )

        self._run_with_lock_retry(action)
