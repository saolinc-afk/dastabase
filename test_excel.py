"""
test_excel.py

Read the test Excel file and prepare company records for the matcher.

This is intentionally READ-ONLY:
- it does not change the Excel file
- it does not access GVIN
- it does not write to the database

Usage from the Dastabase root:

    python test_excel.py

Or with another Excel file:

    python test_excel.py "path/to/file.xlsx"
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from matching.company_matcher import (
    CompanyInput,
    extract_email_domain,
    normalize_company_name,
    normalize_phone,
)


DEFAULT_FILE = "dastabase test_enricher.xlsx"


def find_column(columns, candidates):
    """Find the first matching column, case-insensitively."""
    normalized = {
        str(column).strip().lower(): column
        for column in columns
    }

    for candidate in candidates:
        key = candidate.strip().lower()
        if key in normalized:
            return normalized[key]

    return None


def load_excel(file_path: str | Path) -> pd.DataFrame:
    """Load the first worksheet from an Excel file."""
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"Excel file not found: {path}")

    return pd.read_excel(path)


def prepare_companies(df: pd.DataFrame) -> list[CompanyInput]:
    """Convert Excel rows into CompanyInput records."""
    person_col = find_column(
        df.columns,
        ["Ime", "ime", "Oseba", "Kontakt"],
    )

    email_col = find_column(
        df.columns,
        ["Email", "E-mail", "E-pošta"],
    )

    phone_col = find_column(
        df.columns,
        ["Telefon", "Phone", "Tel"],
    )

    company_col = find_column(
        df.columns,
        ["Podjetje", "Company", "Firma"],
    )

    if company_col is None:
        raise ValueError(
            "Could not find the company column. "
            f"Available columns: {list(df.columns)}"
        )

    companies = []

    for _, row in df.iterrows():
        person = "" if person_col is None else str(row.get(person_col, "") or "")
        email = "" if email_col is None else str(row.get(email_col, "") or "")
        phone = "" if phone_col is None else str(row.get(phone_col, "") or "")
        company = str(row.get(company_col, "") or "")

        # Ignore completely empty rows.
        if not any([person.strip(), email.strip(), phone.strip(), company.strip()]):
            continue

        companies.append(
            CompanyInput(
                name=company.strip(),
                email=email.strip(),
                phone=phone.strip(),
                person_name=person.strip(),
            )
        )

    return companies


def print_company(index: int, company: CompanyInput) -> None:
    """Print one prepared company in a human-readable format."""
    print(f"{index}. {company.name or '[NO COMPANY NAME]'}")

    if company.person_name:
        print(f"   Kontakt:              {company.person_name}")

    print(f"   Email:                {company.email or '[none]'}")
    print(f"   Email domain:         {extract_email_domain(company.email) or '[none]'}")
    print(f"   Telefon:              {company.phone or '[none]'}")
    print(
        "   Telefon normalized:   "
        f"{normalize_phone(company.phone) or '[none]'}"
    )
    print(
        "   Ime normalized:       "
        f"{normalize_company_name(company.name) or '[none]'}"
    )
    print()


def main() -> None:
    file_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FILE

    print()
    print("=" * 70)
    print("DASTABASE — EXCEL INPUT TEST")
    print("=" * 70)
    print()
    print(f"File: {file_path}")
    print()

    try:
        df = load_excel(file_path)
    except Exception as exc:
        print(f"ERROR loading Excel: {exc}")
        sys.exit(1)

    print("Columns found:")
    for column in df.columns:
        print(f"  - {column}")

    print()
    print(f"Rows found: {len(df)}")
    print()

    try:
        companies = prepare_companies(df)
    except Exception as exc:
        print(f"ERROR preparing companies: {exc}")
        sys.exit(1)

    print(f"Companies prepared: {len(companies)}")
    print()
    print("-" * 70)
    print()

    for index, company in enumerate(companies, start=1):
        print_company(index, company)

    print("-" * 70)
    print()
    print("READ-ONLY TEST COMPLETE.")
    print("No Excel data was changed.")
    print("No GVIN lookup was performed.")
    print()


if __name__ == "__main__":
    main()
