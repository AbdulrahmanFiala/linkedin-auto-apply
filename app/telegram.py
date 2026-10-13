from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from urllib import error, parse, request

from .models import JobStatus

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TelegramJobNotifier:
    bot_token: str
    relevant_jobs_chat_id: str
    non_relevant_jobs_chat_id: str
    relevant_score_threshold: int = 30
    max_retries: int = 3
    fallback_retry_delay_seconds: int = 5

    def target_status_for_score(self, score: int) -> JobStatus:
        if score >= self.relevant_score_threshold:
            return JobStatus.SENT_TO_RELEVANT_JOBS
        return JobStatus.SENT_TO_NON_RELEVANT_JOBS

    def target_channel_label_for_score(self, score: int) -> str:
        if score >= self.relevant_score_threshold:
            return "Relevant Jobs"
        return "Non-Relevant Jobs"

    def target_chat_id_for_score(self, score: int) -> str:
        if score >= self.relevant_score_threshold:
            return self.relevant_jobs_chat_id
        return self.non_relevant_jobs_chat_id

    def send_job(self, row) -> bool:
        score = int(row["score"] or 0)
        chat_id = self.target_chat_id_for_score(score)
        text = self._format_message(row, score)
        return self._send_text(chat_id, text)

    def send_jobs_batch(self, rows: list) -> bool:
        if not rows:
            return True
        score = int(rows[0]["score"] or 0)
        chat_id = self.target_chat_id_for_score(score)
        separator = "\n" + ("-" * 40) + "\n"
        text = separator.join(self._format_message(row, int(row["score"] or 0)) for row in rows)
        return self._send_text(chat_id, text)

    def _format_message(self, row, score: int) -> str:
        title = (row["title"] or "Unknown title").strip()
        company = (row["company"] or "Unknown company").strip()
        location = (row["location"] or "").strip()
        url = (row["url"] or "").strip()
        channel = self.target_channel_label_for_score(score)
        lines = [
            f"{title}",
            f"Company: {company}",
            f"Location: {location}",
            f"Score: {score}",
            f"Target Channel: {channel}",
        ]
        if url:
            lines.append(f"URL: {url}")
        return "\n".join(lines)


    def _send_text(self, chat_id: str, text: str) -> bool:
        endpoint = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode(
            "utf-8"
        )
        req = request.Request(endpoint, data=payload, method="POST")
        for attempt in range(1, self.max_retries + 1):
            try:
                with request.urlopen(req, timeout=15) as resp:  # nosec B310
                    body = resp.read().decode("utf-8", errors="replace")
                    if resp.status < 200 or resp.status >= 300:
                        logger.warning("Telegram send failed with HTTP %s: %s", resp.status, body)
                        return False
                    parsed = json.loads(body)
                    ok = bool(parsed.get("ok")) if isinstance(parsed, dict) else False
                    if not ok:
                        logger.warning("Telegram send failed with API response: %s", body)
                    return ok
            except error.HTTPError as exc:
                response_body = exc.read().decode("utf-8", errors="replace")
                retry_after = self._extract_retry_after(response_body)
                if exc.code == 429 and attempt < self.max_retries:
                    delay = retry_after or self.fallback_retry_delay_seconds
                    logger.warning(
                        "Telegram rate limit hit (429). Retrying in %ss (attempt %d/%d)",
                        delay,
                        attempt,
                        self.max_retries,
                    )
                    time.sleep(delay)
                    continue
                logger.warning("Telegram send failed with HTTP %s: %s", exc.code, response_body)
                return False
            except error.URLError as exc:
                logger.warning("Telegram send failed due to network error: %s", exc)
                return False
            except Exception:
                logger.exception("Telegram send failed with unexpected error")
                return False
        return False

    def _extract_retry_after(self, response_body: str) -> int | None:
        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        parameters = parsed.get("parameters")
        if not isinstance(parameters, dict):
            return None
        retry_after = parameters.get("retry_after")
        if isinstance(retry_after, int) and retry_after > 0:
            return retry_after
        return None
