from __future__ import annotations

import json
import logging
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
    CARD_FIELD_TIMEOUT_MS = 500

    RESULTS_CONTAINER_SELECTORS = [
        ".jobs-search-results-list",
        ".jobs-search__results-list",
        "div.scaffold-layout__list-container",
    ]

    JOB_CARD_SELECTOR = ", ".join(
        [
            "li.jobs-search-results__list-item",
            "li.scaffold-layout__list-item",
            "li[data-occludable-job-id]",
            "div.job-card-container",
            "div[data-job-id]",
            "li:has(a[href*='/jobs/view/'])",
        ]
    )

    JOB_LINK_SELECTOR = ", ".join(
        [
            "a.job-card-list__title--link",
            "a.job-card-container__link",
            "a.base-card__full-link",
            "a[href*='/jobs/view/']",
            "a[href*='currentJobId=']",
            "a[href*='jobPosting:']",
        ]
    )

    RESULTS_LINK_SELECTOR = ", ".join(
        [
            "a[href*='/jobs/view/']",
            "a[href*='currentJobId=']",
            "a[href*='jobPosting:']",
            "a[href*='/jobs/collections/']",
            "a[href*='/jobs/search/']",
        ]
    )

    TITLE_SELECTOR = ", ".join(
        [
            "a.job-card-list__title--link",
            "a.job-card-container__link .visually-hidden",
            "h3.base-search-card__title",
            "h3",
        ]
    )

    COMPANY_SELECTOR = ", ".join(
        [
            "a.job-card-container__company-name",
            "h4.base-search-card__subtitle a",
            "span.artdeco-entity-lockup__subtitle",
            "span.job-card-container__primary-description",
        ]
    )

    LOCATION_SELECTOR = ", ".join(
        [
            ".job-search-card__location",
            "li.job-card-container__metadata-item",
            ".job-card-container__metadata-wrapper li",
            "span.artdeco-entity-lockup__caption",
        ]
    )

    DETAIL_COMPANY_SELECTORS = ", ".join(
        [
            "a.topcard__org-name-link",
            ".job-details-jobs-unified-top-card__company-name a",
            ".job-details-jobs-unified-top-card__company-name",
            ".topcard__flavor-row a",
        ]
    )
    DETAIL_DESCRIPTION_SELECTORS = ", ".join(
        [
            "div[id^='JobDetails_AboutTheJob_']",
            ".show-more-less-html__markup",
            "div.jobs-description__content",
            ".jobs-description-content__text",
            ".jobs-box__html-content",
            "#job-details",
        ]
    )
    DETAIL_DESCRIPTION_HEADING = "About the job"
    DETAIL_JSON_LD_SELECTOR = "script[type='application/ld+json']"

    NEXT_PAGE_SELECTORS = [
        "button.artdeco-pagination__button--next",
        "button[aria-label*='next' i]",
        "button[aria-label*='suivante' i]",
        "li.artdeco-pagination__indicator--number.active + li button",
        "li.artdeco-pagination__indicator--number--active + li button",
    ]

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
        if any(part in url for part in ["/checkpoint/", "/challenge/", "/captcha/", "authwall"]):
            raise AccessRestrictionError(f"LinkedIn restriction page URL detected: {page.url}")
        if any(part in title for part in ["security verification", "verify your identity", "captcha challenge"]):
            raise AccessRestrictionError(f"LinkedIn restriction page title detected: {page.title()}")

    def _normalize_job_url(self, raw_url: str) -> str:
        value = (raw_url or "").strip()
        if not value:
            return ""
        if value.startswith("/"):
            value = f"https://www.linkedin.com{value}"
        return value

    def _extract_job_id(self, url: str) -> Optional[str]:
        if not url:
            return None
        match = re.search(r"/jobs/view/(\d+)", url)
        if match:
            return match.group(1)
        match = re.search(r"jobPosting:(\d+)", url)
        if match:
            return match.group(1)
        try:
            current_job = parse_qs(urlparse(url).query).get("currentJobId", [""])[0]
            return current_job if current_job.isdigit() else None
        except Exception:
            return None

    def _job_id_from_card(self, card, url: str) -> Optional[str]:
        from_url = self._extract_job_id(url)
        if from_url:
            return from_url
        for attr in ["data-occludable-job-id", "data-job-id", "data-entity-urn"]:
            value = self._safe_attr(card, attr)
            if value.isdigit():
                return value
            match = re.search(r"jobPosting:(\d+)", value)
            if match:
                return match.group(1)
        return None

    def _parse_relative_posted_text(self, raw_text: str, now_utc: datetime) -> str:
        text = raw_text.strip().lower().replace("reposted", "").strip()
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

    def _scroll_results(self, page) -> None:
        for selector in self.RESULTS_CONTAINER_SELECTORS:
            if page.locator(selector).count() == 0:
                continue
            previous_count = -1
            stable = 0
            for _ in range(16):
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
                    pass
                page.wait_for_timeout(300)
                count = page.locator(self.JOB_CARD_SELECTOR).count()
                if count == previous_count:
                    stable += 1
                else:
                    stable = 0
                previous_count = count
                if stable >= 3 and count >= self.PAGE_SIZE:
                    break
            return

    def _candidate_links_from_html(self, html: str) -> list[str]:
        links: list[str] = []
        for match in re.finditer(r"https://www\.linkedin\.com/jobs/view/(\d+)", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"https:\\/\\/www\.linkedin\.com\\/jobs\\/view\\/(\d+)", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"/jobs/view/(\d+)", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"urn:li:jobPosting:(\d+)", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"urn:li:fsd_jobPosting:(\d+)", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"jobPosting:(\d+)", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"data-occludable-job-id=[\"'](\d+)[\"']", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"data-job-id=[\"'](\d+)[\"']", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"currentJobId=(\d+)", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"jobPostingId%7E%3A(\d+)", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"jobPostingId=(\d+)", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        for match in re.finditer(r"fsd_jobPosting%3A(\d+)", html):
            links.append(f"https://www.linkedin.com/jobs/view/{match.group(1)}/")
        # preserve order while deduplicating
        return list(dict.fromkeys(links))

    def _posted_at_from_card(self, card, discovered_at: datetime) -> str:
        time_locator = card.locator("time").first
        date_attr = self._safe_attr(time_locator, "datetime")
        if date_attr:
            return date_attr
        date_text = self._safe_text(time_locator)
        if date_text:
            return self._parse_relative_posted_text(date_text, discovered_at)
        for text in self._safe_all_texts(card.locator("span")):
            lower = text.lower()
            if any(x in lower for x in ["ago", "today", "yesterday"]):
                return self._parse_relative_posted_text(text, discovered_at)
        return ""

    def _apply_method_from_texts(self, texts: list[str]) -> str:
        for text in texts:
            lower = text.lower()
            if "apply on company website" in lower:
                return ApplyMethod.EXTERNAL.value
            if "easy apply" in lower:
                return ApplyMethod.EASY_APPLY.value
        for text in texts:
            if re.search(r"\bapply\b", text.lower()):
                return ApplyMethod.EXTERNAL.value
        return ApplyMethod.UNKNOWN.value

    def _clean_line(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    def _is_noise_line(self, text: str) -> bool:
        lower = self._clean_line(text).lower()
        if not lower:
            return True
        noise_parts = [
            "connection works here",
            "connections work here",
            "company alumni work here",
            "school alumni work here",
            "be an early applicant",
            "actively reviewing applicants",
            "responses managed off linkedin",
            "people clicked apply",
            "easy apply",
            "viewed",
            "promoted",
            "reposted",
        ]
        if any(part in lower for part in noise_parts):
            return True
        if re.search(r"\b\d+\s+(minute|hour|day|week|month)s?\s+ago\b", lower):
            return True
        return False

    def _looks_like_location(self, text: str) -> bool:
        value = self._clean_line(text)
        lower = value.lower()
        if self._looks_like_job_title(value):
            return False
        work_mode_only = lower in {"remote", "hybrid", "on-site", "on site"}
        work_mode_suffix = bool(re.search(r"\((?:remote|hybrid|on-site|on site)\)\s*$", lower))
        return work_mode_only or work_mode_suffix or "," in value

    def _looks_like_job_title(self, text: str) -> bool:
        return bool(
            re.search(
                r"\b(?:developer|engineer|architect|manager|consultant|analyst|specialist|"
                r"designer|administrator|scientist|programmer|devops|frontend|backend|"
                r"full[\s-]*stack|mobile)\b",
                self._clean_line(text),
                re.IGNORECASE,
            )
        )

    def _is_punctuation_only(self, text: str) -> bool:
        return not bool(re.search(r"[A-Za-z0-9]", text or ""))

    def _clean_title_text(self, text: str) -> str:
        value = self._clean_line(text)
        value = re.sub(r"\s*\(verified job\)\s*", " ", value, flags=re.IGNORECASE)
        return self._clean_line(value)

    def _extract_card_fields_from_text(self, card, fallback_location: str) -> tuple[str, str, str]:
        raw = ""
        try:
            raw = card.inner_text(timeout=self.CARD_FIELD_TIMEOUT_MS) or ""
        except Exception:
            raw = ""

        lines: list[str] = []
        for line in raw.splitlines():
            cleaned = self._clean_line(line)
            if cleaned:
                lines.append(cleaned)
        if not lines:
            for value in self._safe_all_texts(card.locator("a, div, span")):
                cleaned = self._clean_line(value)
                if cleaned:
                    lines.append(cleaned)

        unique_lines = list(dict.fromkeys(lines))
        useful = [
            line
            for line in unique_lines
            if not self._is_noise_line(line) and not self._is_punctuation_only(line)
        ]

        title = ""
        company = ""
        location = ""

        title_index = next(
            (index for index, line in enumerate(useful) if self._looks_like_job_title(line)),
            None,
        )
        if title_index is not None:
            title = self._clean_title_text(useful[title_index])

            company_candidates = [
                (index, line)
                for index, line in enumerate(useful)
                if index != title_index
                and not self._looks_like_job_title(line)
                and not self._looks_like_location(line)
            ]
            before_title = [candidate for candidate in company_candidates if candidate[0] < title_index]
            company_index, company = (before_title or company_candidates or [(-1, "")])[0]
            company = self._clean_line(company)

            explicit_locations = [line for line in useful if self._looks_like_location(line)]
            if explicit_locations:
                location = self._clean_line(explicit_locations[0])
            elif company_index >= 0 and company_index < title_index:
                between = [
                    line
                    for index, line in enumerate(useful)
                    if company_index < index < title_index and not self._looks_like_job_title(line)
                ]
                if between:
                    location = self._clean_line(between[-1])
        else:
            for line in useful:
                if not title and not self._looks_like_location(line):
                    title = self._clean_title_text(line)
                    continue
                if title and not company and not self._looks_like_location(line):
                    company = self._clean_line(line)
                    continue
                if not location and self._looks_like_location(line):
                    location = self._clean_line(line)

        return title, company, (location or fallback_location)

    def _extract_current_page_jobs(self, page, query: SearchQuery) -> list[DiscoveredJob]:
        self._check_access_restriction(page)
        self._scroll_results(page)

        jobs: list[DiscoveredJob] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        cards = page.locator(self.JOB_CARD_SELECTOR)
        total_cards = cards.count()

        ai_cards = page.locator("div[role='button'][componentkey^='job-card-component-ref-']")
        ai_count = ai_cards.count()
        if ai_count > total_cards:
            total_cards = ai_count

        if ai_count > 0:
            for idx in range(ai_count):
                card = ai_cards.nth(idx)
                comp_key = self._safe_attr(card, "componentkey")
                match = re.search(r"job-card-component-ref-(\d+)", comp_key)
                ext_id = match.group(1) if match else None
                if not ext_id or ext_id in seen_ids:
                    continue

                parsed_title, parsed_company, parsed_location = self._extract_card_fields_from_text(card, query.location)
                title = parsed_title or "Unknown title"
                company = parsed_company or "Unknown company"
                location = parsed_location or query.location

                discovered_at = datetime.now(UTC)
                jobs.append(
                    DiscoveredJob(
                        source="linkedin",
                        external_id=ext_id,
                        url=f"https://www.linkedin.com/jobs/view/{ext_id}/",
                        title=title,
                        company=company,
                        location=location,
                        description=None,
                        posted_at="",
                        discovered_at=discovered_at,
                        apply_method=ApplyMethod.UNKNOWN.value,
                    )
                )
                seen_ids.add(ext_id)
                seen_urls.add(f"https://www.linkedin.com/jobs/view/{ext_id}/")

            if ai_count < self.PAGE_SIZE:
                try:
                    page.evaluate(
                        """
                        () => {
                            const leftPane = document.querySelector('[data-testid="lazy-column"]') || document.querySelector('main');
                            if (!leftPane) return;
                            leftPane.scrollTop = leftPane.scrollHeight;
                        }
                        """
                    )
                    page.wait_for_timeout(500)
                except Exception:
                    pass

        for idx in range(total_cards):
            card = cards.nth(idx)
            link = card.locator(self.JOB_LINK_SELECTOR).first
            url = self._normalize_job_url(self._safe_attr(link, "href"))
            ext_id = self._job_id_from_card(card, url)
            if ext_id and not url:
                url = f"https://www.linkedin.com/jobs/view/{ext_id}/"
            if not ext_id and not url:
                continue
            if ext_id and ext_id in seen_ids:
                continue
            if url and url in seen_urls:
                continue

            title = self._safe_text(card.locator(self.TITLE_SELECTOR).first) or self._safe_text(link) or "Unknown title"
            company = self._safe_text(card.locator(self.COMPANY_SELECTOR).first) or "Unknown company"
            location = self._safe_text(card.locator(self.LOCATION_SELECTOR).first) or query.location

            parsed_title, parsed_company, parsed_location = self._extract_card_fields_from_text(card, query.location)
            title = self._clean_title_text(title)
            if (not title or title == "Unknown title" or self._is_noise_line(title)) and parsed_title:
                title = parsed_title
            if title.lower().startswith("selected") and parsed_title:
                title = parsed_title

            if (not company or company == "Unknown company" or self._is_noise_line(company)) and parsed_company:
                company = parsed_company
            company = self._clean_line(company) or "Unknown company"

            if (not location or location == query.location or self._is_noise_line(location)) and parsed_location:
                location = parsed_location
            location = self._clean_line(location) or query.location

            discovered_at = datetime.now(UTC)
            posted_at = self._posted_at_from_card(card, discovered_at)
            apply_method = self._apply_method_from_texts(self._safe_all_texts(card.locator("span, a, button")))

            jobs.append(
                DiscoveredJob(
                    source="linkedin",
                    external_id=ext_id,
                    url=url,
                    title=title,
                    company=company,
                    location=location,
                    description=None,
                    posted_at=posted_at,
                    discovered_at=discovered_at,
                    apply_method=apply_method,
                )
            )
            if ext_id:
                seen_ids.add(ext_id)
            if url:
                seen_urls.add(url)

        try:
            html = page.content()
        except Exception:
            html = ""
        for href in self._candidate_links_from_html(html):
            url = self._normalize_job_url(href)
            ext_id = self._extract_job_id(url)
            if ext_id and ext_id in seen_ids:
                continue
            if url in seen_urls:
                continue
            jobs.append(
                DiscoveredJob(
                    source="linkedin",
                    external_id=ext_id,
                    url=url,
                    title="Unknown title",
                    company="Unknown company",
                    location=query.location,
                    description=None,
                    posted_at="",
                    discovered_at=datetime.now(UTC),
                    apply_method=ApplyMethod.UNKNOWN.value,
                )
            )
            if ext_id:
                seen_ids.add(ext_id)
            seen_urls.add(url)

        # AI jobs surface often exposes additional jobs as links with currentJobId.
        links = page.locator("a[href*='currentJobId='], a[href*='/jobs/view/']")
        for idx in range(links.count()):
            href = self._normalize_job_url(self._safe_attr(links.nth(idx), "href"))
            ext_id = self._extract_job_id(href)
            if not ext_id:
                continue
            canonical_url = f"https://www.linkedin.com/jobs/view/{ext_id}/"
            if ext_id in seen_ids or canonical_url in seen_urls:
                continue
            title = self._safe_text(links.nth(idx)) or "Unknown title"
            jobs.append(
                DiscoveredJob(
                    source="linkedin",
                    external_id=ext_id,
                    url=canonical_url,
                    title=title,
                    company="Unknown company",
                    location=query.location,
                    description=None,
                    posted_at="",
                    discovered_at=datetime.now(UTC),
                    apply_method=ApplyMethod.UNKNOWN.value,
                )
            )
            seen_ids.add(ext_id)
            seen_urls.add(canonical_url)

        logger.info("Extracted jobs from page: cards=%d unique=%d", total_cards, len(jobs))
        return jobs

    def _build_search_url(self, query: SearchQuery, start: int = 0) -> str:
        keywords = query.keyword.strip()
        lowered = keywords.lower()
        suffix = " posted in the past 24 hours"
        if lowered.endswith(suffix):
            keywords = keywords[: -len(suffix)].strip()

        url = (
            "https://www.linkedin.com/jobs/search/"
            f"?keywords={quote_plus(keywords)}"
            f"&location={quote_plus(query.location)}"
        )
        if self.config.search_last_24h_only:
            url += "&f_TPR=r86400"
        if start > 0:
            url += f"&start={start}"
        return url

    def _start_from_url(self, url: str) -> int:
        try:
            return int(parse_qs(urlparse(url).query).get("start", ["0"])[0])
        except Exception:
            return 0

    def _is_disabled(self, locator) -> bool:
        return (
            bool(self._safe_attr(locator, "disabled"))
            or self._safe_attr(locator, "aria-disabled").lower() == "true"
            or "disabled" in self._safe_attr(locator, "class").lower()
        )

    def _click_next_page(self, page) -> bool:
        for selector in self.NEXT_PAGE_SELECTORS:
            try:
                button = page.locator(selector).first
                if button.count() == 0 or self._is_disabled(button):
                    continue
                button.click(timeout=4000)
                page.wait_for_timeout(1400)
                return True
            except Exception:
                continue
        return False

    def _next_start_from_pagination(self, page, current_start: int) -> int | None:
        starts: set[int] = set()
        links = page.locator("a[href*='start=']")
        for idx in range(links.count()):
            href = self._safe_attr(links.nth(idx), "href")
            match = re.search(r"[?&]start=(\d+)", href)
            if match:
                starts.add(int(match.group(1)))
        higher = sorted(x for x in starts if x > current_start)
        return higher[0] if higher else None

    def _max_page_number(self, page) -> int:
        max_page = 0
        buttons = page.locator("button[aria-label*='Page']")
        for idx in range(buttons.count()):
            label = self._safe_attr(buttons.nth(idx), "aria-label")
            match = re.search(r"(\d+)", label)
            if match:
                max_page = max(max_page, int(match.group(1)))
        return max_page

    def search_jobs(self, query: SearchQuery) -> list[DiscoveredJob]:
        page = self._ensure_page()
        all_jobs: list[DiscoveredJob] = []
        seen_keys: set[str] = set()
        seen_page_signatures: set[tuple[str, ...]] = set()
        visited_starts: set[int] = set()

        start = 0
        page_count = 0
        while page_count < self.MAX_PAGES_PER_QUERY:
            if start in visited_starts:
                logger.info("Pagination loop detected at start=%d", start)
                break
            visited_starts.add(start)
            page_count += 1

            search_url = self._build_search_url(query, start)
            logger.info("Navigating to %s", search_url)
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1200)
            self._check_access_restriction(page)

            try:
                page.wait_for_selector(self.RESULTS_LINK_SELECTOR, timeout=8000)
            except TimeoutError:
                logger.info("No job link selector appeared for query '%s'", query.keyword)

            page_jobs = self._extract_current_page_jobs(page, query)
            if not page_jobs:
                logger.info("No jobs found at start=%d", start)
                break

            signature = tuple((j.external_id or j.url) for j in page_jobs[:15])
            if signature in seen_page_signatures:
                logger.info("Repeated page signature at start=%d", start)
                break
            seen_page_signatures.add(signature)

            new_count = 0
            for job in page_jobs:
                key = job.external_id or job.url
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                all_jobs.append(job)
                new_count += 1

            logger.info(
                "Page processed query='%s' start=%d page_jobs=%d new_jobs=%d total=%d",
                query.keyword,
                start,
                len(page_jobs),
                new_count,
                len(all_jobs),
            )

            current_page_num = (start // self.PAGE_SIZE) + 1
            max_page_num = self._max_page_number(page)
            if max_page_num > 0 and current_page_num >= max_page_num:
                logger.info("Reached last visible page (%d/%d)", current_page_num, max_page_num)
                break

            if self._click_next_page(page):
                next_start = self._start_from_url(page.url)
                if next_start <= start:
                    next_start = start + self.PAGE_SIZE
                start = next_start
                continue

            next_start = self._next_start_from_pagination(page, start)
            if next_start is None:
                next_start = start + self.PAGE_SIZE
            if new_count == 0 and next_start > start:
                logger.info("No new jobs at start=%d and no effective UI pagination; stopping", start)
                break
            start = next_start

        return all_jobs

    def _company_from_detail_page(self, page) -> str:
        company = self._safe_text(page.locator(self.DETAIL_COMPANY_SELECTORS).first)
        if company:
            return company
        scripts = page.locator(self.DETAIL_JSON_LD_SELECTOR)
        for i in range(scripts.count()):
            try:
                content = scripts.nth(i).text_content(timeout=700)
                if not content:
                    continue
                payload = json.loads(content)
                items = payload if isinstance(payload, list) else [payload]
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
        return ""

    def _identity_from_detail_page(self, page) -> tuple[str, str, str]:
        title = ""
        company = ""
        location = ""

        browser_title = (page.title() or "").strip()
        if browser_title.endswith(" | LinkedIn"):
            identity = browser_title[: -len(" | LinkedIn")].strip()
            if " | " in identity:
                title, company = [part.strip() for part in identity.rsplit(" | ", 1)]

        try:
            body_text = page.locator("body").inner_text(timeout=3000) or ""
            for raw_line in body_text.splitlines():
                line = self._clean_line(raw_line)
                if "·" not in line:
                    continue
                if not re.search(r"\b\d+\s+(?:minute|hour|day|week|month)s?\s+ago\b", line, re.IGNORECASE):
                    continue
                candidate = self._clean_line(line.split("·", 1)[0])
                if candidate:
                    location = candidate
                    break
        except Exception:
            logger.debug("Could not extract location from job detail page", exc_info=True)

        return title, company, location

    def _title_from_detail_page(self, page) -> str:
        title = (page.title() or "").strip()
        parts = [p.strip() for p in title.split("|") if p.strip()]
        return parts[0] if parts else ""

    def _posted_at_from_detail_page(self, page, discovered_at: datetime) -> str:
        for text in self._safe_all_texts(page.locator("time, span")):
            lower = text.lower()
            posted_match = re.search(r"posted\s+(.+)$", lower)
            if posted_match:
                return self._parse_relative_posted_text(posted_match.group(1), discovered_at)
            if any(x in lower for x in ["ago", "today", "yesterday"]):
                return self._parse_relative_posted_text(text, discovered_at)
        return ""

    def _description_from_detail_page(self, page) -> str:
        locators = [
            page.locator("div[id^='JobDetails_AboutTheJob_']").first,
            page.locator(self.DETAIL_DESCRIPTION_SELECTORS).first,
        ]
        for locator in locators:
            try:
                text = (locator.inner_text(timeout=4000) or "").strip()
            except Exception:
                continue
            if not text:
                continue
            normalized = re.sub(r"\s+", " ", text).strip()
            if normalized.lower().startswith("about the job "):
                normalized = normalized[len("about the job ") :].strip()
            if normalized and normalized.lower() != "about the job":
                return normalized

        # LinkedIn's newer job detail layout uses generated class names and no
        # stable AboutTheJob id. The section can still be identified by its
        # accessible heading; its grandparent contains the complete description.
        try:
            headings = page.get_by_text(self.DETAIL_DESCRIPTION_HEADING, exact=True)
            for index in range(headings.count()):
                section = headings.nth(index).locator("xpath=../..")
                text = (section.inner_text(timeout=2000) or "").strip()
                normalized = re.sub(r"\s+", " ", text).strip()
                normalized = re.sub(r"^About the job\s*", "", normalized, flags=re.IGNORECASE)
                normalized = re.sub(r"\s*(?:…|\.\.\.)\s*more\s*$", "", normalized, flags=re.IGNORECASE)
                if normalized:
                    return normalized
        except Exception:
            logger.debug("Could not extract description from About the job heading", exc_info=True)

        scripts = page.locator(self.DETAIL_JSON_LD_SELECTOR)
        for i in range(scripts.count()):
            try:
                content = scripts.nth(i).text_content(timeout=700)
                if not content:
                    continue
                payload = json.loads(content)
                items = payload if isinstance(payload, list) else [payload]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    raw = item.get("description")
                    if isinstance(raw, str) and raw.strip():
                        cleaned = re.sub(r"<[^>]+>", " ", raw)
                        return re.sub(r"\s+", " ", cleaned).strip()
            except Exception:
                continue
        return ""

    def _apply_method_from_detail_page(self, page) -> str:
        texts = self._safe_all_texts(page.locator("button, a, span"))
        return self._apply_method_from_texts(texts)

    def enrich_metadata_for_jobs(self, jobs: list[DiscoveredJob]) -> None:
        targets: list[DiscoveredJob] = []
        seen_targets: set[str] = set()
        for job in jobs:
            key = job.external_id or job.url
            if not job.url or not key or key in seen_targets:
                continue
            seen_targets.add(key)
            targets.append(job)
        if not targets:
            return
        detail_page = self.context.new_page()
        try:
            for job in targets:
                try:
                    detail_page.goto(job.url, wait_until="domcontentloaded", timeout=22000)
                    try:
                        detail_page.locator(
                            f"{self.DETAIL_DESCRIPTION_SELECTORS}, "
                            f"h2:has-text('{self.DETAIL_DESCRIPTION_HEADING}')"
                        ).first.wait_for(state="visible", timeout=7000)
                    except Exception:
                        pass
                    detail_page.wait_for_timeout(800)

                    detail_title, detail_company, detail_location = self._identity_from_detail_page(detail_page)
                    if detail_title and detail_company:
                        job.title = detail_title
                        job.company = detail_company
                        if detail_location:
                            job.location = detail_location
                        job.detail_enriched = True

                    if not job.company or job.company == "Unknown company":
                        company = self._company_from_detail_page(detail_page)
                        if company:
                            job.company = company
                    if not job.title or job.title == "Unknown title":
                        title = self._title_from_detail_page(detail_page)
                        if title:
                            job.title = title
                    if not job.posted_at:
                        posted_at = self._posted_at_from_detail_page(detail_page, job.discovered_at)
                        if posted_at:
                            job.posted_at = posted_at
                    if not job.description:
                        description = self._description_from_detail_page(detail_page)
                        if description:
                            job.description = description
                    apply_method = self._apply_method_from_detail_page(detail_page)
                    if apply_method != ApplyMethod.UNKNOWN.value:
                        job.apply_method = apply_method
                except Exception:
                    logger.debug("Failed detail enrichment for %s", job.url, exc_info=True)
        finally:
            detail_page.close()
