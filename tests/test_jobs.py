import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Config
from app.jobs import JobDiscoveryService
from app.linkedin import AccessRestrictionError, LinkedInClient
from app.models import ApplyMethod, DiscoveredJob, SearchQuery
from app.scheduler import SearchScheduler


class FakeDatabase:
    def __init__(self):
        self.rows = {}
        self.count = 0

    def upsert_job(self, job):
        key = (job.source, job.external_id or job.url)
        if key in self.rows:
            return self.rows[key], False
        self.count += 1
        self.rows[key] = self.count
        return self.count, True

    def upsert_jobs_bulk(self, jobs):
        inserted = 0
        for job in jobs:
            _, is_new = self.upsert_job(job)
            if is_new:
                inserted += 1
        return inserted

    def upsert_jobs_bulk_detailed(self, jobs):
        inserted = 0
        new_jobs = []
        existing_jobs = []
        for job in jobs:
            _, is_new = self.upsert_job(job)
            marker = f"{job.external_id or job.url} | {job.title} | {job.company}"
            if is_new:
                inserted += 1
                new_jobs.append(marker)
            else:
                existing_jobs.append(marker)
        return inserted, new_jobs, existing_jobs

    def split_new_and_existing_jobs(self, jobs):
        new_jobs = []
        existing_jobs = []
        seen_new = set()
        for job in jobs:
            key = (job.source, job.external_id or job.url)
            if key in self.rows:
                existing_jobs.append(job)
                continue
            if key in seen_new:
                continue
            seen_new.add(key)
            new_jobs.append(job)
        return new_jobs, existing_jobs

    def list_jobs_needing_metadata(self, limit=50):
        return []

    def update_job_metadata_if_missing(self, job_id, title, company, description, posted_at, apply_method):
        return None

    def backfill_job_scores(self, limit=500):
        return 0


class FakeLinkedInClient:
    def __init__(self, behavior):
        self.behavior = behavior
        self.enriched_batches = []

    def search_jobs(self, query):
        result = self.behavior[(query.keyword, query.location)]
        if isinstance(result, Exception):
            raise result
        return result

    def enrich_metadata_for_jobs(self, jobs):
        self.enriched_batches.append([job.external_id or job.url for job in jobs])
        return None


def make_discovered(external_id: str) -> DiscoveredJob:
    return DiscoveredJob(
        source="linkedin",
        external_id=external_id,
        url=f"https://www.linkedin.com/jobs/view/{external_id}",
        title="Java Engineer",
        company="Acme",
        location="Egypt",
        description=None,
        posted_at=None,
        discovered_at=datetime.now(UTC),
        apply_method=ApplyMethod.UNKNOWN.value,
    )


def make_config() -> Config:
    return Config(
        automation_enabled=True,
        chrome_executable_path=None,
        chrome_debug_port=9222,
        chrome_cdp_url="http://127.0.0.1:9222",
        chrome_user_data_dir=Path("./data/chrome-profile"),
        keep_chrome_open=True,
        db_path=Path("./data/jobs.db"),
        search_interval_minutes=15,
        search_last_24h_only=True,
        application_score_threshold=60,
        search_queries=[SearchQuery("Java", "Egypt")],
    )


def test_job_service_continues_when_single_query_fails() -> None:
    db = FakeDatabase()
    queries = [SearchQuery("q1", "l1"), SearchQuery("q2", "l2")]
    client = FakeLinkedInClient(
        {
            ("q1", "l1"): RuntimeError("transient"),
            ("q2", "l2"): [make_discovered("1"), make_discovered("2")],
        }
    )

    service = JobDiscoveryService(db, client)
    results = service.run_cycle(queries)

    assert len(results) == 1
    assert results[0].found == 2
    assert results[0].inserted == 2
    assert results[0].duplicates == 0


def test_job_service_reports_duplicates(caplog) -> None:
    db = FakeDatabase()
    query = SearchQuery("q1", "l1")
    jobs = [make_discovered("1"), make_discovered("1"), make_discovered("2")]
    client = FakeLinkedInClient({("q1", "l1"): jobs})

    service = JobDiscoveryService(db, client)
    with caplog.at_level("INFO"):
        results = service.run_cycle([query])

    assert len(results) == 1
    assert results[0].found == 3
    assert results[0].inserted == 2
    assert results[0].duplicates == 1
    assert "duplicates_existing=1" in caplog.text
    assert client.enriched_batches == [["1", "2"]]


def test_job_service_enriches_only_new_jobs_against_existing_db() -> None:
    db = FakeDatabase()
    db.upsert_job(make_discovered("1"))
    query = SearchQuery("q1", "l1")
    jobs = [make_discovered("1"), make_discovered("2"), make_discovered("3")]
    client = FakeLinkedInClient({("q1", "l1"): jobs})

    service = JobDiscoveryService(db, client)
    results = service.run_cycle([query])

    assert len(results) == 1
    assert results[0].found == 3
    assert results[0].inserted == 2
    assert results[0].duplicates == 1
    assert client.enriched_batches == [["2", "3"]]


