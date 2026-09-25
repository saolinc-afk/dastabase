"""GVIN Lite: collect manually prepared search results only; never open details."""

import hashlib
import json
import logging
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from database_lite import IdentityConflict, get_connection, initialize_database, save_company

BASE_URL = "https://www.gvin.com"
CDP_URL = "http://127.0.0.1:9222"
ROWS = "li.newsearchIcon"
VALUES = "div.advanceResultDataDisplaySubjektFix"
NEXT = '[id$="bamSearch_lnkPagingNext"]'
LOG_PATH = Path(__file__).resolve().parent / "logs" / "collector_lite.log"
LOG = logging.getLogger("collector_lite")

# Prefer durable row identifiers over values, which can change independently.
IDENTITIES_JS = r"""() => Array.from(document.querySelectorAll('li.newsearchIcon')).map(row => {
    const href = row.querySelector('a[href*="CompanyId="]')?.getAttribute('href') || '';
    const id = href.match(/CompanyId=(\d+)/);
    const reg = row.querySelector('.registrationNumber')?.textContent.trim();
    return id ? 'gvin:' + id[1] : (reg || row.textContent.replace(/\s+/g, ' ').trim());
})"""


def human_pause(page):
    """Simple pacing between results pages; retained for existing callers."""
    page.wait_for_timeout(1000)


def parse_optional_number(value):
    if not value:
        return None
    try:
        number = float(re.sub(r"\s+", "", value).replace(".", "").replace(",", "."))
        return round(number, 2) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def parse_number(value):
    return parse_optional_number(value) or 0.0


def municipality_from_address(address):
    match = re.search(r"(\d{4}\s+[A-Za-zČŠŽčšžĆćĐđ\- ]+)$", address)
    return match.group(1).strip() if match else ""


def company_id_from_href(href):
    match = re.search(r"CompanyId=(\d+)", href or "")
    return match.group(1) if match else ""


def detail_url_from_href(href):
    # Stored provenance only. No caller navigates to this URL.
    return urljoin(BASE_URL, href.strip()) if href else ""


def find_gvin_page(context):
    for page in context.pages:
        host = (urlparse(page.url).hostname or "").lower()
        if host == "gvin.com" or host.endswith(".gvin.com"):
            return page
    return None


def financial_metric(label):
    text = label.casefold()
    found = []
    if re.search(r"prihod|celotni prih|revenue", text):
        found.append("revenue")
    if re.search(r"dobič|dobic|profit|poslovni izid|čisti poslov", text):
        found.append("profit")
    if re.search(r"zaposlen|employees|povprečno št", text):
        found.append("employees")
    if text.strip() in ("sredstva", "assets"):
        found.append("assets")
    if text.strip() in ("kapital", "capital"):
        found.append("capital")
    return found[0] if len(found) == 1 else None


def financial_fields(soup, headers=()):
    """Map labeled metrics; financial year 2025 is supplied by the operator."""
    cells = soup.select(VALUES)
    entries = []
    result = {f"{metric}_2025": None for metric in ("revenue", "profit", "employees", "assets", "capital")}
    use_headers = len(headers) == len(cells) and len(cells) > 0
    for index, cell in enumerate(cells):
        raw = cell.get_text(" ", strip=True)
        contexts = [cell.get(attr, "") for attr in ("title", "aria-label", "data-label")]
        # Some result layouts wrap a label and one value together.
        parent = cell.parent
        if parent and parent.name not in ("li", "body", "html", "[document]") and len(parent.select(VALUES)) == 1:
            wrapper = BeautifulSoup(str(parent), "html.parser")
            for value in wrapper.select(VALUES):
                value.decompose()
            contexts.append(wrapper.get_text(" ", strip=True))
        if use_headers:
            contexts.append(headers[index])
        # A value cell may itself contain "Revenue 2025: 1.234,00".
        if financial_metric(raw):
            contexts.append(raw)
        label = " | ".join(text for text in contexts if text)
        metric = financial_metric(label)
        value = parse_optional_number(raw)
        if value is None and ":" in raw:
            value = parse_optional_number(raw.rsplit(":", 1)[1])
        entries.append({"raw": raw, "label": label, "metric": metric, "year": 2025, "value": value})

    errors = bool(headers) and len(headers) != len(cells)
    recognized = 0
    for entry in entries:
        metric = entry["metric"]
        if not metric:
            continue
        recognized += 1
        unique = sum(e["metric"] == metric for e in entries) == 1
        if not unique:
            errors = True
        elif entry["value"] is not None:
            result[f"{metric}_2025"] = entry["value"]
        elif entry["raw"].strip().lower() not in ("", "-", "—", "n/a", "ni podatka"):
            errors = True
    if cells and not recognized:
        errors = True
    missing = any(result[f"{metric}_2025"] is None for metric in ("revenue", "profit", "employees"))
    missing = missing or any(e["metric"] and e["value"] is None for e in entries)
    result["financial_status"] = "PARSE_ERROR" if errors else "PARTIAL" if missing else "OK"
    result["financial_raw_json"] = json.dumps({
        "configured_year": 2025, "year_source": "user_configuration",
        "cells": entries, "headers": list(headers),
    }, ensure_ascii=False)
    return result


