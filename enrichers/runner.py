"""
runner.py

Runs website crawler for companies that have not yet been enriched.
"""

from database import get_companies_for_enrichment
from crawler.crawler import crawl


INVALID_WEBSITES = {
    "",
    "ni podatka",
    "brez spletne strani",
    "ni",
    "n/a",
    "-",
    "/",
}


def run(limit=None):
    companies = get_companies_for_enrichment(limit)

    print(f"\nFound {len(companies)} companies to process.")

    success = 0
    failed = 0
    skipped = 0

    for company in companies:

        company_id = company["id"]
        company_name = company["company_name"]
        website = (company["website"] or "").strip()

        print("\n" + "=" * 70)
        print(f"[{company_id}] {company_name}")
        print(f"Website: {website}")

        # Skip companies without a valid website
        if website.lower() in INVALID_WEBSITES:
            skipped += 1
            print("❌ No website")
            continue

        # Crawl website
        result = crawl(website)

        if result is None:
            failed += 1
            print("❌ Crawl failed")
            continue

        success += 1

        print(f"✅ Status      : {result['status']}")
        print(f"✅ Final URL   : {result['url']}")
        print(f"✅ Title       : {result['title']}")
        print(f"✅ Description : {result['description']}")
        print(f"✅ Text length : {result['text_length']} characters")

    print("\n" + "=" * 70)
    print("Finished")
    print(f"Successful : {success}")
    print(f"Failed     : {failed}")
    print(f"Skipped    : {skipped}")
    print("=" * 70)


if __name__ == "__main__":

    # Change limit=None to process all companies
    run(limit=3)