from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from database_v2 import (
    DB_PATH,
    get_connection,
    initialize_database,
    json_dumps,
    normalize_registration_number,
    normalize_tax_number,
    raw_hash,
    utc_now,
)
from ingestion.models import CompanyRecord, FactRecord
from ingestion.writer import ingest_company


SOURCE = "excel_gvin_enrichment"
JOB_TYPE = "excel_enriched_import"


class ImportErrorV2(Exception):
    pass


def usage() -> None:
    print('Usage: .venv/bin/python import_enriched_v2.py "enriched.xlsx"')


def clean_value(value: Any) -> Any:
    if pd.isna(value):
        return None

    if hasattr(value, "item"):
        return value.item()

    return value


def cell_text(value: Any) -> str:
    value = clean_value(value)

    if value is None:
        return ""

    text = str(value).strip()

    if text.endswith(".0"):
        text = text[:-2]

    return text


def cell_number(value: Any) -> float | None:
    value = clean_value(value)

    if value is None or value == "":
        return None

    if isinstance(value, str):
        value = value.strip().replace(".", "").replace(",", ".")

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def row_payload(row: pd.Series) -> dict[str, Any]:
    return {str(key): clean_value(value) for key, value in row.to_dict().items()}


def create_job(conn: sqlite3.Connection, input_name: str, total_items: int) -> int:
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO jobs(
            job_type,
            source,
            input_name,
            status,
            started_at,
            total_items,
            config_json,
            created_at
        )
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            JOB_TYPE,
            SOURCE,
            input_name,
            "RUNNING",
            now,
            total_items,
            json_dumps({"importer": "import_enriched_v2.py"}),
            now,
        ),
    )
    return int(cursor.lastrowid)


def finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    processed_items: int,
    error_count: int,
) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET status = ?,
            finished_at = ?,
            processed_items = ?,
            error_count = ?
        WHERE job_id = ?
        """,
        (status, utc_now(), processed_items, error_count, job_id),
    )


def upsert_source_record(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    company_id: int | None,
    source_file: str,
    source_row: int,
    external_id: str | None,
    raw: dict[str, Any],
    collected_at: str,
) -> tuple[int, bool]:
    payload_hash = raw_hash(raw)

    existing = conn.execute(
        """
        SELECT source_record_id, company_id
        FROM source_records
        WHERE source = ?
          AND source_file = ?
          AND source_row = ?
          AND raw_hash = ?
        """,
        (SOURCE, source_file, source_row, payload_hash),
    ).fetchone()

    if existing is not None:
        if company_id is not None and existing["company_id"] is None:
            conn.execute(
                """
                UPDATE source_records
                SET company_id = ?
                WHERE source_record_id = ?
                """,
                (company_id, existing["source_record_id"]),
            )

        return int(existing["source_record_id"]), False

    cursor = conn.execute(
        """
        INSERT INTO source_records(
            job_id,
            company_id,
            source,
            source_file,
            source_row,
            external_id,
            raw_json,
            raw_hash,
            collected_at
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            job_id,
            company_id,
            SOURCE,
            source_file,
            source_row,
            external_id,
            json_dumps(raw),
            payload_hash,
            collected_at,
        ),
    )
    return int(cursor.lastrowid), True


def update_source_record_company(
    conn: sqlite3.Connection,
    *,
    source_record_id: int,
    company_id: int,
) -> None:
    conn.execute(
        """
        UPDATE source_records
        SET company_id = ?
        WHERE source_record_id = ?
        """,
        (company_id, source_record_id),
    )


def find_existing_company_id(
    conn: sqlite3.Connection,
    *,
    registration_number: str | None,
    tax_number: str | None,
) -> int | None:
    ids = set()

    if registration_number:
        row = conn.execute(
            """
            SELECT company_id
            FROM companies
            WHERE registration_number = ?
            """,
            (registration_number,),
        ).fetchone()

        if row is not None:
            ids.add(int(row["company_id"]))

    if tax_number:
        row = conn.execute(
            """
            SELECT company_id
            FROM companies
            WHERE tax_number = ?
            """,
            (tax_number,),
        ).fetchone()

        if row is not None:
            ids.add(int(row["company_id"]))

    if len(ids) > 1:
        raise ImportErrorV2("registration_number and tax_number point to different companies")

    return next(iter(ids), None)


