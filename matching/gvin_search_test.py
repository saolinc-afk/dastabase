import os
import random
import sys
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from playwright.sync_api import TimeoutError, sync_playwright

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from collector_lite import collect_page, find_gvin_page, human_pause
from matching.company_matcher import (
    CompanyCandidate,
    CompanyInput,
    best_match,
)
from test_excel import find_column


CDP_URL = "http://127.0.0.1:9222"
INPUT_FILE = Path(PROJECT_ROOT) / "dastabase test_enricher(1).xlsx"
FALLBACK_INPUT_FILE = Path(PROJECT_ROOT) / "dastabase test_enricher.xlsx"
OUTPUT_FILE = Path(PROJECT_ROOT) / "dastabase_gvin_test_output.xlsx"

SEARCH_URL = (
    "https://www.gvin.com/IskalnikCE/Pages/SearchResult.aspx"
    "?Mode=GvinSI&App=GvinIskalnikSI&Kontekst=1"
    "&QueryVsebina={query}&Lang=sl-SI"
)

RATE_LIMIT_MARKERS = [
    "rate limit",
    "too many requests",
    "prevec zahtev",
    "preveč zahtev",
    "poskusite kasneje",
    "temporarily unavailable",
]


def cell_text(value):
    if pd.isna(value):
        return ""

    text = str(value).strip()

    if text.endswith(".0"):
        text = text[:-2]

    return text


def selected_input_file():
    if INPUT_FILE.exists():
        return INPUT_FILE, ""

    if FALLBACK_INPUT_FILE.exists():
        return (
            FALLBACK_INPUT_FILE,
            f"Input file not found: {INPUT_FILE.name}. "
            f"Using fallback: {FALLBACK_INPUT_FILE.name}",
        )

    raise FileNotFoundError(
        f"Input Excel not found: {INPUT_FILE} "
        f"or {FALLBACK_INPUT_FILE}"
    )


def make_search_url(company_name):
    return SEARCH_URL.format(query=quote_plus(company_name))


def detect_gvin_problem(page):
    try:
        body = page.locator("body").inner_text(timeout=5000).lower()
    except Exception as exc:
        return f"Could not read page body: {exc}"

    for marker in RATE_LIMIT_MARKERS:
        if marker in body:
            return f"GVIN rate-limit/error marker found: {marker}"

    if "authenticate" in page.url.lower() or "accounts.bisnode" in page.url.lower():
        return f"GVIN redirected to login/authentication: {page.url}"

    return ""


def candidate_from_company(company):
    return CompanyCandidate(
        name=company.get("company_name", ""),
        domain=company.get("domain", ""),
        email_domain=company.get("email_domain", ""),
        registration_number=company.get("registration_number", ""),
        tax_number=company.get("tax_number", ""),
        address=company.get("address", ""),
        raw=company,
    )


def empty_gvin_result(status, error=""):
    return {
        "GVIN Company ID": "",
        "GVIN Registration": "",
        "GVIN Tax": "",
        "GVIN Company": "",
        "GVIN Address": "",
        "GVIN Domain": "",
        "GVIN Email": "",
        "GVIN Revenue 2025": "",
        "GVIN Employees 2025": "",
        "Match Score": "",
        "Match Confidence": status,
        "Match Reasons": error,
    }


def result_from_match(match):
    candidate = match.candidate
    raw = candidate.raw
    confidence = match.confidence

    if confidence == "REJECT":
        confidence = "NO_MATCH"

    return {
        "GVIN Company ID": raw.get("gvin_company_id", ""),
        "GVIN Registration": candidate.registration_number,
        "GVIN Tax": candidate.tax_number,
        "GVIN Company": candidate.name,
        "GVIN Address": candidate.address,
        "GVIN Domain": candidate.domain,
        "GVIN Email": raw.get("email", ""),
        "GVIN Revenue 2025": raw.get("revenue_2025", ""),
        "GVIN Employees 2025": raw.get("employees_2025", ""),
        "Match Score": match.score,
        "Match Confidence": confidence,
        "Match Reasons": ", ".join(match.reasons),
    }


def build_company_input(row, columns):
    person_col, email_col, phone_col, company_col = columns

    return CompanyInput(
        name=cell_text(row.get(company_col, "")),
        email="" if email_col is None else cell_text(row.get(email_col, "")),
        phone="" if phone_col is None else cell_text(row.get(phone_col, "")),
        person_name="" if person_col is None else cell_text(row.get(person_col, "")),
    )


def print_summary(summary):
    print()
    print("=" * 80)
    print("BATCH SUMMARY")
    print("=" * 80)
    print("Processed:", summary["processed"])
    print("HIGH:", summary["HIGH"])
    print("MEDIUM:", summary["MEDIUM"])
    print("LOW:", summary["LOW"])
    print("NO_MATCH:", summary["NO_MATCH"])
    print("ERROR:", summary["ERROR"])
    print("GVIN registration found:", summary["registration_found"])
    print("GVIN tax found:", summary["tax_found"])
    print("Rate-limit issues:", summary["rate_limit_issues"])
    print("Output:", OUTPUT_FILE)
    print()


