from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from dotenv import load_dotenv

from .models import SearchQuery


@dataclass(slots=True)
class Config:
    automation_enabled: bool
    chrome_executable_path: str | None
    chrome_debug_port: int
    chrome_cdp_url: str
    chrome_user_data_dir: Path
    keep_chrome_open: bool
    db_path: Path
    search_interval_minutes: int
    search_last_24h_only: bool
    application_score_threshold: int
    telegram_enabled: bool
    telegram_bot_token: str | None
    telegram_relevant_jobs_chat_id: str | None
    telegram_non_relevant_jobs_chat_id: str | None
    telegram_relevant_score_threshold: int
    search_queries: List[SearchQuery]


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {name}: {value}") from exc


def _queries_from_env() -> List[SearchQuery]:
    raw = os.getenv("SEARCH_QUERIES")
    if not raw:
        return [
            SearchQuery(keyword="Java or Full Stack or Software Engineer posted in the past 24 hours", location="Egypt"),
        ]

    queries: list[SearchQuery] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "|" not in part:
            raise ValueError("SEARCH_QUERIES format must be keyword|location;keyword|location")
        keyword, location = [x.strip() for x in part.split("|", 1)]
        if not keyword or not location:
            raise ValueError("SEARCH_QUERIES entries require both keyword and location")
        queries.append(SearchQuery(keyword=keyword, location=location))

    if not queries:
        raise ValueError("SEARCH_QUERIES produced no valid entries")
    return queries


def validate_config(config: Config) -> None:
    if config.chrome_debug_port <= 0:
        raise ValueError("CHROME_DEBUG_PORT must be > 0")
    if config.search_interval_minutes <= 0:
        raise ValueError("SEARCH_INTERVAL_MINUTES must be > 0")
    if not config.search_queries:
        raise ValueError("At least one search query is required")
    if config.application_score_threshold < 0 or config.application_score_threshold > 90:
        raise ValueError("APPLICATION_SCORE_THRESHOLD must be between 0 and 90")
    if config.telegram_relevant_score_threshold < 0 or config.telegram_relevant_score_threshold > 100:
        raise ValueError("TELEGRAM_RELEVANT_SCORE_THRESHOLD must be between 0 and 100")

    parsed = urlparse(config.chrome_cdp_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("CHROME_CDP_URL must point to localhost only")
    if config.telegram_enabled:
        if not config.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required when TELEGRAM_ENABLED=true")
        if not config.telegram_relevant_jobs_chat_id:
            raise ValueError("TELEGRAM_RELEVANT_JOBS_CHAT_ID is required when TELEGRAM_ENABLED=true")
        if not config.telegram_non_relevant_jobs_chat_id:
            raise ValueError("TELEGRAM_NON_RELEVANT_JOBS_CHAT_ID is required when TELEGRAM_ENABLED=true")


def load_config() -> Config:
    load_dotenv()

    root_dir = Path(__file__).resolve().parent.parent
    data_dir = root_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    chrome_debug_port = _int_env("CHROME_DEBUG_PORT", 9222)
    cdp_url = os.getenv("CHROME_CDP_URL", f"http://127.0.0.1:{chrome_debug_port}")

    user_data_default = data_dir / "chrome-profile"
    user_data = Path(os.getenv("CHROME_USER_DATA_DIR", str(user_data_default))).resolve()
    db_path = Path(os.getenv("SQLITE_DB_PATH", str(data_dir / "jobs.db"))).resolve()

    config = Config(
        automation_enabled=_bool_env("AUTOMATION_ENABLED", True),
        chrome_executable_path=os.getenv("CHROME_EXECUTABLE_PATH") or None,
        chrome_debug_port=chrome_debug_port,
        chrome_cdp_url=cdp_url,
        chrome_user_data_dir=user_data,
        keep_chrome_open=_bool_env("KEEP_CHROME_OPEN", True),
        db_path=db_path,
        search_interval_minutes=_int_env("SEARCH_INTERVAL_MINUTES", 5),
        search_last_24h_only=_bool_env("SEARCH_LAST_24H_ONLY", True),
        application_score_threshold=_int_env("APPLICATION_SCORE_THRESHOLD", 60),
        telegram_enabled=_bool_env("TELEGRAM_ENABLED", False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_relevant_jobs_chat_id=os.getenv("TELEGRAM_RELEVANT_JOBS_CHAT_ID") or None,
        telegram_non_relevant_jobs_chat_id=os.getenv("TELEGRAM_NON_RELEVANT_JOBS_CHAT_ID") or None,
        telegram_relevant_score_threshold=_int_env("TELEGRAM_RELEVANT_SCORE_THRESHOLD", 30),
        search_queries=_queries_from_env(),
    )

    validate_config(config)
    return config
