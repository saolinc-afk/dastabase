"""
----------------------------------------------------
Dastabase
Search Engine
Release 2.0
----------------------------------------------------
"""

from ddgs import DDGS
from urllib.parse import urlparse


# ----------------------------------------------------
# SETTINGS
# ----------------------------------------------------

MAX_RESULTS = 10

BLACKLIST = {

    "facebook.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "x.com",
    "twitter.com",

    "ajpes.si",
    "bizi.si",
    "gvin.com",
    "companywall.si",

    "wikipedia.org",
    "najdi.si",

}


# ----------------------------------------------------
# HELPERS
# ----------------------------------------------------

def normalize_host(url):

    try:

        host = urlparse(url).netloc.lower()

        if host.startswith("www."):

            host = host[4:]

        return host

    except:

        return ""


def allowed(url):

    host = normalize_host(url)

    if host == "":

        return False

    for blocked in BLACKLIST:

        if host.endswith(blocked):

            return False

    return True


# ----------------------------------------------------
# SEARCH
# ----------------------------------------------------

def run_query(query):

    print()

    print("SEARCH:", query)

    results = []

    try:

        with DDGS() as ddgs:

            for r in ddgs.text(

                query,

                max_results=MAX_RESULTS

            ):

                url = r.get("href") or r.get("link") or ""

                if not allowed(url):

                    continue

                results.append({

                    "url": url,

                    "title": r.get("title",""),

                    "snippet": r.get("body",""),

                    "host": normalize_host(url)

                })

    except Exception as e:

        print("Search error:", e)

    return results


# ----------------------------------------------------
# COMPANY SEARCH
# ----------------------------------------------------

def search_company(company):

    queries = [

        f'"{company["company_name"]}"',

        f'"{company["company_name"]}" Slovenija',

        f'"{company["company_name"]}" {company["municipality"]}',

        f'"{company["company_name"]}" {company["address"]}'

    ]

    all_results = []

    seen = set()

    for query in queries:

        results = run_query(query)

        for r in results:

            if r["host"] in seen:

                continue

            seen.add(r["host"])

            all_results.append(r)

    return all_results

# ----------------------------------------------------
# DISCOVER URLS
# ----------------------------------------------------

def discover_urls(company):

    results = search_company(company)

    urls = []

    seen = set()

    for result in results:

        url = result["url"]

        host = result["host"]

        if host in seen:
            continue

        seen.add(host)
        urls.append(url)

    return urls


# ----------------------------------------------------
# DISCOVER RESULTS
# ----------------------------------------------------

def discover_results(company):

    return search_company(company)


# ----------------------------------------------------
# TEST
# ----------------------------------------------------

if __name__ == "__main__":

    company = {

        "company_name": "SLOMETAL d.o.o.",

        "municipality": "5000 Nova Gorica",

        "address": "Ulica Gradnikove brigade 6",

    }

    results = discover_results(company)

    print()
    print("=" * 70)
    print("SEARCH RESULTS")
    print("=" * 70)
    print()

    if not results:

        print("No results found.")

    else:

        for i, result in enumerate(results, start=1):

            print(f"{i:2d}. {result['url']}")

            if result["title"]:
                print(f"    {result['title']}")

            if result["snippet"]:
                print(f"    {result['snippet'][:120]}")

            print()

    print("=" * 70)
    print(f"{len(results)} unique domains")
    print("=" * 70)
