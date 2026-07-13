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