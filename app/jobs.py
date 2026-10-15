from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import List

from .database import Database
from .linkedin import AccessRestrictionError, LinkedInClient
from .models import DiscoveredJob, JobStatus, SearchQuery

logger = logging.getLogger(__name__)
BACKFILL_PER_JOB_DELAY_SECONDS = 0.8


@dataclass(slots=True)
class SearchCycleResult:
    query: SearchQuery
    found: int
    inserted: int
    duplicates: int


class JobDiscoveryService:
    def __init__(self, database: Database, linkedin_client: LinkedInClient, telegram_notifier=None):
        self.database = database
        self.linkedin_client = linkedin_client
        self.telegram_notifier = telegram_notifier

    def run_cycle(self, queries: List[SearchQuery]) -> List[SearchCycleResult]:
        results: list[SearchCycleResult] = []
        start = time.time()

        for query in queries:
            q_start = time.time()
            logger.info("Starting search: keyword='%s', location='%s'", query.keyword, query.location)
            try:
                jobs = self.linkedin_client.search_jobs(query)
                new_jobs, existing_jobs_for_enrichment = self.database.split_new_and_existing_jobs(jobs)
                existing_jobs_missing_metadata = self.database.filter_existing_jobs_needing_metadata(
                    existing_jobs_for_enrichment
                )
                if existing_jobs_for_enrichment:
                    logger.info(
                        "Skipping enrichment for existing DB jobs: %d/%d",
                        len(existing_jobs_for_enrichment),
                        len(jobs),
                    )
                if existing_jobs_missing_metadata:
                    logger.info(
                        "Existing DB jobs needing metadata enrichment: %d/%d",
                        len(existing_jobs_missing_metadata),
                        len(existing_jobs_for_enrichment),
                    )

                to_enrich = [*new_jobs, *existing_jobs_missing_metadata]
                if to_enrich:
                    logger.info(
                        "Enriching metadata for jobs: new=%d existing_missing=%d total=%d",
                        len(new_jobs),
                        len(existing_jobs_missing_metadata),
                        len(to_enrich),
                    )
                    self.linkedin_client.enrich_metadata_for_jobs(to_enrich)
                else:
                    logger.info("No jobs require metadata enrichment on this page: 0/%d", len(jobs))
                inserted, new_jobs, existing_jobs = self.database.upsert_jobs_bulk_detailed(jobs)
                found = len(jobs)
                duplicates = max(found - inserted, 0)
                elapsed = time.time() - q_start
                logger.info(
                    "Completed search: keyword='%s', location='%s', returned_from_linkedin=%d, new_inserted=%d, duplicates_existing=%d, duration=%.2fs",
                    query.keyword,
                    query.location,
                    found,
                    inserted,
                    duplicates,
                    elapsed,
                )
                logger.info(
                    "New jobs added (%d): %s",
                    len(new_jobs),
                    "; ".join(new_jobs) if new_jobs else "none",
                )
                logger.info(
                    "Existing jobs already in DB (%d): %s",
                    len(existing_jobs),
                    "; ".join(existing_jobs) if existing_jobs else "none",
                )
                results.append(
                    SearchCycleResult(
                        query=query,
                        found=found,
                        inserted=inserted,
                        duplicates=duplicates,
                    )
                )
            except AccessRestrictionError as exc:
                logger.warning(
                    "Access restriction detected for keyword='%s', location='%s': %s",
                    query.keyword,
                    query.location,
                    exc,
                )
                raise
            except Exception:
                logger.exception(
                    "Search failed: keyword='%s', location='%s'",
                    query.keyword,
                    query.location,
                )

        logger.info("Search cycle completed in %.2fs", time.time() - start)
        self._dispatch_new_jobs_to_telegram()
        rescored = self.database.backfill_job_scores(limit=500)
        if rescored:
            logger.info("Score backfill complete. rescored_jobs=%d", rescored)
        return results

    def run_missing_metadata_backfill(self, limit: int = 200) -> int:
        rows = self.database.list_jobs_needing_metadata(limit=limit)
        if not rows:
            logger.info("No jobs with missing metadata found for backfill")
            return 0

        logger.info("Starting standalone metadata backfill for %d jobs", len(rows))
        jobs_with_ids: list[tuple[int, DiscoveredJob]] = []
        for row in rows:
            discovered_at_raw = row["discovered_at"]
            discovered_at = datetime.now(UTC)
            if discovered_at_raw:
                try:
                    discovered_at = datetime.fromisoformat(str(discovered_at_raw))
                except ValueError:
                    discovered_at = datetime.now(UTC)

            jobs_with_ids.append(
                (
                    int(row["id"]),
                    DiscoveredJob(
                        source=str(row["source"] or "linkedin"),
                        external_id=str(row["external_id"]) if row["external_id"] else None,
                        url=str(row["url"] or ""),
                        title=str(row["title"] or "Unknown title"),
                        company=str(row["company"] or "Unknown company"),
                        location=str(row["location"] or ""),
                        description=str(row["description"]) if row["description"] else None,
                        posted_at=str(row["posted_at"]) if row["posted_at"] else None,
                        discovered_at=discovered_at,
                        apply_method=str(row["apply_method"] or "UNKNOWN"),
                    ),
                )
            )

        enrich_targets = [job for _, job in jobs_with_ids if job.url]
        if enrich_targets:
            if len(enrich_targets) > 1:
                logger.info(
                    "Applying metadata backfill latency between jobs: %.2fs",
                    BACKFILL_PER_JOB_DELAY_SECONDS,
                )
            for index, job in enumerate(enrich_targets):
                if index > 0:
                    time.sleep(BACKFILL_PER_JOB_DELAY_SECONDS)
                logger.info("Detail enrichment progress: %d/%d", index + 1, len(enrich_targets))
                self.linkedin_client.enrich_metadata_for_jobs([job])

        for job_id, job in jobs_with_ids:
            self.database.update_job_metadata_if_missing(
                job_id=job_id,
                title=job.title,
                company=job.company,
                description=job.description,
                posted_at=job.posted_at,
                apply_method=job.apply_method,
            )

        logger.info("Standalone metadata backfill finished. processed_jobs=%d", len(jobs_with_ids))
        return len(jobs_with_ids)

    def _dispatch_new_jobs_to_telegram(self) -> None:
        if self.telegram_notifier is None:
            return

        rows = self.database.list_jobs_by_status(JobStatus.NEW, limit=500)
        if not rows:
            logger.info("No NEW jobs pending Telegram delivery")
            return

        logger.info("Starting Telegram delivery for %d NEW jobs", len(rows))
        delivered = 0
        failed = 0
        grouped_rows: dict[str, list] = {}
        for row in rows:
            score = int(row["score"] or 0)
            channel_label = self.telegram_notifier.target_channel_label_for_score(score)
            grouped_rows.setdefault(channel_label, []).append(row)

        for channel_label, channel_rows in grouped_rows.items():
            if not self.telegram_notifier.send_jobs_batch(channel_rows):
                failed += len(channel_rows)
                logger.warning(
                    "Telegram delivery failed for %d jobs target_channel='%s'",
                    len(channel_rows),
                    channel_label,
                )
                continue

            for row in channel_rows:
                score = int(row["score"] or 0)
                new_status = self.telegram_notifier.target_status_for_score(score)
                self.database.update_job_status(int(row["id"]), new_status)
                delivered += 1

        logger.info(
            "Telegram delivery completed. delivered=%d failed=%d remaining_new=%d",
            delivered,
            failed,
            len(rows) - delivered,
        )