def main():
    input_file, warning = selected_input_file()

    if warning:
        print("WARNING:", warning)

    df = pd.read_excel(input_file)

    person_col = find_column(df.columns, ["Ime", "ime", "Oseba", "Kontakt"])
    email_col = find_column(df.columns, ["Email", "E-mail", "E-pošta"])
    phone_col = find_column(df.columns, ["Telefon", "Phone", "Tel"])
    company_col = find_column(df.columns, ["Podjetje", "Company", "Firma"])

    if company_col is None:
        raise ValueError(
            "Could not find company column. "
            f"Available columns: {list(df.columns)}"
        )

    summary = {
        "processed": 0,
        "HIGH": 0,
        "MEDIUM": 0,
        "LOW": 0,
        "NO_MATCH": 0,
        "ERROR": 0,
        "registration_found": 0,
        "tax_found": 0,
        "rate_limit_issues": 0,
    }
    output_rows = []

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)

        if not browser.contexts:
            print("NO CHROME CONTEXT")
            raise SystemExit(1)

        page = find_gvin_page(browser.contexts[0])

        if page is None:
            print("GVIN PAGE NOT FOUND")
            raise SystemExit(1)

        print()
        print("=" * 80)
        print("DASTABASE - GVIN BATCH MATCH TEST")
        print("=" * 80)
        print("Input:", input_file)
        print("Rows:", len(df))
        print("Initial GVIN URL:", page.url)
        print()

        for index, row in df.iterrows():
            company_input = build_company_input(
                row,
                (person_col, email_col, phone_col, company_col),
            )

            original_row = row.to_dict()

            if not company_input.name:
                result = empty_gvin_result("NO_MATCH", "Missing company name")
                output_rows.append({**original_row, **result})
                summary["NO_MATCH"] += 1
                continue

            summary["processed"] += 1

            print("-" * 80)
            print(f"{index + 1}. {company_input.name}")

            try:
                page.goto(
                    make_search_url(company_input.name),
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                human_pause(page)

                problem = detect_gvin_problem(page)

                if problem:
                    print("GVIN problem:", problem)
                    result = empty_gvin_result("ERROR", problem)
                    output_rows.append({**original_row, **result})
                    summary["ERROR"] += 1

                    if "rate-limit" in problem:
                        summary["rate_limit_issues"] += 1
                        break

                    page.wait_for_timeout(random.randint(8000, 12000))
                    continue

                companies = collect_page(page)

                if not companies:
                    print("No candidates")
                    result = empty_gvin_result("NO_MATCH", "No GVIN candidates")
                    output_rows.append({**original_row, **result})
                    summary["NO_MATCH"] += 1
                    page.wait_for_timeout(random.randint(4000, 7000))
                    continue

                candidates = [
                    candidate_from_company(company)
                    for company in companies
                ]
                match = best_match(company_input, candidates)

                if match is None:
                    print("No match result")
                    result = empty_gvin_result("NO_MATCH", "No match result")
                    output_rows.append({**original_row, **result})
                    summary["NO_MATCH"] += 1
                    page.wait_for_timeout(random.randint(4000, 7000))
                    continue

                result = result_from_match(match)
                output_rows.append({**original_row, **result})

                confidence = result["Match Confidence"]
                summary[confidence] += 1

                if result["GVIN Registration"]:
                    summary["registration_found"] += 1

                if result["GVIN Tax"]:
                    summary["tax_found"] += 1

                print("Best:", result["GVIN Company"])
                print("Score:", result["Match Score"])
                print("Confidence:", confidence)
                print("Reasons:", result["Match Reasons"])
                print("Company ID:", result["GVIN Company ID"])
                print("Registration:", result["GVIN Registration"])
                print("Tax:", result["GVIN Tax"])
                print("Domain:", result["GVIN Domain"])
                print("Email:", result["GVIN Email"])

                page.wait_for_timeout(random.randint(5000, 9000))

            except TimeoutError as exc:
                error = f"Timeout while processing company: {exc}"
                print("ERROR:", error)
                result = empty_gvin_result("ERROR", error)
                output_rows.append({**original_row, **result})
                summary["ERROR"] += 1
                page.wait_for_timeout(random.randint(8000, 12000))

            except Exception as exc:
                error = f"Error while processing company: {exc}"
                print("ERROR:", error)
                result = empty_gvin_result("ERROR", error)
                output_rows.append({**original_row, **result})
                summary["ERROR"] += 1
                page.wait_for_timeout(random.randint(8000, 12000))

    output_df = pd.DataFrame(output_rows)
    output_df.to_excel(OUTPUT_FILE, index=False)

    print_summary(summary)


if __name__ == "__main__":
    main()