def create_job_item(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    company_id: int | None,
    item_key: str,
    source_row: int,
    raw: dict[str, Any],
    status: str,
    match_status: str,
    error_type: str | None = None,
    error_message: str | None = None,
    output: dict[str, Any] | None = None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO job_items(
            job_id,
            company_id,
            item_key,
            input_row_number,
            input_payload_json,
            status,
            match_status,
            error_type,
            error_message,
            started_at,
            finished_at,
            attempt_count,
            output_payload_json
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            job_id,
            company_id,
            item_key,
            source_row,
            json_dumps(raw),
            status,
            match_status,
            error_type,
            error_message,
            now,
            now,
            1,
            json_dumps(output or {}),
        ),
    )


def import_row(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    source_file: str,
    row_index: int,
    row: pd.Series,
    collected_at: str,
    db_path: Path | str = DB_PATH,
) -> dict[str, Any]:
    source_row = row_index + 2
    raw = row_payload(row)
    match_status = cell_text(row.get("Match Status")).upper()
    match_reasons = cell_text(row.get("Match Reasons"))
    gvin_company = cell_text(row.get("GVIN Company"))
    gvin_company_id = cell_text(row.get("GVIN Company ID"))
    registration = normalize_registration_number(row.get("GVIN Registration"))
    tax = normalize_tax_number(row.get("GVIN Tax"))
    item_key = f"row:{source_row}"
    company_id: int | None = None
    status = "imported/unresolved"
    error_type = None
    error_message = None

    try:
        existing_source_record = conn.execute(
            """
            SELECT source_record_id, company_id
            FROM source_records
            WHERE source = ?
              AND source_file = ?
              AND source_row = ?
              AND raw_hash = ?
            """,
            (SOURCE, source_file, source_row, raw_hash(raw)),
        ).fetchone()

        if existing_source_record is not None:
            company_id = existing_source_record["company_id"]
            status = "skipped duplicate"
            source_record_id = int(existing_source_record["source_record_id"])
        else:
            source_record_id, _created = upsert_source_record(
                conn,
                job_id=job_id,
                company_id=None,
                source_file=source_file,
                source_row=source_row,
                external_id=gvin_company_id or None,
                raw=raw,
                collected_at=collected_at,
            )
            conn.commit()

            if match_status == "CONFIRMED":
                should_ingest = bool(registration or tax)
                if not should_ingest:
                    status = "imported/unresolved"
                    error_type = "UNRESOLVED_IDENTITY"
                    error_message = "CONFIRMED row has no registration_number or tax_number"

            elif match_status == "REVIEW":
                should_ingest = (
                    find_existing_company_id(
                        conn,
                        registration_number=registration,
                        tax_number=tax,
                    )
                    is not None
                )
                if not should_ingest:
                    status = "imported/unresolved"
                    error_type = "REVIEW_HELD"
                    error_message = "REVIEW match held from canonical creation"

            elif match_status == "ERROR":
                should_ingest = False
                status = "error"
                error_type = "SOURCE_ROW_ERROR"
                error_message = match_reasons or "Excel row has Match Status ERROR"

            elif match_status == "NO_MATCH":
                should_ingest = False
                status = "imported/unresolved"
                error_type = "NO_MATCH"
                error_message = match_reasons or "Excel row has Match Status NO_MATCH"

            else:
                should_ingest = False
                status = "imported/unresolved"
                error_type = "UNKNOWN_MATCH_STATUS"
                error_message = f"Unknown Match Status: {match_status}"

            if should_ingest:
                facts = []
                for fact_key, column_name in (
                    ("revenue", "GVIN Revenue 2025"),
                    ("employees", "GVIN Employees 2025"),
                ):
                    value = cell_number(row.get(column_name))
                    if value is not None:
                        facts.append(
                            FactRecord(
                                fact_key=fact_key,
                                value=value,
                                value_year=2025,
                                observed_at=collected_at,
                            )
                        )

                ingest_result = ingest_company(
                    CompanyRecord(
                        source="gvin",
                        external_id=gvin_company_id or None,
                        external_url=(
                            "https://www.gvin.com/GvinOverview/Pages/"
                            f"Company.aspx?CompanyId={gvin_company_id}"
                            if gvin_company_id
                            else None
                        ),
                        canonical_name=gvin_company or None,
                        registration_number=registration,
                        tax_number=tax,
                        address=cell_text(row.get("GVIN Address")) or None,
                        website=cell_text(row.get("GVIN Domain")) or None,
                        email=cell_text(row.get("GVIN Email")) or None,
                        observed_at=collected_at,
                        verified_at=collected_at,
                        source_record_id=source_record_id,
                        job_id=job_id,
                        facts=facts,
                    ),
                    db_path=db_path,
                )

                company_id = ingest_result.company_id

                if ingest_result.status in ("CREATED", "RESOLVED"):
                    if company_id is not None:
                        update_source_record_company(
                            conn,
                            source_record_id=source_record_id,
                            company_id=company_id,
                        )
                    status = "imported/resolved"
                elif ingest_result.status == "IDENTITY_CONFLICT":
                    status = "error"
                    error_type = "IDENTITY_CONFLICT"
                    error_message = "; ".join(ingest_result.conflicts)
                else:
                    status = "imported/unresolved"
                    error_type = ingest_result.status
                    error_message = "; ".join(ingest_result.warnings)

    except ImportErrorV2 as exc:
        source_record_id, _created = upsert_source_record(
            conn,
            job_id=job_id,
            company_id=None,
            source_file=source_file,
            source_row=source_row,
            external_id=gvin_company_id or None,
            raw=raw,
            collected_at=collected_at,
        )
        status = "error"
        error_type = "IMPORT_ERROR"
        error_message = str(exc)

    output = {
        "source_record_id": source_record_id,
        "company_id": company_id,
        "registration_number": registration,
        "tax_number": tax,
    }
    create_job_item(
        conn,
        job_id=job_id,
        company_id=company_id,
        item_key=item_key,
        source_row=source_row,
        raw=raw,
        status=status,
        match_status=match_status,
        error_type=error_type,
        error_message=error_message,
        output=output,
    )

    return {
        "status": status,
        "match_status": match_status,
        "company_id": company_id,
        "source_record_id": source_record_id,
    }


