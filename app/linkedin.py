from __future__ import annotations

import logging
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Optional
from urllib.parse import parse_qs, quote_plus, urlparse

from playwright.sync_api import BrowserContext, TimeoutError

from .config import Config
from .models import ApplyMethod, DiscoveredJob, SearchQuery

logger = logging.getLogger(__name__)


class AccessRestrictionError(RuntimeError):
    pass


class LinkedInClient:
    PAGE_SIZE = 25
    MAX_PAGES_PER_QUERY = 20
    NEXT_PAGE_SELECTORS = [
        "button.artdeco-pagination__button--next",
        "button[aria-label*='next' i]",
        "li.artdeco-pagination__indicator--number.active + li button",
        "li.artdeco-pagination__indicator--number--active + li button",
    ]
    JOB_CARD_SELECTOR = ", ".join(
        [
            "li.jobs-search-results__list-item",
            "li:has(a.job-card-list__title--link)",
            "li[data-occludable-job-id]",
            "div.job-card-container",
            "li:has(a[href*='/jobs/view/'])",
        ]
    )
    JOB_LINK_SELECTOR = ", ".join(
        [
            "a.job-card-list__title--link",
            "a.job-card-container__link",
            "a.base-card__full-link",
            "a[href*='/jobs/view/']",
        ]
    )
    TITLE_SELECTOR = "h3.base-search-card__title, a.job-card-list__title--link, h1.top-card-layout__title"
    COMPANY_SELECTOR = ", ".join(
        [
            "h4.base-search-card__subtitle a",
            "a.job-card-container__company-name",
            "span.job-card-container__primary-description",
            ".topcard__org-name-link",
            ".topcard__flavor",
        ]
    )
    LOCATION_SELECTOR = ", ".join(
        [
            ".job-search-card__location",
            "li.job-card-container__metadata-item",
            ".job-card-container__metadata-wrapper li",
            ".topcard__flavor--bullet",
        ]
    )
    DATE_SELECTOR = "time"
    RESULTS_LINK_FALLBACK_SELECTOR = "a[href*='/jobs/view/']"
    DETAIL_COMPANY_SELECTORS = ", ".join(
        [
            "a.topcard__org-name-link",
            ".job-details-jobs-unified-top-card__company-name a",
            ".job-details-jobs-unified-top-card__company-name",
            ".topcard__flavor-row a",
        ]
    )
    DETAIL_JSON_LD_SELECTOR = "script[type='application/ld+json']"
    DETAIL_DESCRIPTION_SELECTORS = ", ".join(
        [
            ".show-more-less-html__markup",
            ".jobs-description-content__text",
            ".jobs-box__html-content",
            "#job-details",
        ]
    )
    DETAIL_EASY_APPLY_SELECTORS = [
        "button:has-text('Easy Apply')",
        "button[aria-label*='Easy Apply']",
        "button.jobs-apply-button:has-text('Easy Apply')",
    ]
    DETAIL_EXTERNAL_APPLY_SELECTORS = [
        "a:has-text('Apply on company website')",
        "button:has-text('Apply on company website')",
        "a:has-text('Apply now')",
        "button:has-text('Apply now')",
        "a[href*='offsite']",
        "a[href*='apply']",
    ]

    CARD_FIELD_TIMEOUT_MS = 500

    def __init__(self, context: BrowserContext, config: Config):
        self.context = context
        self.config = config

    def _ensure_page(self):
        return self.context.pages[0] if self.context.pages else self.context.new_page()

    def _safe_text(self, locator) -> str:
        try:
            value = locator.text_content(timeout=self.CARD_FIELD_TIMEOUT_MS)
            return value.strip() if value else ""
        except Exception:
            return ""

    def _safe_attr(self, locator, attr: str) -> str:
        try:
            value = locator.get_attribute(attr, timeout=self.CARD_FIELD_TIMEOUT_MS)
            return value.strip() if value else ""
        except Exception:
            return ""

    def _safe_all_texts(self, locator) -> list[str]:
        try:
            values = locator.all_text_contents()
            return [v.strip() for v in values if v and v.strip()]
        except Exception:
            return []

    def _check_access_restriction(self, page) -> None:
        url = page.url.lower()
        title = (page.title() or "").lower()

        restricted_url_parts = ["/checkpoint/", "/challenge/", "/captcha/", "authwall"]
        if any(part in url for part in restricted_url_parts):
            raise AccessRestrictionError(f"LinkedIn restriction page URL detected: {page.url}")

        restricted_title_parts = ["security verification", "verify your identity", "captcha challenge"]
        if any(part in title for part in restricted_title_parts):
            raise AccessRestrictionError(f"LinkedIn restriction page title detected: {page.title()}")

        strong_restriction_selectors = [
            "form[action*='checkpoint/challenge']",
            "#captcha-internal",
            "h1:has-text('Security Verification')",
        ]
        for selector in strong_restriction_selectors:
            if page.locator(selector).count() > 0:
                raise AccessRestrictionError(f"LinkedIn restriction element detected: selector={selector}")

    def _extract_job_id(self, url: str) -> Optional[str]:
        match = re.search(r"/jobs/view/(\d+)", url)
        return match.group(1) if match else None

    def _normalize_job_url(self, raw_url: str) -> str:
        url = raw_url.strip()
        if not url:
            return ""
        if url.startswith("/"):
            return f"https://www.linkedin.com{url}"
        return url

    def _job_id_from_card(self, card, url: str) -> Optional[str]:
        from_url = self._extract_job_id(url)
        if from_url:
            return from_url
        card_id = self._safe_attr(card, "data-occludable-job-id")
        if card_id and card_id.isdigit():
            return card_id
        return None

    def _company_from_card_text(self, card, title: str, location: str) -> str:
        texts = self._safe_all_texts(card.locator("span"))
        if not texts:
            return ""
        title_l = title.lower().strip()
        location_l = location.lower().strip()
        for t in texts:
            v = t.strip()
            l = v.lower()
            if not v:
                continue
            if title_l and l == title_l:
                continue
            if location_l and l == location_l:
                continue
            if any(
                marker in l
                for marker in ["easy apply", "promoted", "reposted", "ago", "applicants"]
            ):
                continue
            # Skip location-ish phrases.
            if any(marker in l for marker in ["remote", "hybrid", "on-site", "onsite", "egypt", "cairo"]):
                if "," in v or "(" in v:
                    continue
            return v
        return ""

    def _title_from_card_text(self, card) -> str:
        texts = self._safe_all_texts(card.locator("span"))
        for t in texts:
            l = t.lower().strip()
            if not l:
                continue
            if any(marker in l for marker in ["easy apply", "promoted", "reposted", "ago", "applicants"]):
                continue
            if any(marker in l for marker in ["remote", "hybrid", "on-site", "onsite", "egypt", "cairo"]):
                continue
            if len(t.strip()) < 4:
                continue
            return t.strip()
        return ""

    def _apply_method_from_texts(self, texts: list[str]) -> str:
        for text in texts:
            if "easy apply" in text.lower():
                return ApplyMethod.EASY_APPLY.value
        for text in texts:
            lower = text.lower()
            if "apply on company website" in lower:
                return ApplyMethod.EXTERNAL.value
            if re.search(r"\bapply\b", lower) and "easy apply" not in lower:
                return ApplyMethod.EXTERNAL.value
        return ApplyMethod.UNKNOWN.value

    def _apply_method_from_card(self, card) -> str:
        texts = self._safe_all_texts(card.locator("span, a, button"))
        if not texts:
            return ApplyMethod.UNKNOWN.value
        return self._apply_method_from_texts(texts)

    def _parse_relative_posted_text(self, raw_text: str, now_utc: datetime) -> str:
        text = raw_text.strip().lower()
        text = text.replace("reposted", "").strip()

        if "today" in text:
            return now_utc.isoformat()
        if "yesterday" in text:
            return (now_utc - timedelta(days=1)).isoformat()

        match = re.search(r"(\d+)\s*(minute|hour|day|week|month)s?\s*ago", text)
        if not match:
            return raw_text.strip()

        value = int(match.group(1))
        unit = match.group(2)
        if unit == "minute":
            dt = now_utc - timedelta(minutes=value)
        elif unit == "hour":
            dt = now_utc - timedelta(hours=value)
        elif unit == "day":
            dt = now_utc - timedelta(days=value)
        elif unit == "week":
            dt = now_utc - timedelta(weeks=value)
        else:
            dt = now_utc - timedelta(days=30 * value)
        return dt.isoformat()

    def _posted_at_from_card(self, card, discovered_at: datetime) -> str:
        time_locator = card.locator(self.DATE_SELECTOR).first
        date_attr = self._safe_attr(time_locator, "datetime")
        if date_attr:
            return date_attr

        date_text = self._safe_text(time_locator)
        if date_text:
            return self._parse_relative_posted_text(date_text, discovered_at)

        for text in self._safe_all_texts(card.locator("span")):
            lower = text.lower()
            if any(marker in lower for marker in ["ago", "today", "yesterday"]):
                return self._parse_relative_posted_text(text, discovered_at)

        return ""

    def _build_search_url(self, query: SearchQuery, start: int = 0) -> str:
        url = (
            "https://www.linkedin.com/jobs/search/"
            f"?keywords={quote_plus(query.keyword)}&location={quote_plus(query.location)}"
        )
        if self.config.search_last_24h_only:
            url += "&f_TPR=r86400"
        if start > 0:
            url += f"&start={start}"
        return url

    def _scroll_results(self, page) -> None:
        selectors = [
            ".jobs-search-results-list",
            ".jobs-search__results-list",
            "div.scaffold-layout__list-container",
        ]
        for selector in selectors:
            if page.locator(selector).count() == 0:
                continue
            stagnant = 0
            last_height = -1
            for _ in range(10):
                height = page.evaluate(
                    """
                    (sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return -1;
                        el.scrollTop = el.scrollHeight;
                        return el.scrollHeight;
                    }
                    """,
                    selector,
                )
                page.wait_for_timeout(250)
                if height == last_height:
                    stagnant += 1
                else:
                    stagnant = 0
                last_height = height
                if stagnant >= 2:
                    break
            break

    def _job_cards_with_stable_count(self, page):
        cards = page.locator(self.JOB_CARD_SELECTOR)
        best_count = cards.count()
        stable_reads = 0
        for _ in range(8):
            for selector in [
                ".jobs-search-results-list",
                ".jobs-search__results-list",
                "div.scaffold-layout__list-container",
            ]:
                try:
                    page.evaluate(
                        """
                        (sel) => {
                            const el = document.querySelector(sel);
                            if (!el) return false;
                            el.scrollTop = el.scrollHeight;
                            el.dispatchEvent(new Event('scroll', { bubbles: true }));
                            return true;
                        }
                        """,
                        selector,
                    )
                except Exception:
                    continue
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            page.wait_for_timeout(250)
            current_count = cards.count()
            if current_count > best_count:
                best_count = current_count
                stable_reads = 0
                continue
            if current_count == best_count:
                stable_reads += 1
                if stable_reads >= 2 and best_count >= self.PAGE_SIZE:
                    break
        return cards, best_count

    def _extract_current_page_jobs(self, page, query: SearchQuery) -> list[DiscoveredJob]:
        self._check_access_restriction(page)
        self._scroll_results(page)

        cards, total_cards = self._job_cards_with_stable_count(page)
        jobs: list[DiscoveredJob] = []
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()

        def _collect_fallback_jobs(limit: int | None = None) -> int:
            links = page.locator(self.RESULTS_LINK_FALLBACK_SELECTOR)
            link_total = links.count()
            added = 0
            for idx in range(link_total):
                if limit is not None and added >= limit:
                    break
                link = links.nth(idx)
                href = self._normalize_job_url(self._safe_attr(link, "href"))
                ext_id = self._extract_job_id(href)
                if "/jobs/view/" not in href or href in seen_urls:
                    continue
                if ext_id and ext_id in seen_ids:
                    continue
                seen_urls.add(href)
                if ext_id:
                    seen_ids.add(ext_id)
                title = self._safe_text(link) or "Unknown title"
                jobs.append(
                    DiscoveredJob(
                        source="linkedin",
                        external_id=ext_id,
                        url=href,
                        title=title,
                        company="Unknown company",
                        location=query.location,
                        description=None,
                        posted_at="",
                        discovered_at=datetime.now(UTC),
                        apply_method=ApplyMethod.UNKNOWN.value,
                    )
                )
                added += 1
            return added

        if total_cards == 0:
            link_total = page.locator(self.RESULTS_LINK_FALLBACK_SELECTOR).count()
            logger.info("Fallback link candidates=%d", link_total)
            _collect_fallback_jobs()
            logger.info("Fallback unique jobs=%d", len(jobs))
            return jobs

        skip_no_url = 0
        skip_non_job_url = 0
        skip_duplicate_url = 0
        skip_duplicate_id = 0
        for idx in range(total_cards):
            card = cards.nth(idx)
            link = card.locator(self.JOB_LINK_SELECTOR).first
            url = self._normalize_job_url(self._safe_attr(link, "href"))
            ext_id = self._job_id_from_card(card, url)

            if not url and not ext_id:
                skip_no_url += 1
                continue
            if url and "/jobs/view/" not in url and not ext_id:
                skip_non_job_url += 1
                continue
            if url and url in seen_urls:
                skip_duplicate_url += 1
                continue
            if ext_id and ext_id in seen_ids:
                skip_duplicate_id += 1
                continue

            if ext_id and not url:
                url = f"https://www.linkedin.com/jobs/view/{ext_id}/"
            if url:
                seen_urls.add(url)
            if ext_id:
                seen_ids.add(ext_id)

            title = self._safe_text(card.locator(self.TITLE_SELECTOR).first) or self._safe_text(link)
            if not title:
                title = self._title_from_card_text(card)
            company = self._safe_text(card.locator(self.COMPANY_SELECTOR).first)
            location = self._safe_text(card.locator(self.LOCATION_SELECTOR).first)
            if not company:
                company = self._company_from_card_text(card, title, location)
            discovered_at = datetime.now(UTC)
            posted_at = self._posted_at_from_card(card, discovered_at)
            apply_method = self._apply_method_from_card(card)

            jobs.append(
                DiscoveredJob(
                    source="linkedin",
                    external_id=ext_id,
                    url=url,
                    title=title or "Unknown title",
                    company=company or "Unknown company",
                    location=location or query.location,
                    description=None,
                    posted_at=posted_at,
                    discovered_at=discovered_at,
                    apply_method=apply_method,
                )
            )

        logger.info(
            "Primary unique jobs on page=%d (raw_cards=%d, skipped: no_url=%d non_job_url=%d dup_url=%d dup_id=%d)",
            len(jobs),
            total_cards,
            skip_no_url,
            skip_non_job_url,
            skip_duplicate_url,
            skip_duplicate_id,
        )

        if len(jobs) < min(total_cards, self.PAGE_SIZE):
            missing = min(total_cards, self.PAGE_SIZE) - len(jobs)
            added_from_fallback = _collect_fallback_jobs(limit=missing)
            if added_from_fallback:
                logger.info(
                    "Supplemented page jobs via fallback links: +%d (now=%d)",
                    added_from_fallback,
                    len(jobs),
                )
        return jobs

    def enrich_metadata_for_jobs(self, jobs: list[DiscoveredJob]) -> None:
        targets = [job for job in jobs if job.url]
        if not targets:
            return

        detail_page = self.context.new_page()
        try:
            logger.info("Starting detail enrichment for %d jobs", len(targets))
            enriched_company = 0
            enriched_posted = 0
            enriched_title = 0
            enriched_apply_method = 0
            enriched_description = 0
            for idx, job in enumerate(targets, start=1):
                if idx == 1 or idx % 5 == 0 or idx == len(targets):
                    logger.info("Detail enrichment progress: %d/%d", idx, len(targets))
                try:
                    detail_page.goto(job.url, wait_until="domcontentloaded", timeout=10000)
                    detail_page.wait_for_timeout(600)
                    company = self._company_from_detail_page(detail_page)
                    if company and job.company == "Unknown company":
                        job.company = company
                        enriched_company += 1
                    if not job.title or job.title == "Unknown title":
                        title = self._title_from_detail_page(detail_page)
                        if title:
                            job.title = title
                            enriched_title += 1
                    if not job.posted_at:
                        posted = self._posted_at_from_detail_page(detail_page, job.discovered_at)
                        if posted:
                            job.posted_at = posted
                            enriched_posted += 1
                    if not job.description or not job.description.strip():
                        description = self._description_from_detail_page(detail_page)
                        if description:
                            job.description = description
                            enriched_description += 1
                    apply_method = self._apply_method_from_detail_page(detail_page)
                    if (
                        apply_method != ApplyMethod.UNKNOWN.value
                        and (
                            job.apply_method == ApplyMethod.UNKNOWN.value
                            or (
                                apply_method == ApplyMethod.EASY_APPLY.value
                                and job.apply_method == ApplyMethod.EXTERNAL.value
                            )
                        )
                    ):
                        job.apply_method = apply_method
                        enriched_apply_method += 1
                except Exception:
                    logger.debug("Failed detail enrichment for job url=%s", job.url, exc_info=True)
            logger.info(
                "Detail enrichment complete: company_enriched=%d posted_at_enriched=%d description_enriched=%d apply_method_enriched=%d unknown_company_remaining=%d missing_posted_remaining=%d unknown_apply_remaining=%d",
                enriched_company,
                enriched_posted,
                enriched_description,
                enriched_apply_method,
                len([j for j in jobs if j.company == "Unknown company"]),
                len([j for j in jobs if not j.posted_at]),
                len([j for j in jobs if j.apply_method == ApplyMethod.UNKNOWN.value]),
            )
            logger.info(
                "Detail enrichment title stats: title_enriched=%d unknown_title_remaining=%d",
                enriched_title,
                len([j for j in jobs if not j.title or j.title == "Unknown title"]),
            )
        finally:
            detail_page.close()

    def _company_from_detail_page(self, page) -> str:
        company = self._safe_text(page.locator(self.DETAIL_COMPANY_SELECTORS).first)
        if company:
            return company

        scripts = page.locator(self.DETAIL_JSON_LD_SELECTOR)
        for i in range(scripts.count()):
            try:
                content = scripts.nth(i).text_content(timeout=600)
                if not content:
                    continue
                payload = json.loads(content)
                if isinstance(payload, list):
                    items = payload
                else:
                    items = [payload]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    hiring = item.get("hiringOrganization")
                    if isinstance(hiring, dict):
                        name = hiring.get("name")
                        if isinstance(name, str) and name.strip():
                            return name.strip()
            except Exception:
                continue

        title = (page.title() or "").strip()
        parts = [p.strip() for p in title.split("|") if p.strip()]
        if len(parts) >= 2:
            candidate = parts[1]
            if candidate.lower() != "linkedin":
                return candidate
        if " at " in title:
            # Example: "Role at Company | LinkedIn"
            rhs = title.split(" at ", 1)[1]
            company_guess = rhs.split("|", 1)[0].strip()
            if company_guess:
                return company_guess
        return ""

    def _posted_at_from_detail_page(self, page, discovered_at: datetime) -> str:
        # LinkedIn top card frequently contains text like "Posted 2 days ago" or "7 hours ago".
        span_texts = self._safe_all_texts(page.locator("span"))
        for text in span_texts:
            lower = text.lower()
            posted_match = re.search(r"posted\s+(.+)$", lower)
            if posted_match:
                return self._parse_relative_posted_text(posted_match.group(1), discovered_at)
            if any(marker in lower for marker in ["ago", "today", "yesterday"]):
                return self._parse_relative_posted_text(text, discovered_at)
        return ""

    def _description_from_detail_page(self, page) -> str:
        locator = page.locator(self.DETAIL_DESCRIPTION_SELECTORS).first
        description = ""
        try:
            description = (locator.inner_text(timeout=3000) or "").strip()
        except Exception:
            description = self._safe_text(locator)
        if description:
            return re.sub(r"\s+", " ", description).strip()

        scripts = page.locator(self.DETAIL_JSON_LD_SELECTOR)
        for i in range(scripts.count()):
            try:
                content = scripts.nth(i).text_content(timeout=600)
                if not content:
                    continue
                payload = json.loads(content)
                items = payload if isinstance(payload, list) else [payload]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    raw = item.get("description")
                    if isinstance(raw, str) and raw.strip():
                        text = re.sub(r"<[^>]+>", " ", raw)
                        text = re.sub(r"\s+", " ", text).strip()
                        if text:
                            return text
            except Exception:
                continue

        try:
            body_text = page.locator("body").inner_text(timeout=4000)
        except Exception:
            body_text = ""

        normalized = re.sub(r"\s+", " ", body_text or "").strip()
        if not normalized:
            return ""

        low = normalized.lower()
        about_idx = low.find("about the job")
        if about_idx != -1:
            segment = normalized[about_idx + len("about the job") : about_idx + len("about the job") + 7000]
            stop_markers = [
                "people also viewed",
                "jobs you may like",
                "set alert for similar jobs",
                "meet the hiring team",
                "about the company",
                "report this job",
            ]
            segment_low = segment.lower()
            cut = len(segment)
            for marker in stop_markers:
                idx = segment_low.find(marker)
                if idx != -1 and idx < cut:
                    cut = idx
            segment = segment[:cut].strip(" |-:")
            if len(segment) >= 80:
                return segment
        return ""

    def _apply_method_from_detail_page(self, page) -> str:
        for selector in self.DETAIL_EASY_APPLY_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    return ApplyMethod.EASY_APPLY.value
            except Exception:
                continue

        for selector in self.DETAIL_EXTERNAL_APPLY_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    return ApplyMethod.EXTERNAL.value
            except Exception:
                continue

        texts = self._safe_all_texts(page.locator("button, a, span"))
        return self._apply_method_from_texts(texts)

    def _title_from_detail_page(self, page) -> str:
        title = (page.title() or "").strip()
        parts = [p.strip() for p in title.split("|") if p.strip()]
        if parts:
            first = parts[0]
            if first.lower() != "linkedin":
                return first
        return ""

    def _next_start_from_pagination(self, page, current_start: int) -> int | None:
        starts: set[int] = set()
        links = page.locator("a[href*='start=']")
        count = links.count()
        for idx in range(count):
            href = self._safe_attr(links.nth(idx), "href")
            match = re.search(r"[?&]start=(\d+)", href)
            if match:
                starts.add(int(match.group(1)))

        higher = sorted(s for s in starts if s > current_start)
        return higher[0] if higher else None

    def _start_from_url(self, url: str) -> int:
        try:
            start_value = parse_qs(urlparse(url).query).get("start", ["0"])[0]
            return int(start_value)
        except Exception:
            return 0

    def _is_disabled(self, locator) -> bool:
        disabled_attr = self._safe_attr(locator, "disabled")
        aria_disabled = self._safe_attr(locator, "aria-disabled").lower()
        class_name = self._safe_attr(locator, "class").lower()
        return bool(disabled_attr) or aria_disabled == "true" or "disabled" in class_name

    def _click_next_page(self, page) -> bool:
        for selector in self.NEXT_PAGE_SELECTORS:
            try:
                button = page.locator(selector).first
                if button.count() == 0:
                    continue
                if self._is_disabled(button):
                    continue
                button.click(timeout=4000)
                page.wait_for_timeout(1200)
                return True
            except Exception:
                continue
        return False

    def search_jobs(self, query: SearchQuery) -> list[DiscoveredJob]:
        page = self._ensure_page()
        all_jobs: list[DiscoveredJob] = []
        seen_keys: set[str] = set()
        seen_page_fingerprints: set[tuple[str, ...]] = set()
        visited_starts: set[int] = set()

        start = 0
        navigation_mode = "url"
        page_count = 0
        while True:
            page_count += 1
            if page_count > self.MAX_PAGES_PER_QUERY:
                logger.info(
                    "Stopping pagination after max pages limit (%d) for query '%s'",
                    self.MAX_PAGES_PER_QUERY,
                    query.keyword,
                )
                break

            if start in visited_starts:
                logger.info("Pagination loop detected at start=%d; stopping", start)
                break
            visited_starts.add(start)

            if navigation_mode == "url":
                search_url = self._build_search_url(query, start=start)
                logger.info("Navigating to %s", search_url)
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1200)

            try:
                page.wait_for_selector(self.RESULTS_LINK_FALLBACK_SELECTOR, timeout=10000)
            except TimeoutError:
                logger.info("No job links appeared within wait timeout for query '%s'", query.keyword)

            page_jobs = self._extract_current_page_jobs(page, query)
            if not page_jobs:
                logger.info("No jobs found on page start=%d; finishing pagination", start)
                break

            page_keys = tuple((job.external_id or job.url) for job in page_jobs[:10])
            if page_keys in seen_page_fingerprints:
                logger.info("Repeated page fingerprint at start=%d; finishing pagination", start)
                break
            seen_page_fingerprints.add(page_keys)

            new_on_page = 0
            for job in page_jobs:
                key = job.external_id or job.url
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_jobs.append(job)
                new_on_page += 1

            logger.info(
                "Page processed for query '%s' start=%d page_jobs=%d new_jobs=%d total=%d",
                query.keyword,
                start,
                len(page_jobs),
                new_on_page,
                len(all_jobs),
            )

            if self._click_next_page(page):
                next_start_from_url = self._start_from_url(page.url)
                if next_start_from_url <= start:
                    next_start_from_url = start + self.PAGE_SIZE
                logger.info("Moved to next page via UI pagination: start=%d", next_start_from_url)
                start = next_start_from_url
                navigation_mode = "ui"
                continue

            next_start = self._next_start_from_pagination(page, start)
            if next_start is None:
                if navigation_mode == "ui":
                    logger.info("Reached final pagination page for query '%s'", query.keyword)
                    break
                next_start = start + self.PAGE_SIZE
                logger.info(
                    "No pagination links from start=%d; using offset fallback next_start=%d",
                    start,
                    next_start,
                )
            start = next_start
            navigation_mode = "url"

        return all_jobs