def test_job_service_logs_skip_when_all_jobs_already_exist(caplog) -> None:
    db = FakeDatabase()
    db.upsert_job(make_discovered("1"))
    db.upsert_job(make_discovered("2"))
    query = SearchQuery("q1", "l1")
    jobs = [make_discovered("1"), make_discovered("2")]
    client = FakeLinkedInClient({("q1", "l1"): jobs})

    service = JobDiscoveryService(db, client)
    with caplog.at_level("INFO"):
        results = service.run_cycle([query])

    assert len(results) == 1
    assert results[0].found == 2
    assert results[0].inserted == 0
    assert results[0].duplicates == 2
    assert client.enriched_batches == []
    assert "Skipping enrichment for existing DB jobs: 2/2" in caplog.text
    assert "No new DB jobs to enrich on this page: 0/2" in caplog.text


def test_job_service_stops_on_access_restriction() -> None:
    db = FakeDatabase()
    queries = [SearchQuery("q1", "l1"), SearchQuery("q2", "l2")]
    client = FakeLinkedInClient({("q1", "l1"): AccessRestrictionError("captcha")})

    service = JobDiscoveryService(db, client)

    with pytest.raises(AccessRestrictionError):
        service.run_cycle(queries)


def test_scheduler_overlap_prevention() -> None:
    scheduler = SearchScheduler(interval_minutes=15)
    calls = []

    def long_run():
        calls.append("start")
        time.sleep(0.2)
        calls.append("end")

    t = threading.Thread(target=lambda: scheduler.run_non_overlapping(long_run))
    t.start()
    time.sleep(0.05)

    ran_second = scheduler.run_non_overlapping(long_run)
    t.join()

    assert ran_second is False
    assert calls == ["start", "end"]


class _FakeLocator:
    def __init__(self, count_value: int):
        self._count_value = count_value

    def count(self):
        return self._count_value


class _SequenceCountLocator:
    def __init__(self, counts: list[int]):
        self._counts = counts
        self._index = 0

    def count(self):
        value = self._counts[min(self._index, len(self._counts) - 1)]
        self._index += 1
        return value


class _FakePage:
    def __init__(self, *, url: str, title: str, content: str, selector_counts: dict[str, int]):
        self.url = url
        self._title = title
        self._content = content
        self._selector_counts = selector_counts

    def title(self):
        return self._title

    def content(self):
        return self._content

    def locator(self, selector: str):
        return _FakeLocator(self._selector_counts.get(selector, 0))


class _FakeDynamicCountPage(_FakePage):
    def __init__(self, *, url: str, title: str, content: str, selector_counts: dict[str, int], cards_counts: list[int]):
        super().__init__(url=url, title=title, content=content, selector_counts=selector_counts)
        self._cards_locator = _SequenceCountLocator(cards_counts)
        self.wait_calls = 0

    def locator(self, selector: str):
        if selector == LinkedInClient.JOB_CARD_SELECTOR:
            return self._cards_locator
        return super().locator(selector)

    def wait_for_timeout(self, _timeout_ms: int):
        self.wait_calls += 1


class _FakeScrollNudgedCountPage(_FakePage):
    def __init__(self, *, url: str, title: str, content: str, selector_counts: dict[str, int], cards_counts: list[int]):
        super().__init__(url=url, title=title, content=content, selector_counts=selector_counts)
        self._cards_counts = cards_counts
        self._index = 0
        self._nudged = False

    class _DynamicCardsLocator:
        def __init__(self, owner: "_FakeScrollNudgedCountPage"):
            self._owner = owner

        def count(self):
            return self._owner._cards_counts[min(self._owner._index, len(self._owner._cards_counts) - 1)]

    def locator(self, selector: str):
        if selector == LinkedInClient.JOB_CARD_SELECTOR:
            return self._DynamicCardsLocator(self)
        return super().locator(selector)

    def evaluate(self, _script: str, _selector: str | None = None):
        self._nudged = True
        return True

    def wait_for_timeout(self, _timeout_ms: int):
        if self._nudged:
            self._index = min(self._index + 1, len(self._cards_counts) - 1)
            self._nudged = False


class _FakeContext:
    pages = []

    def new_page(self):
        return None


def test_restriction_detector_does_not_trigger_on_generic_verify_text() -> None:
    client = LinkedInClient(_FakeContext(), make_config())
    page = _FakePage(
        url="https://www.linkedin.com/jobs/search/?keywords=java",
        title="LinkedIn Jobs",
        content="Please verify details on your profile",
        selector_counts={client.JOB_CARD_SELECTOR: 3},
    )

    client._check_access_restriction(page)


