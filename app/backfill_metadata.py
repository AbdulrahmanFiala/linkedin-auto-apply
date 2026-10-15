from __future__ import annotations

import argparse
import logging
import sys

from .browser import BrowserConnector, cleanup_playwright
from .chrome import ChromeStartupError, ensure_chrome_running
from .config import load_config
from .database import Database
from .jobs import JobDiscoveryService
from .linkedin import LinkedInClient


logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill missing job metadata")
    parser.add_argument("--limit", type=int, default=200, help="Maximum jobs to process in one run")
    return parser.parse_args()


def main() -> int:
    configure_logging()
    args = parse_args()
    config = load_config()
    db = Database(config.db_path, application_score_threshold=config.application_score_threshold)

    chrome_handle = None
    playwright = browser = context = None
    try:
        chrome_handle = ensure_chrome_running(config, open_linkedin=False)
        connector = BrowserConnector(config)
        playwright, browser, context = connector.connect()
        linkedin = LinkedInClient(context, config)
        service = JobDiscoveryService(db, linkedin)

        processed = service.run_missing_metadata_backfill(limit=max(args.limit, 1))
        logger.info("Missing metadata backfill completed. processed_jobs=%d", processed)
        return 0
    except ChromeStartupError as exc:
        logger.error("Chrome startup error: %s", exc)
        return 1
    except Exception:
        logger.exception("Metadata backfill failed")
        return 1
    finally:
        if playwright and browser:
            cleanup_playwright(playwright, browser)
        if chrome_handle:
            chrome_handle.terminate_if_needed(config.keep_chrome_open)


if __name__ == "__main__":
    sys.exit(main())
