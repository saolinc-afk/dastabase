"""Integration test: Excel input -> simulated GVIN candidates -> matcher."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from test_excel import load_excel, prepare_companies
from matching.company_matcher import CompanyCandidate, match_company


EXCEL_FILE = "dastabase test_enricher.xlsx"
CANDIDATES_FILE = Path(__file__).with_name("test_candidates.json")


def load_candidates() -> list[CompanyCandidate]:
    data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    return [CompanyCandidate(**item) for item in data["companies"]]


def main() -> None:
    df = load_excel(EXCEL_FILE)
    companies = prepare_companies(df)
    candidates = load_candidates()

    print()
    print("=" * 80)
    print("DASTABASE — MATCHER INTEGRATION TEST")
    print("=" * 80)
    print()
    print(f"Input companies: {len(companies)}")
    print(f"Candidate companies: {len(candidates)}")
    print()

    for index, company in enumerate(companies, start=1):
        results = match_company(company, candidates, limit=3)

        print("-" * 80)
        print(f"{index}. INPUT: {company.name}")
        print(f"   Email:  {company.email}")
        print(f"   Phone:  {company.phone}")
        print()

        for result in results:
            print(
                f"   {result.score:6.1f}  {result.confidence:7s}  "
                f"{result.candidate.name}"
            )

            if result.reasons:
                print(f"            {', '.join(result.reasons)}")

        print()

    print("=" * 80)
    print("MATCHER INTEGRATION TEST COMPLETE")
    print("=" * 80)
    print()


if __name__ == "__main__":
    main()
