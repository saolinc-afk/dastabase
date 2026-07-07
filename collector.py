from playwright.sync_api import sync_playwright

COMPANY_URL = "https://www.gvin.com/GvinOverview/Pages/Company.aspx?CompanyId=50682&Lang=sl-SI&Mode=GvinSI&App=GvinOverviewSI"


def main():

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False
        )

        page = browser.new_page()

        print("Opening GVIN...")

        page.goto("https://www.gvin.com")

        input(
            "\nPrijavi se v GVIN in odpri podjetje CABLEX.\n"
            "Ko vidiš stran podjetja, pritisni ENTER..."
        )

        page.wait_for_timeout(1000)

        # Naziv podjetja
        company = page.locator("h2.mL10").text_content().strip()

        # Celotno besedilo strani
        text = page.locator("body").inner_text()

        # Razbij besedilo na vrstice
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        # Funkcija za iskanje vrednosti po oznaki
        def value_after(prefix):
            for line in lines:
                if line.startswith(prefix):
                    return line.replace(prefix, "").strip()
            return ""

        # Naslov je vrstica tik pred "Matična številka:"
        address = ""

        for i, line in enumerate(lines):
            if line.startswith("Matična številka:"):
                if i >= 1:
                    address = lines[i - 1]
                break

        # Ostali podatki
        registration = value_after("Matična številka:")
        tax = value_after("Davčna številka:")
        activity = value_after("Dejavnost:")
        phone = value_after("Telefon:")
        website = value_after("URL:")
        email = value_after("E-pošta:")

        # Slovar podjetja
        company_data = {
            "company_name": company,
            "address": address,
            "registration_number": registration,
            "tax_number": tax,
            "activity": activity,
            "phone": phone,
            "website": website,
            "email": email,
        }

        print("\n==============================")
        print("COMPANY DATA")
        print("==============================")

        for key, value in company_data.items():
            print(f"{key:22} : {value}")

        print("==============================\n")

        browser.close()


if __name__ == "__main__":
    main()
