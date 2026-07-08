from playwright.sync_api import sync_playwright

from database import initialize_database, save_company


def main():
    print("Collector version 0.3")

    initialize_database()

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False
        )

        page = browser.new_page()

        print("Opening GVIN...")

        page.goto("https://www.gvin.com")

        input(
            "\nPrijavi se v GVIN in odpri poljubno podjetje.\n"
            "Ko vidiš stran podjetja, pritisni ENTER..."
        )

        page.wait_for_timeout(1000)

        # Naziv podjetja
        company = page.locator("h2.mL10").text_content().strip()

        # Celotno besedilo strani
        text = page.locator("body").inner_text()

        # Razbij na vrstice
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

        # Naslov je vrstica pred "Matična številka:"
        address = ""

        for i, line in enumerate(lines):
            if line.startswith("Matična številka:"):
                if i > 0:
                    address = lines[i - 1]
                break

        company_data = {
            "company_name": company,
            "tax_number": value_after("Davčna številka:"),
            "registration_number": value_after("Matična številka:"),
            "address": address,
            "activity": value_after("Dejavnost:"),
            "phone": value_after("Telefon:"),
            "website": value_after("URL:"),
            "email": value_after("E-pošta:")
        }

        print("\n========================")
        print("Company")
        print("========================")

        for key, value in company_data.items():
            print(f"{key:22}: {value}")

        print("========================\n")

        save_company(company_data)

        browser.close()


if __name__ == "__main__":
    main()