def results_financial_headers(page):
    # Keep every heading in order, including unknown metrics, to avoid shifting
    # values into adjacent columns. These selectors were inspected in live GVIN.
    return page.locator(".advenceResultDisplaySubjektFinHeader").evaluate_all("""nodes => nodes
        .filter(n => n.getClientRects().length)
        .map(n => [n.innerText, n.getAttribute('title'), n.getAttribute('aria-label')]
            .filter(Boolean).join(' '))""")


def expected_result_count(page):
    labels = page.locator('[id$="_lblPageStats"]:visible').all_inner_texts()
    if len(labels) != 1:
        return None
    text = labels[0].strip()
    # Accept integers and conventional thousands separators, not page ranges.
    if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:[.\s][0-9]{3})+)", text):
        return None
    return int(re.sub(r"[.\s]", "", text))


def parse_company_row(item, headers=()):
    # A snapshot avoids locator waits for optional fields and row shifts.
    soup = BeautifulSoup(item if isinstance(item, str) else item.inner_html(), "html.parser")

    def text(selector):
        node = soup.select_one(selector)
        return " ".join(node.get_text(" ", strip=True).split()) if node else ""

    name = text("h3 a")
    if not name:
        raise ValueError("Missing company name")
    address = text("div.address")
    link = soup.select_one('a[href*="CompanyId="]')
    href = link.get("href", "") if link else ""
    company = {
        "company_name": name,
        "address": address,
        "municipality": municipality_from_address(address),
        "registration_number": re.sub(r"^Matična:\s*", "", text("span.registrationNumber"), flags=re.I),
        "tax_number": re.sub(r"^Davčna:\s*", "", text("span.taxNumber"), flags=re.I),
        "gvin_company_id": company_id_from_href(href),
        "gvin_detail_url": detail_url_from_href(href),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        # Compatibility with existing matching callers; never fetched here.
        "domain": "", "email": "", "email_domain": "",
    }
    company.update(financial_fields(soup, headers))
    return company


def page_identity(page):
    return page.evaluate(IDENTITIES_JS)


def fingerprint(identities):
    return hashlib.sha256(json.dumps(sorted(identities), ensure_ascii=False).encode()).hexdigest()[:20]


def collect_page(page, stats=None):
    if stats is None:
        stats = {}
    headers = results_financial_headers(page)
    snapshots = page.locator(ROWS).evaluate_all("nodes => nodes.map(n => n.outerHTML)")
    stats.update(rows=len(snapshots), parsed=0, inserted=0, updated=0, failed=0)
    companies = []
    for index, html in enumerate(snapshots, 1):
        try:
            company = parse_company_row(html, headers)
            companies.append(company)
            stats["parsed"] += 1
            if company["financial_status"] != "OK":
                LOG.warning("Row %s %s financials=%s; raw values retained: %s", index,
                            company["company_name"], company["financial_status"], company["financial_raw_json"])
        except (ValueError, TypeError) as exc:
            stats["failed"] += 1
            LOG.error("Row %s parse failed: %s; row=%s", index, exc, html)
    return companies


def access_blocked(page):
    # Detect visible challenges only; never interact with them.
    for selector in ('iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]', 'input[name*="captcha" i]'):
        for node in page.locator(selector).all():
            if node.is_visible():
                LOG.error("Visible access challenge; stopping for manual action.")
                return True
    return False


def next_page(page, previous=None, timeout=30000):
    previous = previous if previous is not None else page_identity(page)
    if not previous or access_blocked(page):
        return False
    controls = page.locator(NEXT)
    if controls.count() != 1 or not controls.first.is_visible():
        LOG.info("Next page unavailable; stopping.")
        return False
    control = controls.first
    disabled = control.evaluate("""n => !!n.closest(
        '[disabled], [aria-disabled="true"], .disabled, .aspNetDisabled') ||
        !(n.getAttribute('href') || n.getAttribute('onclick'))""")
    if disabled:
        LOG.info("Next page disabled/unavailable; stopping.")
        return False
    action = (control.get_attribute("href") or "") + (control.get_attribute("onclick") or "")
    if "__doPostBack" not in action or "bamSearch_lnkPagingNext" not in action:
        LOG.error("Next control has an unrecognized action; stopping without navigation.")
        return False
    try:
        # Activate the actual existing ASP.NET Next control, retaining its
        # __doPostBack behavior without hardcoding a potentially stale control ID.
        control.click(timeout=timeout)
        page.wait_for_function(
            """previous => {
                const current = (""" + IDENTITIES_JS + """)();
                return current.length && current[0] !== previous[0] &&
                    JSON.stringify([...current].sort()) !== JSON.stringify([...previous].sort());
            }""", arg=previous, timeout=timeout,
        )
        human_pause(page)
        current = page_identity(page)
        if (not current or current[0] == previous[0] or fingerprint(current) == fingerprint(previous)):
            LOG.error("Pagination did not produce a stable changed results page; stopping.")
            return False
        return not access_blocked(page)
    except Exception as exc:
        LOG.error("Pagination failed after committed page %s: %s; stopping. Check Chrome before restarting.",
                  fingerprint(previous), exc)
        return False


