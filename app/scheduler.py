from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class SearchScheduler:
    def __init__(self, interval_minutes: int):
        self.delay_minutes = interval_minutes
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

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
        self._stop_event.clear()
        while not self._stop_event.is_set():
            try:
                self.run_non_overlapping(run_once)
            except Exception:
                logger.exception(
                    "Search cycle failed. The next cycle will start after the normal %d-minute delay",
                    self.delay_minutes,
                )
            if self._stop_event.is_set():
                break
            logger.info(
                "Search cycle finished. Waiting %d minutes before the next cycle",
                self.delay_minutes,
            )
            self._stop_event.wait(self.delay_minutes * 60)

    def stop(self) -> None:
        self._stop_event.set()
