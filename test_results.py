from playwright.sync_api import sync_playwright


def main():

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False
        )

        page = browser.new_page()

        print("Opening GVIN...")

        page.goto("https://www.gvin.com")

        input(
            "\nPrijavi se v GVIN, naredi iskanje in pridi na stran z rezultati.\n"
            "Ko vidiš seznam podjetij, pritisni ENTER..."
        )

        page.wait_for_timeout(1000)

        # Vse povezave na strani
        links = page.locator("a")

        print(f"\nFound {links.count()} links.\n")

        found = 0

        for i in range(links.count()):

            try:

                href = links.nth(i).get_attribute("href")

                if not href:
                    continue

                if "Company.aspx?CompanyId=" not in href:
                    continue

                name = links.nth(i).inner_text().strip()

                print("=" * 60)
                print(name)
                print(href)

                found += 1

            except Exception:
                pass

        print("\n-------------------------------------")
        print(f"Found {found} company links.")
        print("-------------------------------------")

        browser.close()


if __name__ == "__main__":
    main()