def import_file(path: Path, *, db_path: Path | str = DB_PATH) -> dict[str, int]:
    initialize_database(db_path)
    df = pd.read_excel(path)
    collected_at = utc_now()
    stats = {
        "rows": len(df),
        "resolved": 0,
        "unresolved": 0,
        "duplicates": 0,
        "errors": 0,
    }

    with get_connection(db_path) as conn:
        job_id = create_job(conn, path.name, len(df))
        conn.commit()

        try:
            for index, row in df.iterrows():
                result = import_row(
                    conn,
                    job_id=job_id,
                    source_file=path.name,
                    row_index=index,
                    row=row,
                    collected_at=collected_at,
                    db_path=db_path,
                )

                status = result["status"]

                if status == "imported/resolved":
                    stats["resolved"] += 1
                elif status == "skipped duplicate":
                    stats["duplicates"] += 1
                elif status == "error":
                    stats["errors"] += 1
                else:
                    stats["unresolved"] += 1

                conn.commit()

            finish_job(
                conn,
                job_id,
                status="FINISHED",
                processed_items=len(df),
                error_count=stats["errors"],
            )
            conn.commit()
        except Exception:
            finish_job(
                conn,
                job_id,
                status="FAILED",
                processed_items=sum(
                    stats[key]
                    for key in ("resolved", "unresolved", "duplicates", "errors")
                ),
                error_count=stats["errors"] + 1,
            )
            conn.commit()
            raise

        stats["job_id"] = job_id

    return stats


def main() -> int:
    if len(sys.argv) != 2:
        usage()
        return 2

    path = Path(sys.argv[1]).expanduser().resolve()

    if not path.exists():
        print(f"ERROR: input Excel not found: {path}")
        return 1

    stats = import_file(path)

    print("Import finished")
    print("job_id:", stats["job_id"])
    print("rows:", stats["rows"])
    print("resolved:", stats["resolved"])
    print("unresolved:", stats["unresolved"])
    print("duplicates:", stats["duplicates"])
    print("errors:", stats["errors"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