def test_restriction_detector_does_not_trigger_on_weak_selector_when_jobs_exist() -> None:
    client = LinkedInClient(_FakeContext(), make_config())
    page = _FakePage(
        url="https://www.linkedin.com/jobs/search/?keywords=java",
        title="LinkedIn Jobs",
        content="",
        selector_counts={client.JOB_CARD_SELECTOR: 2, "iframe[src*='captcha']": 1},
    )

    client._check_access_restriction(page)


def test_restriction_detector_triggers_on_checkpoint_url() -> None:
    client = LinkedInClient(_FakeContext(), make_config())
    page = _FakePage(
        url="https://www.linkedin.com/checkpoint/challenge/123",
        title="LinkedIn",
        content="",
        selector_counts={client.JOB_CARD_SELECTOR: 0},
    )

    with pytest.raises(AccessRestrictionError):
        client._check_access_restriction(page)


def test_relative_posted_text_parsing() -> None:
    client = LinkedInClient(_FakeContext(), make_config())
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    parsed = client._parse_relative_posted_text("2 days ago", now)
    assert parsed.startswith("2026-10-09T12:00:00")


def test_relative_posted_text_fallback_raw() -> None:
    client = LinkedInClient(_FakeContext(), make_config())
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    parsed = client._parse_relative_posted_text("Just now", now)
    assert parsed == "Just now"


def test_apply_method_text_detection() -> None:
    client = LinkedInClient(_FakeContext(), make_config())

    assert client._apply_method_from_texts(["Easy Apply", "Cairo"]) == ApplyMethod.EASY_APPLY.value
    assert client._apply_method_from_texts(["Apply on company website"]) == ApplyMethod.EXTERNAL.value


def test_job_cards_with_stable_count_waits_for_late_first_page_cards() -> None:
    client = LinkedInClient(_FakeContext(), make_config())
    page = _FakeDynamicCountPage(
        url="https://www.linkedin.com/jobs/search/?keywords=java",
        title="LinkedIn Jobs",
        content="",
        selector_counts={},
        cards_counts=[7, 7, 25, 25, 25],
    )

    _cards, total_cards = client._job_cards_with_stable_count(page)

    assert total_cards == 25
    assert page.wait_calls > 0


def test_job_cards_with_stable_count_nudges_late_second_page_cards() -> None:
    client = LinkedInClient(_FakeContext(), make_config())
    page = _FakeScrollNudgedCountPage(
        url="https://www.linkedin.com/jobs/search/?keywords=java&start=25",
        title="LinkedIn Jobs",
        content="",
        selector_counts={},
        cards_counts=[16, 16, 20, 25, 25],
    )

    _cards, total_cards = client._job_cards_with_stable_count(page)

    assert total_cards == 25


def test_extract_current_page_jobs_supplements_with_fallback_links_when_primary_undercounts() -> None:
    client = LinkedInClient(_FakeContext(), make_config())

    class _Link:
        def __init__(self, href: str):
            self.href = href

        @property
        def first(self):
            return self

    class _Card:
        def __init__(self, href: str, ext_id: str | None):
            self.href = href
            self.ext_id = ext_id

        def locator(self, _selector: str):
            return _Link(self.href)

    class _CardsLocator:
        def __init__(self, cards):
            self._cards = cards

        def nth(self, idx: int):
            return self._cards[idx]

    class _FallbackLinksLocator:
        def __init__(self, links):
            self._links = links

        def count(self):
            return len(self._links)

        def nth(self, idx: int):
            return self._links[idx]

    class _Page:
        def __init__(self, fallback_links):
            self._fallback = _FallbackLinksLocator(fallback_links)

        def locator(self, selector: str):
            if selector == LinkedInClient.RESULTS_LINK_FALLBACK_SELECTOR:
                return self._fallback
            return _FakeLocator(0)

    cards = []
    fallback_links = []
    for idx in range(25):
        job_id = str(1000 + idx)
        href = f"https://www.linkedin.com/jobs/view/{job_id}/"
        fallback_links.append(_Link(href))
        if idx < 16:
            cards.append(_Card(href, job_id))
        else:
            cards.append(_Card("", None))

    cards_locator = _CardsLocator(cards)
    page = _Page(fallback_links)
    query = SearchQuery("java", "egypt")

    client._check_access_restriction = lambda _page: None
    client._scroll_results = lambda _page: None
    client._job_cards_with_stable_count = lambda _page: (cards_locator, 25)
    client._job_id_from_card = lambda card, _url: card.ext_id
    client._safe_attr = lambda loc, name: getattr(loc, name, "") if name == "href" else ""
    client._safe_text = lambda _loc: ""
    client._title_from_card_text = lambda _card: ""
    client._company_from_card_text = lambda _card, _title, _location: ""
    client._posted_at_from_card = lambda _card, _at: ""
    client._apply_method_from_card = lambda _card: ApplyMethod.UNKNOWN.value

    jobs = client._extract_current_page_jobs(page, query)

    assert len(jobs) == 25
