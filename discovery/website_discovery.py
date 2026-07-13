"""
----------------------------------------------------
Dastabase
Website Discovery
Release 3.0
DEL 1 / 2
----------------------------------------------------
"""

import bootstrap

from urllib.parse import urlparse

from database_lite import get_connection
from domain_generator import generate_candidates
from website_verifier import verify
from search_engine import discover_urls


CONFIDENCE_THRESHOLD = 60


# ----------------------------------------------------
# LOAD COMPANIES
# ----------------------------------------------------

def load_companies():

    conn = get_connection()

    conn.row_factory = __import__("sqlite3").Row

    cur = conn.cursor()

    cur.execute("""

        SELECT c.*

        FROM companies_lite c

        WHERE NOT EXISTS(

            SELECT 1

            FROM website_discovery w

            WHERE w.company_id = c.id

        )

        ORDER BY c.id

    """)

    rows = [

        dict(r)

        for r in cur.fetchall()

    ]

    conn.close()

    return rows


# ----------------------------------------------------
# SAVE RESULT
# ----------------------------------------------------

def save_result(

    company,

    result,

    method

):

    conn = get_connection()

    cur = conn.cursor()

    cur.execute("""

        INSERT INTO website_discovery(

            company_id,

            website,

            confidence,

            status,

            method,

            checked_at

        )

        VALUES(

            ?,?,?,?,?,datetime('now')

        )

    """,

    (

        company["id"],

        result.get(

            "final_url",

            ""

        ),

        result.get(

            "confidence",

            0

        ),

        "FOUND"

        if result.get(

            "verified"

        )

        else

        "NOT_FOUND",

        method

    ))

    conn.commit()

    conn.close()


# ----------------------------------------------------

# BEST CANDIDATE

# ----------------------------------------------------

def best_candidate(company, urls):

    best = None

    best_score = -1

    best_si = None

    best_si_score = -1

    for url in urls:

        try:

            result = verify(company, url)

            score = result["confidence"]

            print(f"{url}  -->  {score}")

            host = urlparse(

                result["final_url"]

            ).netloc.lower()

            #

            # najboljši .si kandidat

            #

            if host.endswith(".si"):

                if score > best_si_score:

                    best_si = result

                    best_si_score = score

            #

            # najboljši kandidat nasploh

            #

            if score > best_score:

                best = result

                best_score = score

        except Exception as e:

            print()

            print("VERIFY ERROR")

            print(url)

            print(e)

            print()

            continue

    #

    # Če obstaja dobra .si domena,

    # ima prednost pred .com/.eu,

    # razen če je bistveno slabša.

    #

    if (

        best_si

        and best_si["verified"]

        and (

            best is None

            or best_si["confidence"] >= best["confidence"] - 20

        )

    ):

        print()

        print("BEST (.si):")

        print(best_si)

        return best_si

    print()

    print("BEST:")

    print(best)

    return best

# ----------------------------------------------------
# DISCOVER
# ----------------------------------------------------

def discover(company):

    print()

    print(f"=== {company['company_name']} ===")

    #
    # 1. Guess domains
    #

    guessed = generate_candidates(company["company_name"])

    best = best_candidate(

        company,

        guessed

    )

    #
    # dovolj dobro
    #

    if (

        best

        and

        best["confidence"] >= CONFIDENCE_THRESHOLD

    ):

        method = "guess"

        if "domain_exact" in best.get(

            "evidence",

            []

        ):

            method = "guess+domain"

        elif "title" in best.get(

            "evidence",

            []

        ):

            method = "guess+title"

        save_result(

            company,

            best,

            method

        )

        print(

            "FOUND",

            best["final_url"],

            best["confidence"],

            best.get("evidence", [])

        )

        return

    #
    # Search Engine
    #

    print()

    print(

        "Guess not sufficient."

    )

    print(

        "Trying Search Engine..."

    )

    search_results = discover_urls(

        company

    )

    searched = best_candidate(

        company,

        search_results

    )

    #
    # primerjaj
    #

    if (

        searched

        and

        (

            best is None

            or

            searched["confidence"]

            >

            best["confidence"]

        )

    ):

        best = searched

        method = "search"

    else:

        method = "guess"

    #
    # fallback
    #

    if best is None:

        best = {

            "verified": False,

            "confidence": 0,

            "final_url": "",

            "evidence": []

        }

    save_result(

        company,

        best,

        method

    )

    print(

        method.upper(),

        best["final_url"],

        best["confidence"],

        best.get("evidence", [])

    )


# ----------------------------------------------------
# MAIN
# ----------------------------------------------------

def main():

    companies = load_companies()

    print(

        f"{len(companies)} companies to process"

    )

    for company in companies:

        try:

            discover(

                company

            )

        except KeyboardInterrupt:

            raise

        except Exception:
            raise

    print()

    print("=" * 60)

    print("Done.")

    print("=" * 60)


# ----------------------------------------------------
# TEST
# ----------------------------------------------------

if __name__ == "__main__":

    main()
