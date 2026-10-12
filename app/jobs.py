from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List

from .database import Database
from .linkedin import AccessRestrictionError, LinkedInClient
from .models import SearchQuery

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
        rescored = self.database.backfill_job_scores(limit=500)
        if rescored:
            logger.info("Score backfill complete. rescored_jobs=%d", rescored)
        return results
