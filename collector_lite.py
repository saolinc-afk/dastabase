# ----------------------------------------------------
# Dastabase
# GVIN Lite Collector
# Release 0.9
# ----------------------------------------------------

import random
import re
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright

from database_lite import (
    initialize_database,
    save_company
)

BASE_URL = "https://www.gvin.com"
CDP_URL = "http://127.0.0.1:9222"

POSTBACK_JS = "__doPostBack('ctl00$contentPlaceHolderMain$ctl00$ctl04$bamSearch$ctl21$bamSearch_lnkPagingNext','')"


# ----------------------------------------------------
# Helpers
# ----------------------------------------------------

def human_pause(page):

    r = random.random()

    if r < 0.80:
        page.wait_for_timeout(random.randint(700, 1300))

    elif r < 0.95:
        page.wait_for_timeout(random.randint(1800, 3500))

    else:
        page.wait_for_timeout(random.randint(5000, 9000))


def parse_number(value):

    if value is None:
        return 0.0

    value = value.strip()

    if value == "":
        return 0.0

    value = value.replace(".", "")
    value = value.replace(",", ".")

    try:
        return round(float(value), 2)

    except:
        return 0.0


def municipality_from_address(address):

    m = re.search(
        r"(\d{4}\s+[A-Za-zČŠŽčšž\- ]+)$",
        address
    )

    if m:
        return m.group(1).strip()

    return ""


def company_id_from_href(href):

    if not href:
        return ""

    m = re.search(r"CompanyId=(\d+)", href)
    if m:
        return m.group(1)
    return ""


def find_gvin_page(context):

    for page in context.pages:

        if "gvin" in page.url.lower():
            return page

    return None

# ----------------------------------------------------
# Parse one company row
# ----------------------------------------------------

def parse_company_row(item):

    company = {}

    # -----------------------------
    # Company name
    # -----------------------------

    company["company_name"] = (
        item.locator("h3 span")
        .inner_text()
        .strip()
    )

    # -----------------------------
    # Address
    # -----------------------------

    address = (
        item.locator("div.address")
        .inner_text()
        .replace("\n", " ")
        .replace("\xa0", " ")
        .strip()
    )

    address = " ".join(address.split())

    company["address"] = address
    company["municipality"] = municipality_from_address(address)

    # -----------------------------
    # Registration number
    # -----------------------------

    reg = (
        item.locator("span.registrationNumber")
        .inner_text()
    )

    reg = (
        reg.replace("Matična:", "")
        .replace("\xa0", "")
        .strip()
    )

    company["registration_number"] = reg

    # -----------------------------
    # Tax number
    # -----------------------------

    tax = (
        item.locator("span.taxNumber")
        .inner_text()
    )

    tax = (
        tax.replace("Davčna:", "")
        .replace("\xa0", "")
        .strip()
    )

    company["tax_number"] = tax

    # -----------------------------
    # Financial values
    # -----------------------------

    values = item.locator(
        "div.advanceResultDataDisplaySubjektFix"
    )

    company["revenue_2025"] = 0.0
    company["profit_2025"] = 0.0
    company["employees_2025"] = 0.0

    if values.count() >= 3:

        company["revenue_2025"] = parse_number(
            values.nth(0).inner_text()
        )

        company["profit_2025"] = parse_number(
            values.nth(1).inner_text()
        )

        company["employees_2025"] = parse_number(
            values.nth(2).inner_text()
        )

    # -----------------------------
    # Company ID
    # -----------------------------

    href = (
        item.locator("h3 a")
        .get_attribute("href")
    )

    company["gvin_company_id"] = (
        company_id_from_href(href)
    )

    return company

# ----------------------------------------------------
# Collect one results page
# ----------------------------------------------------

def collect_page(page):

    companies = []

    rows = page.locator("li.newsearchIcon.semaphoreG")

    if rows.count() == 0:
        print("No companies found.")
        return []

    print(f"Found {rows.count()} companies")

    for i in range(rows.count()):

        try:

            company = parse_company_row(
                rows.nth(i)
            )

            companies.append(company)

            print(
                f"✓ {company['company_name']}"
            )

        except Exception as e:

            print(
                f"Skipped company: {e}"
            )

    return companies


# ----------------------------------------------------
# Go to next page
# ----------------------------------------------------

def next_page(page):

    try:

        page.evaluate(POSTBACK_JS)

        page.wait_for_load_state(
            "domcontentloaded"
        )

        page.wait_for_timeout(
            random.randint(1000, 2000)
        )

        return True

    except:

        return False


# ----------------------------------------------------
# Save one page
# ----------------------------------------------------

def process_page(page):

    companies = collect_page(page)

    for company in companies:

        save_company(company)

    print(
        f"Saved {len(companies)} companies."
    )

    return len(companies)

# ----------------------------------------------------
# Main
# ----------------------------------------------------

def main():

    initialize_database()

    with sync_playwright() as p:

        browser = p.chromium.connect_over_cdp(
            CDP_URL
        )

        if not browser.contexts:
            print("No Chrome context found.")
            return

        context = browser.contexts[0]

        page = find_gvin_page(context)

        if page is None:

            print("GVIN page not found.")
            return

        input(
            "Open GVIN search results and press ENTER..."
        )

        page_no = 1
        total = 0
        seen_first_company_ids = set()

        while True:

            print()
            print("=" * 60)
            print(f"PAGE {page_no}")
            print("=" * 60)

            rows = page.locator("li.newsearchIcon")

            if rows.count() == 0:
                print("No companies found. Finished.")
                break

            try:
                href = rows.nth(0).locator("h3 a").first.get_attribute("href")
                first_company_id = company_id_from_href(href)
            except Exception:
                first_company_id = ""

            if first_company_id and first_company_id in seen_first_company_ids:
                print("Reached last page.")
                break

            if first_company_id:
                seen_first_company_ids.add(first_company_id)

            count = process_page(page)

            total += count

            print()
            print(
                f"Total companies: {total}"
            )

            human_pause(page)

            if not next_page(page):

                print()
                print("Finished.")
                break

            page_no += 1

    print()
    print("=" * 60)
    print(f"Collected {total} companies.")
    print("=" * 60)


if __name__ == "__main__":
    main()
    