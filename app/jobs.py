from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List

from .database import Database
from .linkedin import AccessRestrictionError, LinkedInClient
from .models import ApplyMethod, DiscoveredJob, SearchQuery

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SearchCycleResult:
    query: SearchQuery
    found: int
    inserted: int
    duplicates: int


class JobDiscoveryService:
    def __init__(self, database: Database, linkedin_client: LinkedInClient):
        self.database = database
        self.linkedin_client = linkedin_client

    def run_cycle(self, queries: List[SearchQuery]) -> List[SearchCycleResult]:
        results: list[SearchCycleResult] = []
        start = time.time()

        for query in queries:
            q_start = time.time()
            logger.info("Starting search: keyword='%s', location='%s'", query.keyword, query.location)
            try:
                jobs = self.linkedin_client.search_jobs(query)
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
        self._backfill_missing_metadata()
        rescored = self.database.backfill_job_scores(limit=500)
        if rescored:
            logger.info("Score backfill complete. rescored_jobs=%d", rescored)
        return results

    def _backfill_missing_metadata(self) -> None:
        rows = self.database.list_jobs_needing_metadata(limit=50)
        if not rows:
            return

        logger.info("Starting metadata backfill for %d existing jobs", len(rows))
        jobs = []
        for row in rows:
            jobs.append(
                DiscoveredJob(
                    source=row["source"],
                    external_id=row["external_id"],
                    url=row["url"],
                    title=row["title"],
                    company=row["company"] or "Unknown company",
                    location=row["location"] or "",
                    description=row["description"],
                    posted_at=row["posted_at"],
                    discovered_at=datetime.fromisoformat(row["discovered_at"]),
                    apply_method=row["apply_method"] or ApplyMethod.UNKNOWN.value,
                )
            )

        self.linkedin_client.enrich_metadata_for_jobs(jobs)
        updated = 0
        for row, job in zip(rows, jobs):
            before_title = (row["title"] or "").strip()
            before_company = (row["company"] or "").strip()
            before_posted = (row["posted_at"] or "").strip()
            before_apply = (row["apply_method"] or "").strip()
            self.database.update_job_metadata_if_missing(
                int(row["id"]),
                job.title,
                job.company,
                job.description,
                job.posted_at,
                job.apply_method,
            )
            if (not before_title or before_title == "Unknown title") and job.title != "Unknown title":
                updated += 1
            elif (not before_company or before_company == "Unknown company") and job.company != "Unknown company":
                updated += 1
            elif (not (row["description"] or "").strip()) and (job.description or "").strip():
                updated += 1
            elif (not before_posted) and job.posted_at:
                updated += 1
            elif before_apply in {"", ApplyMethod.UNKNOWN.value} and job.apply_method != ApplyMethod.UNKNOWN.value:
                updated += 1
        logger.info("Metadata backfill complete. updated_jobs=%d", updated)
