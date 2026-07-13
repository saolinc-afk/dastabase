"""
----------------------------------------------------
Dastabase
Website Verifier
Release 3.0
DEL 1 / 2
----------------------------------------------------
"""

import json
import re
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": "Mozilla/5.0 Dastabase"
}

TIMEOUT = 10

CONTACT_PATHS = [
    "/kontakt",
    "/contact",
    "/o-nas",
    "/about",
    "/company",
    "/podjetje",
]


# ----------------------------------------------------
# Helpers
# ----------------------------------------------------

LEGAL_FORMS = (
    "d.o.o.",
    "d.d.",
    "s.p.",
)


def normalize_company_name(text: str) -> str:

    if not text:
        return ""

    text = text.lower()

    # odreži vse od pravne oblike naprej

    for form in LEGAL_FORMS:

        pos = text.find(form)

        if pos != -1:

            text = text[:pos]

            break

    # odstrani ločila

    text = re.sub(

        r"[^a-z0-9čšžćđ ]",

        " ",

        text

    )

    # normaliziraj presledke

    return " ".join(text.split())


def homepage(url: str) -> str:

    p = urlparse(url)

    return f"{p.scheme}://{p.netloc}"


def fetch(url):

    try:

        r = requests.get(

            url,

            headers=HEADERS,

            timeout=TIMEOUT,

            allow_redirects=True,

        )

        if r.status_code >= 400:

            return None

        return r

    except Exception:

        return None


def soup_from_html(html):

    return BeautifulSoup(

        html,

        "html.parser"

    )


# ----------------------------------------------------
# DOMAIN SCORE
# ----------------------------------------------------

def domain_score(url, company_name):

    score = 0

    evidence = []

    host = urlparse(url).netloc.lower()

    host = host.replace("www.", "")

    domain = host.split(".")[0]

    company = company_name.replace(" ", "")

    if domain == company:

        score += 40
        evidence.append("domain_exact")

    elif company in domain:

        score += 30
        evidence.append("domain_contains")

    elif domain in company:

        score += 20
        evidence.append("domain_partial")

    return score, evidence


# ----------------------------------------------------
# JSON-LD
# ----------------------------------------------------

def jsonld_score(soup, company_name):

    score = 0

    evidence = []

    scripts = soup.find_all(

        "script",

        type="application/ld+json"

    )

    for script in scripts:

        try:

            data = json.loads(script.string)

        except Exception:

            continue

        if isinstance(data, dict):

            data = [data]

        for item in data:

            if not isinstance(item, dict):

                continue

            if item.get("@type") != "Organization":

                continue

            org = normalize_company_name(

                item.get("name", "")

            )

            if company_name in org:

                score += 30

                evidence.append("jsonld")


    return score, evidence


# ----------------------------------------------------
# BODY SCORE
# ----------------------------------------------------

def body_score(

    text,

    company_name,

    municipality

):

    score = 0

    evidence = []

    body = normalize_company_name(text)

    if company_name in body:

        score += 30

        evidence.append("company_name")

    municipality = normalize_company_name(

        municipality

    )

    if municipality and municipality in body:

        score += 20

        evidence.append("municipality")

    return score, evidence

# ----------------------------------------------------
# CONTACT PAGE
# ----------------------------------------------------

def find_contact_page(base_url):

    for path in CONTACT_PATHS:

        try:

            r = requests.get(

                urljoin(base_url, path),

                headers=HEADERS,

                timeout=5,

                allow_redirects=True,

            )

            if r.status_code == 200:

                return r.url

        except Exception:

            pass

    return ""


# ----------------------------------------------------
# SCORE SINGLE PAGE
# ----------------------------------------------------

def score_page(company, response):

    soup = soup_from_html(response.text)

    title = ""

    if soup.title:

        title = normalize_company_name(

            soup.title.text

        )

    company_name = normalize_company_name(

        company["company_name"]

    )

    municipality = company.get(

        "municipality",

        ""

    )

    text = soup.get_text(

        " ",

        strip=True

    )

    score = 0

    evidence = []

    #
    # domain
    #

    s, e = domain_score(

        response.url,

        company_name

    )

    score += s

    evidence.extend(e)

    #
    # title
    #

    if company_name in title:

        score += 40

        evidence.append(

            "title"

        )

    #
    # jsonld
    #

    s, e = jsonld_score(

        soup,

        company_name

    )

    score += s

    evidence.extend(e)

    #
    # body
    #

    s, e = body_score(

        text,

        company_name,

        municipality

    )

    score += s

    evidence.extend(e)

    return {

        "score": score,

        "evidence": evidence

    }


# ----------------------------------------------------
# VERIFY
# ----------------------------------------------------

def verify(company, url):

    candidates = []

    #
    # exact url
    #

    r = fetch(url)

    if r:

        candidates.append(r)

    #
    # homepage
    #

    home = homepage(url)

    if home != url:

        r = fetch(home)

        if r:

            candidates.append(r)

    #
    # nič ni uspelo
    #

    if not candidates:

        return {

            "verified": False,

            "confidence": 0,

            "final_url": home,

            "contact_page": "",

            "about_page": "",

            "evidence": []

        }

    best = None

    best_score = -1

    for response in candidates:

        result = score_page(

            company,

            response

        )

        if result["score"] > best_score:

            best_score = result["score"]

            best = (

                response,

                result

            )

    response, result = best

    contact = find_contact_page(

        homepage(

            response.url

        )

    )

    confidence = result["score"]

    if contact:

        confidence += 10

        result["evidence"].append(

            "contact_page"

        )

    confidence = min(

        confidence,

        100

    )

    return {

        "verified": confidence >= 60,

        "confidence": confidence,

        "final_url": homepage(

            response.url

        ),

        "contact_page": contact,

        "about_page": "",

        "evidence": sorted(

            set(

                result["evidence"]

            )

        )

    }


# ----------------------------------------------------
# TEST
# ----------------------------------------------------

if __name__ == "__main__":

    company = {

        "company_name": "SLOMETAL d.o.o.",

        "municipality": "Nova Gorica",

        "address": "Ulica Gradnikove brigade 6"

    }

    print()

    print("=" * 60)

    print(

        verify(

            company,

            "https://www.slometal.si/company.html"

        )

    )

    print("=" * 60)
