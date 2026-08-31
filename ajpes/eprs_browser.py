from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from ajpes.eprs_parser import SearchResult, parse_search_results


CDP_URL = "http://127.0.0.1:9222"
EPRS_URL = "https://www.ajpes.si/prs/"
SEARCH_URL = "https://www.ajpes.si/prs/default.asp"
DEDICATED_PAGE_MARKER = "dastabase-ajpes-eprs-worker"


class AjpesSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AjpesLookupResult:
    search: SearchResult
    detail_url: str | None = None
    detail_html: str | None = None
    diagnostics: dict[str, object] | None = None


def count_ajpes_pages(browser) -> int:
    return sum(
        1
        for context in browser.contexts
        for page in context.pages
        if "ajpes.si" in page.url.lower()
    )


def is_dedicated_page(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                marker => (
                    window.name === marker ||
                    window.sessionStorage.getItem("dastabase_page_marker") === marker
                )
                """,
                DEDICATED_PAGE_MARKER,
            )
        )
    except Exception:
        return False


def mark_dedicated_page(page) -> None:
    page.evaluate(
        """
        marker => {
            window.name = marker;
            window.sessionStorage.setItem("dastabase_page_marker", marker);
        }
        """,
        DEDICATED_PAGE_MARKER,
    )


class AjpesEprsBrowserSession:
    def __init__(self, *, cdp_url: str = CDP_URL) -> None:
        self.cdp_url = cdp_url

    def lookup_company(
        self,
        *,
        registration_number: str | None = None,
        tax_number: str | None = None,
    ) -> AjpesLookupResult:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(self.cdp_url)
            before_acquire = count_ajpes_pages(browser)
            page = self._get_or_create_dedicated_page(browser)
            after_acquire = count_ajpes_pages(browser)

            self._goto(page, SEARCH_URL)
            page.locator("#naziv").fill("")
            page.locator("#maticna").fill(registration_number or "")
            page.locator("#davcna").fill(tax_number or "")
            page.locator("button[type='submit']").filter(
                has_text=re.compile(r"^I")
            ).last.click()
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1_000)

            search = parse_search_results(page.content(), base_url=page.url)
            detail_url = None
            detail_html = None
            if search.status == "OK" and len(search.candidates) == 1:
                detail_url = search.candidates[0].detail_url
                if detail_url:
                    self._goto(page, detail_url)
                    page.wait_for_timeout(1_000)
                    detail_url = page.url
                    detail_html = page.content()

            after_lookup = count_ajpes_pages(browser)
            return AjpesLookupResult(
                search=search,
                detail_url=detail_url,
                detail_html=detail_html,
                diagnostics={
                    "ajpes_pages_before_acquire": before_acquire,
                    "ajpes_pages_after_acquire": after_acquire,
                    "ajpes_pages_after_lookup": after_lookup,
                    "lookup_opened_new_ajpes_pages": after_lookup > after_acquire,
                    "single_dedicated_page_reused": after_lookup <= after_acquire,
                },
            )

    def _get_or_create_dedicated_page(self, browser):
        for context in browser.contexts:
            for page in context.pages:
                if is_dedicated_page(page):
                    return page

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        self._goto(page, EPRS_URL)
        return page

    def _goto(self, page, url: str) -> None:
        page.goto(urljoin(page.url, url), wait_until="domcontentloaded", timeout=30_000)
        mark_dedicated_page(page)


__all__ = [
    "AjpesEprsBrowserSession",
    "AjpesLookupResult",
    "AjpesSessionError",
    "PlaywrightTimeoutError",
]
