from playwright.sync_api import sync_playwright
from database import initialize_database, save_company

BASE_URL = "https://www.gvin.com"
CDP_URL = "http://127.0.0.1:9222"


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

        except Exception:
            pass

    # odstrani duplikate
    company_links = list(dict.fromkeys(company_links))

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


def find_gvin_page(context):

    for page in context.pages:
        try:
            if "gvin" in page.url.lower():
                print(f"Connected to: {page.url}")
                return page
        except Exception:
            pass

    return None


def main():

    initialize_database()

    with sync_playwright() as p:

        print("Connecting to existing Chrome...")

        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)

        except Exception as e:
            print()
            print("ERROR")
            print("---------------------------------------")
            print("Cannot connect to Chrome.")
            print()
            print("Start Chrome like this:")
            print()
            print("/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222")
            print()
            print(e)
            return

        if not browser.contexts:
            print("No browser context found.")
            return

        context = browser.contexts[0]

        page = find_gvin_page(context)

        if page is None:
            print()
            print("GVIN tab not found.")
            print("Open GVIN, log in and display the search results first.")
            return

        print()
        input("When the company list is visible press ENTER...")

        page.wait_for_timeout(1000)

        company_links = get_company_links(page)

        print()

        saved = 0

        for index, href in enumerate(company_links, start=1):

            print("=" * 60)
            print(f"[{index}/{len(company_links)}]")

            url = BASE_URL + href

            print(url)

            page.goto(url)

            page.wait_for_load_state("networkidle")

            company = parse_company(page)

            print(company["company_name"])

            save_company(company)

            saved += 1

        print()
        print("=" * 60)
        print("FINISHED")
        print("=" * 60)
        print(f"Processed : {len(company_links)}")
        print(f"Saved     : {saved}")
        print("=" * 60)

        browser.close()


if __name__ == "__main__":
    main()