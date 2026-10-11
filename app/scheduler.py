from __future__ import annotations

import logging
import threading
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)


class SearchScheduler:
    def __init__(self, interval_minutes: int):
        self.interval_minutes = interval_minutes
        self._scheduler = BackgroundScheduler()
        self._lock = threading.Lock()

    def run_non_overlapping(self, run_once: Callable[[], None]) -> bool:
        if not self._lock.acquire(blocking=False):
            logger.warning("Skipping search cycle because previous cycle is still running")
            return False
        try:
            run_once()
            return True
        finally:
            self._lock.release()

    def start(self, run_once: Callable[[], None]) -> None:
        self.run_non_overlapping(run_once)
        self._scheduler.add_job(
            lambda: self.run_non_overlapping(run_once),
            "interval",
            minutes=self.interval_minutes,
            id="linkedin-search",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        self._scheduler.start()

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
