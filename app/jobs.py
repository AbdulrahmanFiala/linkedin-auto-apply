from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List

from .database import Database
from .linkedin import AccessRestrictionError, LinkedInClient
from .models import JobStatus, SearchQuery

logger = logging.getLogger(__name__)


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
                if existing_jobs_for_enrichment:
                    logger.info(
                        "Skipping enrichment for existing DB jobs: %d/%d",
                        len(existing_jobs_for_enrichment),
                        len(jobs),
                    )
                if new_jobs:
                    logger.info("Enriching metadata only for new DB jobs: %d/%d", len(new_jobs), len(jobs))
                    self.linkedin_client.enrich_metadata_for_jobs(new_jobs)
                else:
                    logger.info("No new DB jobs to enrich on this page: 0/%d", len(jobs))
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
