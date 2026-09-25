import time
import random
import requests

from bs4 import BeautifulSoup

from settings import (
    WEBSITE_CACHE_DIR,
    REQUEST_TIMEOUT,
    USER_AGENT
)

from database import (
    get_connection,
)

# -------------------------------------------------
# Public email providers
# -------------------------------------------------

PUBLIC_EMAIL_PROVIDERS = {

    "gmail.com",
    "hotmail.com",
    "outlook.com",
    "live.com",

    "yahoo.com",

    "icloud.com",
    "me.com",

    "siol.net",

    "t-2.net",

    "amis.net",

    "aol.com"

}

# -------------------------------------------------
# Session
# -------------------------------------------------

session = requests.Session()

session.headers.update({

    "User-Agent": USER_AGENT

})

# -------------------------------------------------
# Cache folder
# -------------------------------------------------

WEBSITE_CACHE_DIR.mkdir(
    parents=True,
    exist_ok=True
)

# -------------------------------------------------
# Helpers
# -------------------------------------------------


def random_delay():

    delay = random.uniform(1.0, 3.0)

    time.sleep(delay)


def sanitize_url(url):

    if not url:

        return None

    url = url.strip()

    if (

        not url

        or url.lower() == "ni podatka"

    ):

        return None

    if not url.startswith("http"):

        url = "https://" + url

    return url


def website_from_email(email):

    if not email:

        return None

    if "@" not in email:

        return None

    domain = email.split("@")[1].lower()

    if domain in PUBLIC_EMAIL_PROVIDERS:

        return None

    return "https://" + domain


def get_cache_filename(company_id):

    return WEBSITE_CACHE_DIR / f"{company_id}.html"


def already_cached(company_id):

    conn = get_connection()

    cur = conn.cursor()

    cur.execute("""

        SELECT id

        FROM website_cache

        WHERE company_id = ?

    """, (company_id,))

    row = cur.fetchone()

    conn.close()

    return row is not None

# -------------------------------------------------
# Download website
# -------------------------------------------------

def download_website(url):

    url = sanitize_url(url)

    if not url:

        return {

            "success": False,

            "url": "",

            "status": 0,

            "title": "",

            "content_type": "",

            "duration": 0,

            "html": "",

            "error": "Invalid URL"

        }

    try:

        start = time.time()

        response = session.get(

            url,

            timeout=REQUEST_TIMEOUT,

            allow_redirects=True

        )

        duration = int(

            (time.time() - start) * 1000

        )

        content_type = response.headers.get(

            "Content-Type",

            ""

        )

        soup = BeautifulSoup(

            response.text,

            "html.parser"

        )

        title = ""

        if soup.title:

            title = soup.title.get_text(

                strip=True

            )

        return {

            "success": True,

            "url": response.url,

            "status": response.status_code,

            "title": title,

            "content_type": content_type,

            "duration": duration,

            "html": response.text

        }

    except Exception as e:

        return {

            "success": False,

            "url": url,

            "status": 0,

            "title": "",

            "content_type": "",

            "duration": 0,

            "html": "",

            "error": str(e)

        }


# -------------------------------------------------
# Cache
# -------------------------------------------------

def save_html(company_id, html):

    filename = get_cache_filename(company_id)

    filename.write_text(

        html,

        encoding="utf-8",

        errors="ignore"

    )

    return filename


# -------------------------------------------------
# Save metadata
# -------------------------------------------------

def save_cache_record(

    company_id,

    result,

    filename

):

    conn = get_connection()

    cur = conn.cursor()

    cur.execute("""

        INSERT OR REPLACE INTO website_cache(

            company_id,

            url,

            cache_file,

            http_status,

            page_title,

            content_type,

            crawl_duration_ms,

            crawled_at

        )

        VALUES(

            ?,?,?,?,?,?,?,datetime('now')

        )

    """, (

        company_id,

        result["url"],

        str(filename),

        result["status"],

        result["title"],

        result["content_type"],

        result["duration"]

    ))

    conn.commit()

    conn.close()

    # -------------------------------------------------
# Crawl one company
# -------------------------------------------------

def crawl_company(company):

    company_id = company["id"]

    if already_cached(company_id):

        print(f"SKIP {company['company_name']}")

        return

    website = company["website"]

    if website and website.lower() == "ni podatka":

        website = None

    if not website:

        website = website_from_email(

            company["email"]

        )

    if not website:

        print(

            f"NO WEBSITE : "

            f"{company['company_name']}"

        )

        return

    print()

    print("-" * 60)

    print(company["company_name"])

    print(website)

    result = download_website(

        website

    )

    if not result["success"]:

        print(result["error"])

        return

    filename = save_html(

        company_id,

        result["html"]

    )

    save_cache_record(

        company_id,

        result,

        filename

    )

    print(

        f"OK "

        f"{result['status']}"

    )

    random_delay()


# -------------------------------------------------
# Main
# -------------------------------------------------

def main():

    from database import (

        get_companies_for_enrichment

    )

    companies = (

        get_companies_for_enrichment()

    )

    print()

    print(

        f"{len(companies)} "

        f"companies to crawl"

    )

    for index, company in enumerate(

        companies,

        start=1

    ):

        print()

        print(

            f"[{index}/{len(companies)}]"

        )

        crawl_company(company)

    print()

    print(

        "Crawler finished."

    )

if __name__ == "__main__":
    main()