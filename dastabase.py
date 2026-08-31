from __future__ import annotations

import random
import sys
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from playwright.sync_api import TimeoutError, sync_playwright

from collector_lite import collect_page, find_gvin_page, human_pause
from database_v2 import (
    DB_PATH as DB_V2_PATH,
    create_job as create_v2_job,
    create_job_item as create_v2_job_item,
    find_company_id_by_identifiers,
    finish_job as finish_v2_job,
    initialize_database as initialize_v2_database,
    normalize_registration_number,
    normalize_tax_number,
    update_source_record_company as update_v2_source_record_company,
    upsert_source_record as upsert_v2_source_record,
    utc_now,
)
from ingestion.models import CompanyRecord, FactRecord
from ingestion.writer import ingest_company
from matching.company_matcher import CompanyCandidate, CompanyInput, best_match


CDP_URL = "http://127.0.0.1:9222"
SEARCH_URL = (
    "https://www.gvin.com/IskalnikCE/Pages/SearchResult.aspx"
    "?Mode=GvinSI&App=GvinIskalnikSI&Kontekst=1"
    "&QueryVsebina={query}&Lang=sl-SI"
)

GVIN_COLUMNS = [
    "GVIN Company",
    "GVIN Company ID",
    "GVIN Registration",
    "GVIN Tax",
    "GVIN Address",
    "GVIN Domain",
    "GVIN Email",
    "GVIN Revenue 2025",
    "GVIN Employees 2025",
]

MATCH_COLUMNS = [
    "Match Score",
    "Match Confidence",
    "Match Status",
    "Match Reasons",
]

DONE_STATUSES = {"CONFIRMED", "REVIEW", "NO_MATCH"}
RATE_LIMIT_MARKERS = [
    "rate limit",
    "too many requests",
    "prevec zahtev",
    "preveč zahtev",
    "poskusite kasneje",
    "temporarily unavailable",
]
V2_JOB_TYPE = "excel_gvin_enrichment"
V2_SOURCE = "gvin"


def usage() -> None:
    print('Usage: .venv/bin/python dastabase.py "input.xlsx"')


