from playwright.sync_api import sync_playwright
from database import initialize_database, save_company

BASE_URL = "https://www.gvin.com"


def get_company_links(page):

    print("Reading result list...")

    links = page.locator("a")

    company_links = []

    for i in range(links.count()):

        try:

            href = links.nth(i).get_attribute("href")

            if not href:
                continue

            if "Company.aspx?CompanyId=" not in href:
                continue

            company_links.append(href)

        except:
            pass

    print(f"Found {len(company_links)} companies.")

    return company_links


def parse_company(page):

    company = page.locator("h2.mL10").text_content().strip()

    text = page.locator("body").inner_text()

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    def value_after(prefix):

        for line in lines:

            if line.startswith(prefix):

                return line.replace(prefix, "").strip()

        return ""

    address = ""

    for i, line in enumerate(lines):

        if line.startswith("Matična številka:"):

            if i > 0:

                address = lines[i - 1]

            break

    return {

        "company_name": company,
        "tax_number": value_after("Davčna številka:"),
        "registration_number": value_after("Matična številka:"),
        "address": address,
        "activity": value_after("Dejavnost:"),
        "phone": value_after("Telefon:"),
        "website": value_after("URL:"),
        "email": value_after("E-pošta:")

    }