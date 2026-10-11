from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .browser import BrowserConnector, cleanup_playwright
from .chrome import ChromeStartupError, ensure_chrome_running
from .config import load_config
from .database import Database
from .jobs import JobDiscoveryService
from .linkedin import AccessRestrictionError, LinkedInClient
from .scheduler import SearchScheduler


logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LinkedIn job discovery bot")
    parser.add_argument("--setup", action="store_true", help="Launch Chrome and open LinkedIn")
    parser.add_argument("--once", action="store_true", help="Run one search cycle and exit")
    parser.add_argument("--status", action="store_true", help="Show database status and exit")
    return parser.parse_args()


def show_status(db: Database) -> None:
    counts = db.status_counts()
    print("Database status")
    for key in sorted(counts.keys()):
        print(f"{key}: {counts[key]}")


def run_once(config, db: Database, linkedin: LinkedInClient) -> int:
    service = JobDiscoveryService(db, linkedin)
    try:
        service.run_cycle(config.search_queries)
        return 0
    except AccessRestrictionError:
        logger.warning("Stopping cycle due to access restriction")
        return 2


def main() -> int:
    configure_logging()
    args = parse_args()
    config = load_config()

    if not config.automation_enabled and not args.status:
        logger.warning("AUTOMATION_ENABLED=false. Exiting without running automation.")
        return 0

    db = Database(config.db_path)

    if args.status:
        show_status(db)
        return 0

    chrome_handle = None
    playwright = browser = context = None
    scheduler = None
    try:
        chrome_handle = ensure_chrome_running(config, open_linkedin=args.setup)

        if args.setup:
            print("Chrome launched for manual LinkedIn login. Press Ctrl+C when done.")
            while True:
                time.sleep(1)

        connector = BrowserConnector(config)
        playwright, browser, context = connector.connect()
        linkedin = LinkedInClient(context, config)

        if args.once:
            return run_once(config, db, linkedin)

        scheduler = SearchScheduler(config.search_interval_minutes)
        stop = False

        def handle_signal(*_):
            nonlocal stop
            stop = True

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        scheduler.start(lambda: run_once(config, db, linkedin))
        while not stop:
            time.sleep(1)
        return 0
    except KeyboardInterrupt:
        return 0
    except ChromeStartupError as exc:
        logger.error("Chrome startup error: %s", exc)
        return 1
    except Exception:
        logger.exception("Fatal error")
        return 1
    finally:
        if scheduler:
            scheduler.stop()
        if playwright and browser:
            cleanup_playwright(playwright, browser)
        if chrome_handle:
            chrome_handle.terminate_if_needed(config.keep_chrome_open)


if __name__ == "__main__":
    sys.exit(main())
