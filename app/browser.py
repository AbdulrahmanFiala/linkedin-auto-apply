from __future__ import annotations

import logging
import time

from playwright.sync_api import Browser, sync_playwright

from .config import Config

logger = logging.getLogger(__name__)


class BrowserConnector:
    def __init__(self, config: Config):
        self.config = config

    def connect(self, retries: int = 3, delay_seconds: float = 1.5) -> tuple:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            pw = sync_playwright().start()
            try:
                browser = pw.chromium.connect_over_cdp(self.config.chrome_cdp_url)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                return pw, browser, context
            except Exception as exc:
                pw.stop()
                last_error = exc
                logger.warning("CDP connection attempt %d/%d failed: %s", attempt, retries, exc)
                time.sleep(delay_seconds)

        raise RuntimeError(
            f"Failed to connect to Chrome CDP after {retries} attempts: {last_error}"
        )


def cleanup_playwright(playwright, browser: Browser) -> None:
    try:
        browser.close()
    except Exception:
        logger.debug("Ignoring browser close error", exc_info=True)
    finally:
        playwright.stop()