def process_page(page, page_no=1, collected_ids=None):
    stats = {}
    before = page_identity(page)
    LOG.info("PAGE_START page=%s fingerprint=%s url=%s", page_no, fingerprint(before), page.url)
    companies = collect_page(page, stats)
    if page_identity(page) != before:
        raise RuntimeError("Results changed during parsing; page not saved")
    committed_ids = set()
    conn = get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for company in companies:
                conn.execute("SAVEPOINT company_row")
                try:
                    action, company_id = save_company(company, conn, return_id=True)
                    stats[action] += 1
                    # Count canonical SQLite identities, not duplicate result rows.
                    committed_ids.add(company_id)
                    conn.execute("RELEASE company_row")
                except (IdentityConflict, sqlite3.IntegrityError) as exc:
                    conn.execute("ROLLBACK TO company_row")
                    conn.execute("RELEASE company_row")
                    stats["failed"] += 1
                    LOG.error("Company %s persistence failed: %s; row=%s", company["company_name"], exc,
                              json.dumps(company, ensure_ascii=False))
        if collected_ids is not None:
            collected_ids.update(committed_ids)
        # Only report counts as saved after the page transaction commits.
        LOG.info("PAGE_COMMITTED page=%s fingerprint=%s %s", page_no, fingerprint(before), json.dumps(stats))
    finally:
        conn.close()
    print(f"\nPage {page_no}\n  {stats['rows']} rows found\n  {stats['parsed']} parsed\n"
          f"  {stats['inserted']} inserted\n  {stats['updated']} updated\n  {stats['failed']} failed", flush=True)
    return stats["inserted"] + stats["updated"]


def run_collection(page):
    expected = expected_result_count(page)
    collected_ids = set()
    seen, seen_first = set(), set()
    page_no = 1
    LOG.info("Expected results: %s; configured financial year: 2025", expected)
    try:
        while True:
            if expected is not None and len(collected_ids) >= expected:
                message = f"Collection complete: {len(collected_ids)} / {expected} expected companies collected."
                print(message, flush=True)
                LOG.info(message)
                break
            if access_blocked(page):
                break
            identity = page_identity(page)
            if not identity:
                LOG.info("No result rows; stopping. Check search/login state manually.")
                break
            signature = fingerprint(identity)
            if signature in seen or identity[0] in seen_first:
                LOG.warning("Repeated results page %s; stopping.", signature)
                break
            process_page(page, page_no, collected_ids)
            seen.add(signature)
            seen_first.add(identity[0])
            if expected is not None and len(collected_ids) >= expected:
                continue  # Report completion before making another Next request.
            if not next_page(page, identity):
                break
            page_no += 1
    finally:
        if expected is not None and len(collected_ids) < expected:
            message = f"Collection incomplete: {len(collected_ids)} / {expected} expected companies collected."
            print("WARNING: " + message, flush=True)
            LOG.warning(message)
        LOG.info("RUN_END unique_saved=%s expected=%s last_attempted_page=%s",
                 len(collected_ids), expected, page_no)
    return collected_ids


def main():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()])
    LOG.info("RUN_START result-pages-only; log=%s", LOG_PATH)
    try:
        initialize_database()
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            input("Manually log in and prepare a small GVIN search, then press ENTER...")
            pages = [page for context in browser.contexts for page in context.pages
                     if (urlparse(page.url).hostname or "").lower() in ("gvin.com", "www.gvin.com")]
            candidates = [page for page in pages if page.locator(ROWS).count()]
            if len(candidates) != 1:
                LOG.error("Expected one GVIN results tab, found %s. Prepare one results tab and restart.", len(candidates))
                return
            page = candidates[0]
            run_collection(page)
    except KeyboardInterrupt:
        LOG.warning("Interrupted. Committed pages are safe; repeat the search to collect the full expected total.")
    except Exception:
        LOG.exception("Collection stopped. The current transaction was rolled back if uncommitted.")
        raise


if __name__ == "__main__":
    main()
