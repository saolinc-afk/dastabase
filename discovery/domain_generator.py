"""
----------------------------------------------------
Dastabase
Domain Generator
Release 1.0
----------------------------------------------------
"""

import re
import unicodedata


# Pravne oblike, ki jih odstranimo
LEGAL_FORMS = [
    "d.o.o.",
    "d.o.o",
    "d.d.",
    "d.d",
    "d.n.o.",
    "k.d.",
    "s.p.",
    "z.o.o.",
]


def remove_accents(text: str) -> str:
    """
    Č -> C
    Š -> S
    Ž -> Z
    """
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def normalize_company_name(name: str) -> str:

    text = name.lower()

    #
    # odreži vse od prve pravne oblike naprej
    #

    for form in LEGAL_FORMS:

        pos = text.find(form)

        if pos != -1:

            text = text[:pos]

            break

    text = remove_accents(text)

    text = re.sub(
        r"[^a-z0-9 ]",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()

    return text.replace(" ", "")

def generate_candidates(company_name: str):

    base = normalize_company_name(company_name)

    if len(base) < 3:
        return []

    domains = []

    for tld in [".si", ".com", ".eu"]:

        domains.append(f"https://{base}{tld}")
        domains.append(f"https://www.{base}{tld}")

    return domains


if __name__ == "__main__":

    tests = [
        "MIKA DOM d.o.o.",
        "LUNAR d.o.o.",
        "AKRAPOVIČ d.d.",
        "RLS MERILNA TEHNIKA d.o.o.",
        "ŠPICA INTERNATIONAL d.o.o."
    ]

    for company in tests:

        print("=" * 60)
        print(company)

        for d in generate_candidates(company):
            print(d)

# Shared URL handling for the existing discovery stages.
from urllib.parse import urlsplit, urlunsplit
import ipaddress


def normalize_domain(value):
    value = str(value or '').strip().lower()
    if value.startswith('mailto:'):
        value = value[7:].split('?', 1)[0]
    if '@' in value and '://' not in value:
        value = value.rsplit('@', 1)[1]
    try:
        host = urlsplit(value if '://' in value else 'https://' + value).hostname or ''
        host = host.rstrip('.').encode('idna').decode('ascii')
        return host[4:] if host.startswith('www.') else host
    except (ValueError, UnicodeError):
        return ''


def normalize_url(value):
    value = str(value or '').strip()
    if not value:
        return ''
    try:
        parsed = urlsplit(value if '://' in value else 'https://' + value)
        if parsed.scheme.lower() not in ('http', 'https') or parsed.username or parsed.password:
            return ''
        host = parsed.hostname or ''
        if not host or '.' not in host or host.endswith('.local') or parsed.port not in (None, 80, 443):
            return ''
        try:
            ipaddress.ip_address(host)
            return ''  # Company sites must be named public hosts.
        except ValueError:
            pass
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/', parsed.query, ''))
    except ValueError:
        return ''


def same_site(url, website):
    # Only exact host / www aliases, never arbitrary sibling domains.
    return bool(normalize_domain(url)) and normalize_domain(url) == normalize_domain(website)