def output_path_for(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_enriched.xlsx")


def cell_text(value) -> str:
    if pd.isna(value):
        return ""

    text = str(value).strip()

    if text.endswith(".0"):
        text = text[:-2]

    return text


def find_column(columns, candidates):
    normalized = {
        str(column).strip().lower(): column
        for column in columns
    }

    for candidate in candidates:
        key = candidate.strip().lower()

        if key in normalized:
            return normalized[key]

    return None


def make_search_url(company_name: str) -> str:
    return SEARCH_URL.format(query=quote_plus(company_name))


def detect_gvin_problem(page) -> str:
    try:
        body = page.locator("body").inner_text(timeout=5000).lower()
    except Exception as exc:
        return f"Could not read page body: {exc}"

    for marker in RATE_LIMIT_MARKERS:
        if marker in body:
            return f"GVIN rate-limit/error marker found: {marker}"

    url = page.url.lower()

    if "authenticate" in url or "accounts.bisnode" in url:
        return f"GVIN redirected to login/authentication: {page.url}"

    if "gvin.com" not in url or "searchresult.aspx" not in url:
        return f"Unexpected GVIN page after search: {page.url}"

    return ""


def company_input_from_row(row, columns) -> CompanyInput:
    person_col, email_col, phone_col, company_col = columns

    return CompanyInput(
        name=cell_text(row.get(company_col, "")),
        email="" if email_col is None else cell_text(row.get(email_col, "")),
        phone="" if phone_col is None else cell_text(row.get(phone_col, "")),
        person_name="" if person_col is None else cell_text(row.get(person_col, "")),
    )


def candidate_from_company(company: dict) -> CompanyCandidate:
    return CompanyCandidate(
        name=company.get("company_name", ""),
        domain=company.get("domain", ""),
        email_domain=company.get("email_domain", ""),
        registration_number=cell_text(company.get("registration_number", "")),
        tax_number=cell_text(company.get("tax_number", "")),
        address=company.get("address", ""),
        raw=company,
    )


def blank_result(status: str, reason: str = "") -> dict:
    return {
        "GVIN Company": "",
        "GVIN Company ID": "",
        "GVIN Registration": "",
        "GVIN Tax": "",
        "GVIN Address": "",
        "GVIN Domain": "",
        "GVIN Email": "",
        "GVIN Revenue 2025": None,
        "GVIN Employees 2025": None,
        "Match Score": None,
        "Match Confidence": "",
        "Match Status": status,
        "Match Reasons": reason,
    }


def status_from_confidence(confidence: str) -> str:
    if confidence == "HIGH":
        return "CONFIRMED"

    if confidence in {"MEDIUM", "LOW"}:
        return "REVIEW"

    return "NO_MATCH"


def result_from_match(match) -> dict:
    status = status_from_confidence(match.confidence)

    if status == "NO_MATCH":
        result = blank_result("NO_MATCH", ", ".join(match.reasons))
        result["Match Score"] = match.score
        result["Match Confidence"] = match.confidence
        return result

    candidate = match.candidate
    raw = candidate.raw

    return {
        "GVIN Company": candidate.name,
        "GVIN Company ID": cell_text(raw.get("gvin_company_id", "")),
        "GVIN Registration": cell_text(candidate.registration_number),
        "GVIN Tax": cell_text(candidate.tax_number),
        "GVIN Address": candidate.address,
        "GVIN Domain": candidate.domain,
        "GVIN Email": raw.get("email", ""),
        "GVIN Revenue 2025": raw.get("revenue_2025"),
        "GVIN Employees 2025": raw.get("employees_2025"),
        "Match Score": match.score,
        "Match Confidence": match.confidence,
        "Match Status": status,
        "Match Reasons": ", ".join(match.reasons),
    }


def load_or_create_output(input_df: pd.DataFrame, output_path: Path) -> pd.DataFrame:
    output_columns = list(input_df.columns) + GVIN_COLUMNS + MATCH_COLUMNS

    if not output_path.exists():
        output_df = input_df.copy()
    else:
        output_df = pd.read_excel(
            output_path,
            dtype={
                "GVIN Company ID": str,
                "GVIN Registration": str,
                "GVIN Tax": str,
            },
        )

        if len(output_df) != len(input_df):
            print(
                "Existing output row count differs from input. "
                "Starting a fresh output file.",
                flush=True,
            )
            output_df = input_df.copy()
        else:
            for column in input_df.columns:
                output_df[column] = input_df[column]

    for column in output_columns:
        if column not in output_df.columns:
            output_df[column] = None

    return output_df[output_columns]


def save_checkpoint(output_df: pd.DataFrame, output_path: Path) -> None:
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        output_df.to_excel(writer, index=False)
        worksheet = writer.sheets["Sheet1"]
        header = [cell.value for cell in worksheet[1]]

        for column_name in [
            "GVIN Company ID",
            "GVIN Registration",
            "GVIN Tax",
        ]:
            if column_name not in header:
                continue

            column_index = header.index(column_name) + 1

            for row in range(2, worksheet.max_row + 1):
                cell = worksheet.cell(row=row, column=column_index)
                cell.number_format = "@"


def print_summary(output_df: pd.DataFrame, processed_now: int, output_path: Path) -> None:
    status_counts = output_df["Match Status"].fillna("").value_counts()

    def nonempty_count(column_name: str) -> int:
        values = output_df[column_name].fillna("").astype(str).str.strip()
        return int((values != "").sum())

    print()
    print("=" * 80)
    print("DASTABASE V1 SUMMARY")
    print("=" * 80)
    print("Processed this run:", processed_now)
    print("Rows in output:", len(output_df))
    print("CONFIRMED:", int(status_counts.get("CONFIRMED", 0)))
    print("REVIEW:", int(status_counts.get("REVIEW", 0)))
    print("NO_MATCH:", int(status_counts.get("NO_MATCH", 0)))
    print("ERROR:", int(status_counts.get("ERROR", 0)))
    print("GVIN registration found:", nonempty_count("GVIN Registration"))
    print("GVIN tax found:", nonempty_count("GVIN Tax"))
    print("GVIN revenue 2025 found:", nonempty_count("GVIN Revenue 2025"))
    print("GVIN employees 2025 found:", nonempty_count("GVIN Employees 2025"))
    print("Output:", output_path)


def clean_v2_value(value):
    if pd.isna(value):
        return None

    if hasattr(value, "item"):
        value = value.item()

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return value


def row_payload(row) -> dict:
    return {
        str(key): clean_v2_value(value)
        for key, value in row.to_dict().items()
    }


def cell_number(value) -> float | None:
    if pd.isna(value) or value == "":
        return None

    if isinstance(value, str):
        value = value.strip().replace(".", "").replace(",", ".")

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def gvin_external_url(gvin_company_id: str) -> str | None:
    if not gvin_company_id:
        return None

    return (
        "https://www.gvin.com/GvinOverview/Pages/"
        f"Company.aspx?CompanyId={gvin_company_id}"
    )


def combined_v2_raw(row, result: dict) -> dict:
    return {
        "input": row_payload(row),
        "enrichment": {
            key: clean_v2_value(value)
            for key, value in result.items()
        },
    }


class V2EnrichmentRecorder:
    def __init__(self, input_path: Path, total_rows: int):
        self.input_path = input_path
        self.total_rows = total_rows
        self.job_id: int | None = None
        self.enabled = False
        self.disabled_reason = ""
        self.processed_items = 0
        self.error_count = 0

        try:
            initialize_v2_database(DB_V2_PATH)
            self.job_id = create_v2_job(
                job_type=V2_JOB_TYPE,
                source=V2_SOURCE,
                input_name=input_path.name,
                total_items=total_rows,
                config={
                    "pipeline": "dastabase.py",
                    "mode": "live_enrichment",
                    "v2_optional": True,
                },
                db_path=DB_V2_PATH,
            )
            self.enabled = True
            print(f"V2 job: {self.job_id}", flush=True)
        except Exception as exc:
            self.disabled_reason = str(exc)
            print(
                "WARNING: V2 ingestion disabled for this run: "
                f"{self.disabled_reason}",
                flush=True,
            )

    def record_resume_skip(self, *, row_number: int, row, result: dict) -> None:
        if not self.enabled or self.job_id is None:
            return

        try:
            create_v2_job_item(
                job_id=self.job_id,
                company_id=None,
                item_key=f"row:{row_number}",
                input_row_number=row_number,
                input_payload=row_payload(row),
                status="resume_skipped",
                match_status=cell_text(result.get("Match Status")),
                output_payload={"v1_result": result, "v2_ingestion": "not_run"},
                db_path=DB_V2_PATH,
            )
            self.processed_items += 1
        except Exception as exc:
            self._disable_after_system_error(exc)

    def record_row(self, *, row_number: int, row, result: dict) -> None:
        if not self.enabled or self.job_id is None:
            return

        match_status = cell_text(result.get("Match Status")).upper()
        gvin_company_id = cell_text(result.get("GVIN Company ID"))
        registration = normalize_registration_number(result.get("GVIN Registration"))
        tax = normalize_tax_number(result.get("GVIN Tax"))
        raw = combined_v2_raw(row, result)
        company_id: int | None = None
        source_record_id: int | None = None
        item_status = "v2/unresolved"
        error_type = None
        error_message = None
        output_payload = {"v1_result": result}

        try:
            source_record = upsert_v2_source_record(
                job_id=self.job_id,
                company_id=None,
                source=V2_SOURCE,
                source_file=self.input_path.name,
                source_row=row_number,
                external_id=gvin_company_id or None,
                raw=raw,
                collected_at=utc_now(),
                db_path=DB_V2_PATH,
            )
            source_record_id = source_record.source_record_id
            company_id = source_record.company_id

            should_ingest = False
            if match_status == "CONFIRMED":
                should_ingest = bool(registration or tax)
                if not should_ingest:
                    error_type = "UNRESOLVED_IDENTITY"
                    error_message = "CONFIRMED row has no registration_number or tax_number"
            elif match_status == "REVIEW":
                existing_company_id = find_company_id_by_identifiers(
                    registration_number=registration,
                    tax_number=tax,
                    db_path=DB_V2_PATH,
                )
                should_ingest = existing_company_id is not None
                if not should_ingest:
                    error_type = "REVIEW_HELD"
                    error_message = "REVIEW match held from canonical creation"
            elif match_status == "ERROR":
                item_status = "v2/error"
                error_type = "SOURCE_ROW_ERROR"
                error_message = cell_text(result.get("Match Reasons")) or "V1 row status ERROR"
            elif match_status == "NO_MATCH":
                error_type = "NO_MATCH"
                error_message = cell_text(result.get("Match Reasons")) or "V1 row status NO_MATCH"
            else:
                error_type = "UNKNOWN_MATCH_STATUS"
                error_message = f"Unknown Match Status: {match_status}"

            if should_ingest:
                facts = []
                for fact_key, result_key in (
                    ("revenue", "GVIN Revenue 2025"),
                    ("employees", "GVIN Employees 2025"),
                ):
                    value = cell_number(result.get(result_key))
                    if value is not None:
                        facts.append(
                            FactRecord(
                                fact_key=fact_key,
                                value=value,
                                value_year=2025,
                            )
                        )

                ingest_result = ingest_company(
                    CompanyRecord(
                        source=V2_SOURCE,
                        external_id=gvin_company_id or None,
                        external_url=gvin_external_url(gvin_company_id),
                        canonical_name=cell_text(result.get("GVIN Company")) or None,
                        registration_number=registration,
                        tax_number=tax,
                        address=cell_text(result.get("GVIN Address")) or None,
                        website=cell_text(result.get("GVIN Domain")) or None,
                        email=cell_text(result.get("GVIN Email")) or None,
                        observed_at=utc_now(),
                        verified_at=utc_now(),
                        source_record_id=source_record_id,
                        job_id=self.job_id,
                        facts=facts,
                    ),
                    db_path=DB_V2_PATH,
                )
                company_id = ingest_result.company_id
                output_payload["v2_ingestion"] = {
                    "status": ingest_result.status,
                    "facts_written": ingest_result.facts_written,
                    "warnings": list(ingest_result.warnings),
                    "conflicts": list(ingest_result.conflicts),
                }

                if ingest_result.status in {"CREATED", "RESOLVED"}:
                    item_status = "v2/resolved"
                    if company_id is not None:
                        update_v2_source_record_company(
                            source_record_id=source_record_id,
                            company_id=company_id,
                            db_path=DB_V2_PATH,
                        )
                elif ingest_result.status == "IDENTITY_CONFLICT":
                    item_status = "v2/error"
                    error_type = "IDENTITY_CONFLICT"
                    error_message = "; ".join(ingest_result.conflicts)
                    self.error_count += 1
                else:
                    item_status = "v2/unresolved"
                    error_type = ingest_result.status
                    error_message = "; ".join(ingest_result.warnings)
            else:
                output_payload["v2_ingestion"] = "not_ingested"
                if item_status == "v2/error":
                    self.error_count += 1

            create_v2_job_item(
                job_id=self.job_id,
                company_id=company_id,
                item_key=f"row:{row_number}",
                input_row_number=row_number,
                input_payload=row_payload(row),
                status=item_status,
                match_status=match_status,
                error_type=error_type,
                error_message=error_message,
                output_payload={
                    **output_payload,
                    "source_record_id": source_record_id,
                    "company_id": company_id,
                },
                db_path=DB_V2_PATH,
            )
            self.processed_items += 1
        except Exception as exc:
            self.error_count += 1
            print(
                f"WARNING: V2 ingestion failed for row {row_number}: {exc}",
                flush=True,
            )
            try:
                if self.enabled and self.job_id is not None:
                    create_v2_job_item(
                        job_id=self.job_id,
                        company_id=company_id,
                        item_key=f"row:{row_number}",
                        input_row_number=row_number,
                        input_payload=row_payload(row),
                        status="v2/error",
                        match_status=match_status,
                        error_type="V2_WRITE_ERROR",
                        error_message=str(exc),
                        output_payload={
                            **output_payload,
                            "source_record_id": source_record_id,
                            "company_id": company_id,
                        },
                        db_path=DB_V2_PATH,
                    )
                    self.processed_items += 1
            except Exception as job_item_exc:
                self._disable_after_system_error(job_item_exc)

    def finish(self, status: str = "FINISHED") -> None:
        if not self.enabled or self.job_id is None:
            return

        try:
            finish_v2_job(
                job_id=self.job_id,
                status=status,
                processed_items=self.processed_items,
                error_count=self.error_count,
                db_path=DB_V2_PATH,
            )
        except Exception as exc:
            print(f"WARNING: could not finish V2 job {self.job_id}: {exc}", flush=True)

    def _disable_after_system_error(self, exc: Exception) -> None:
        self.enabled = False
        self.disabled_reason = str(exc)
        print(
            "WARNING: V2 ingestion disabled after write failure: "
            f"{self.disabled_reason}",
            flush=True,
        )


def conservative_delay(page, row_number: int) -> None:
    if row_number > 0 and row_number % 20 == 0:
        page.wait_for_timeout(random.randint(30000, 60000))
    else:
        page.wait_for_timeout(random.randint(5000, 9000))


def main() -> int:
    if len(sys.argv) != 2:
        usage()
        return 2

    input_path = Path(sys.argv[1]).expanduser().resolve()

    if not input_path.exists():
        print(f"ERROR: input Excel not found: {input_path}")
        return 1

    output_path = output_path_for(input_path)
    input_df = pd.read_excel(input_path)

    person_col = find_column(input_df.columns, ["Ime", "ime", "Oseba", "Kontakt"])
    email_col = find_column(input_df.columns, ["Email", "E-mail", "E-pošta"])
    phone_col = find_column(input_df.columns, ["Telefon", "Phone", "Tel"])
    company_col = find_column(input_df.columns, ["Podjetje", "Company", "Firma"])

    if company_col is None:
        print(
            "ERROR: could not find company column. "
            f"Available columns: {list(input_df.columns)}"
        )
        return 1

    output_df = load_or_create_output(input_df, output_path)
    save_checkpoint(output_df, output_path)

    processed_now = 0
    rate_limit_issues = 0

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as exc:
            print(f"ERROR: could not connect to Chrome/CDP at {CDP_URL}: {exc}")
            return 1

        if not browser.contexts:
            print("ERROR: no Chrome context found.")
            return 1

        page = find_gvin_page(browser.contexts[0])

        if page is None:
            print("ERROR: GVIN page not found in open Chrome.")
            return 1

        v2_recorder = V2EnrichmentRecorder(input_path, len(input_df))

        print()
        print("=" * 80)
        print("DASTABASE V1 - EXCEL -> GVIN -> MATCH -> EXCEL")
        print("=" * 80)
        print("Input:", input_path)
        print("Output:", output_path)
        print("Rows:", len(input_df))
        print()

        columns = (person_col, email_col, phone_col, company_col)

        for index, row in input_df.iterrows():
            row_number = index + 1
            existing_status = cell_text(output_df.at[index, "Match Status"])
            company_input = company_input_from_row(row, columns)

            if existing_status in DONE_STATUSES:
                print(
                    f"[{row_number}/{len(input_df)}] {company_input.name} "
                    f"- resume skip ({existing_status})",
                    flush=True,
                )
                v2_recorder.record_resume_skip(
                    row_number=row_number,
                    row=row,
                    result={
                        key: output_df.at[index, key]
                        for key in GVIN_COLUMNS + MATCH_COLUMNS
                    },
                )
                continue

            print(f"[{row_number}/{len(input_df)}] {company_input.name}", flush=True)

            if not company_input.name:
                result = blank_result("NO_MATCH", "Missing company name")
                for key, value in result.items():
                    output_df.at[index, key] = value
                save_checkpoint(output_df, output_path)
                v2_recorder.record_row(
                    row_number=row_number,
                    row=row,
                    result=result,
                )
                print("GVIN candidates: 0", flush=True)
                print("Match: NO_MATCH", flush=True)
                print("✓ saved", flush=True)
                continue

            try:
                page.goto(
                    make_search_url(company_input.name),
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                human_pause(page)

                problem = detect_gvin_problem(page)

                if problem:
                    result = blank_result("ERROR", problem)
                    for key, value in result.items():
                        output_df.at[index, key] = value
                    save_checkpoint(output_df, output_path)
                    v2_recorder.record_row(
                        row_number=row_number,
                        row=row,
                        result=result,
                    )
                    processed_now += 1
                    print("GVIN candidates: 0", flush=True)
                    print(f"Match: ERROR - {problem}", flush=True)
                    print("✓ saved", flush=True)

                    if "rate-limit" in problem:
                        rate_limit_issues += 1
                        print("Stopping because GVIN rate-limit was detected.", flush=True)
                        break

                    page.wait_for_timeout(random.randint(10000, 15000))
                    continue

                companies = collect_page(page)
                print(f"GVIN candidates: {len(companies)}", flush=True)

                if not companies:
                    result = blank_result("NO_MATCH", "No GVIN candidates")
                else:
                    candidates = [
                        candidate_from_company(company)
                        for company in companies
                    ]
                    match = best_match(company_input, candidates)
                    result = (
                        result_from_match(match)
                        if match is not None
                        else blank_result("NO_MATCH", "No match result")
                    )

                for key, value in result.items():
                    output_df.at[index, key] = value

                save_checkpoint(output_df, output_path)
                v2_recorder.record_row(
                    row_number=row_number,
                    row=row,
                    result=result,
                )
                processed_now += 1

                match_label = result["Match Status"]
                confidence = result["Match Confidence"] or match_label
                score = result["Match Score"]
                score_text = "" if pd.isna(score) else f" / {score:g}"

                print(f"Match: {confidence}{score_text}", flush=True)
                print(f"Status: {match_label}", flush=True)
                print(f"Registration: {result['GVIN Registration']}", flush=True)
                print(f"Tax: {result['GVIN Tax']}", flush=True)
                print("✓ saved", flush=True)

                conservative_delay(page, row_number)

            except TimeoutError as exc:
                result = blank_result("ERROR", f"Timeout: {exc}")
                for key, value in result.items():
                    output_df.at[index, key] = value
                save_checkpoint(output_df, output_path)
                v2_recorder.record_row(
                    row_number=row_number,
                    row=row,
                    result=result,
                )
                processed_now += 1
                print("GVIN candidates: 0", flush=True)
                print(f"Match: ERROR - Timeout: {exc}", flush=True)
                print("✓ saved", flush=True)
                page.wait_for_timeout(random.randint(10000, 15000))

            except Exception as exc:
                result = blank_result("ERROR", f"Error: {exc}")
                for key, value in result.items():
                    output_df.at[index, key] = value
                save_checkpoint(output_df, output_path)
                v2_recorder.record_row(
                    row_number=row_number,
                    row=row,
                    result=result,
                )
                processed_now += 1
                print("GVIN candidates: 0", flush=True)
                print(f"Match: ERROR - {exc}", flush=True)
                print("✓ saved", flush=True)
                page.wait_for_timeout(random.randint(10000, 15000))

        v2_recorder.finish()

    print_summary(output_df, processed_now, output_path)
    print("GVIN rate-limit issues:", rate_limit_issues)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
