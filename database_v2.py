from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DB_PATH = Path("database") / "database_v2.db"


class IdentityConflictError(Exception):
    def __init__(self, conflict_id: int, message: str):
        super().__init__(message)
        self.conflict_id = conflict_id


@dataclass(frozen=True)
class ResolveCompanyResult:
    company_id: int
    created: bool
    conflict_id: int | None = None


@dataclass(frozen=True)
class ResolvePersonResult:
    person_id: int
    created: bool


@dataclass(frozen=True)
class LinkPersonCompanyResult:
    company_person_id: int
    created: bool


@dataclass(frozen=True)
class CompanyPersonRoleResult:
    company_person_role_id: int
    created: bool


@dataclass(frozen=True)
class SourceRecordResult:
    source_record_id: int
    created: bool
    company_id: int | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def normalize_registration_number(value: Any) -> str | None:
    if value is None:
        return None

    digits = re.sub(r"\D+", "", str(value).strip())
    return digits or None


def normalize_tax_number(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    text = re.sub(r"^\s*si", "", text, flags=re.IGNORECASE)
    digits = re.sub(r"\D+", "", text)
    return digits or None


def normalize_person_name(value: Any) -> str | None:
    if value is None:
        return None

    normalized = re.sub(r"\s+", " ", str(value).strip().lower())
    return normalized or None


def normalize_email(value: Any) -> str | None:
    if value is None:
        return None

    normalized = str(value).strip().lower()
    return normalized or None


def normalize_linkedin_url(value: Any) -> str | None:
    if value is None:
        return None

    normalized = str(value).strip()

    if not normalized:
        return None

    return normalized.rstrip("/")


def initialize_database(db_path: Path | str = DB_PATH) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                company_id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_name TEXT,
                registration_number TEXT,
                tax_number TEXT,
                country_code TEXT DEFAULT 'SI',
                legal_form TEXT,
                status TEXT,
                website TEXT,
                email TEXT,
                phone TEXT,
                address TEXT,
                municipality TEXT,
                activity TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_verified_at TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_registration_number
                ON companies(registration_number)
                WHERE registration_number IS NOT NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_tax_number
                ON companies(tax_number)
                WHERE tax_number IS NOT NULL;

            CREATE INDEX IF NOT EXISTS ix_companies_canonical_name
                ON companies(canonical_name);

            CREATE INDEX IF NOT EXISTS ix_companies_country_code
                ON companies(country_code);

            CREATE INDEX IF NOT EXISTS ix_companies_updated_at
                ON companies(updated_at);

            CREATE INDEX IF NOT EXISTS ix_companies_last_verified_at
                ON companies(last_verified_at);

            CREATE TABLE IF NOT EXISTS jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,
                source TEXT,
                input_name TEXT,
                status TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                total_items INTEGER DEFAULT 0,
                processed_items INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                config_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS ix_jobs_type_status
                ON jobs(job_type, status);

            CREATE INDEX IF NOT EXISTS ix_jobs_started_at
                ON jobs(started_at);

            CREATE TABLE IF NOT EXISTS company_source_identities (
                source_identity_id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                external_url TEXT,
                observed_at TEXT,
                last_seen_at TEXT,
                FOREIGN KEY(company_id) REFERENCES companies(company_id),
                UNIQUE(source, external_id)
            );

            CREATE INDEX IF NOT EXISTS ix_source_identities_company_id
                ON company_source_identities(company_id);

            CREATE INDEX IF NOT EXISTS ix_source_identities_source_last_seen
                ON company_source_identities(source, last_seen_at);

            CREATE TABLE IF NOT EXISTS source_records (
                source_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                company_id INTEGER,
                source TEXT NOT NULL,
                source_file TEXT,
                source_row INTEGER,
                external_id TEXT,
                raw_json TEXT,
                raw_hash TEXT,
                collected_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(job_id),
                FOREIGN KEY(company_id) REFERENCES companies(company_id)
            );

            DROP INDEX IF EXISTS ux_source_records_file_row;

            CREATE UNIQUE INDEX IF NOT EXISTS ux_source_records_file_row_hash
                ON source_records(source, source_file, source_row, raw_hash)
                WHERE source_file IS NOT NULL
                  AND source_row IS NOT NULL
                  AND raw_hash IS NOT NULL;

            DROP INDEX IF EXISTS ux_source_records_external_id;

            CREATE INDEX IF NOT EXISTS ix_source_records_company_id
                ON source_records(company_id);

            CREATE INDEX IF NOT EXISTS ix_source_records_raw_hash
                ON source_records(raw_hash);

            CREATE TABLE IF NOT EXISTS company_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                job_id INTEGER,
                source_record_id INTEGER,
                fact_key TEXT NOT NULL,
                value_text TEXT,
                value_number REAL,
                value_json TEXT,
                value_year INTEGER,
                source TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                observed_at TEXT,
                verified_at TEXT,
                confidence REAL,
                evidence_json TEXT,
                model_or_rule_version TEXT,
                FOREIGN KEY(company_id) REFERENCES companies(company_id),
                FOREIGN KEY(job_id) REFERENCES jobs(job_id),
                FOREIGN KEY(source_record_id) REFERENCES source_records(source_record_id)
            );

            CREATE INDEX IF NOT EXISTS ix_observations_company_fact_observed
                ON company_observations(company_id, fact_key, observed_at DESC);

            CREATE INDEX IF NOT EXISTS ix_observations_fact_year
                ON company_observations(fact_key, value_year);

            CREATE INDEX IF NOT EXISTS ix_observations_source_collected
                ON company_observations(source, collected_at);

            CREATE UNIQUE INDEX IF NOT EXISTS ux_observations_source_record_fact
                ON company_observations(
                    source_record_id,
                    fact_key,
                    COALESCE(value_year, -1)
                )
                WHERE source_record_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS company_current_facts (
                current_fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                fact_key TEXT NOT NULL,
                value_year INTEGER,
                observation_id INTEGER,
                value_text TEXT,
                value_number REAL,
                value_json TEXT,
                source TEXT,
                confidence REAL,
                evidence_json TEXT,
                observed_at TEXT,
                verified_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(company_id),
                FOREIGN KEY(observation_id) REFERENCES company_observations(observation_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ux_current_facts_identity
                ON company_current_facts(company_id, fact_key, COALESCE(value_year, -1));

            CREATE INDEX IF NOT EXISTS ix_current_facts_fact_year
                ON company_current_facts(fact_key, value_year);

            CREATE INDEX IF NOT EXISTS ix_current_facts_updated_at
                ON company_current_facts(updated_at);

            CREATE TABLE IF NOT EXISTS job_items (
                job_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                company_id INTEGER,
                item_key TEXT NOT NULL,
                input_row_number INTEGER,
                input_payload_json TEXT,
                status TEXT NOT NULL,
                match_status TEXT,
                error_type TEXT,
                error_message TEXT,
                started_at TEXT,
                finished_at TEXT,
                attempt_count INTEGER DEFAULT 0,
                output_payload_json TEXT,
                FOREIGN KEY(job_id) REFERENCES jobs(job_id),
                FOREIGN KEY(company_id) REFERENCES companies(company_id),
                UNIQUE(job_id, item_key)
            );

            CREATE INDEX IF NOT EXISTS ix_job_items_job_status
                ON job_items(job_id, status);

            CREATE INDEX IF NOT EXISTS ix_job_items_company_status
                ON job_items(company_id, status);

            CREATE TABLE IF NOT EXISTS identity_conflicts (
                conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier_type TEXT NOT NULL,
                identifier_value TEXT NOT NULL,
                company_id_a INTEGER,
                company_id_b INTEGER,
                source TEXT,
                details_json TEXT,
                status TEXT DEFAULT 'OPEN',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_notes TEXT,
                FOREIGN KEY(company_id_a) REFERENCES companies(company_id),
                FOREIGN KEY(company_id_b) REFERENCES companies(company_id)
            );

            CREATE INDEX IF NOT EXISTS ix_identity_conflicts_identifier
                ON identity_conflicts(identifier_type, identifier_value);

            CREATE INDEX IF NOT EXISTS ix_identity_conflicts_status
                ON identity_conflicts(status);

            CREATE TABLE IF NOT EXISTS people (
                person_id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT,
                last_name TEXT,
                full_name TEXT,
                normalized_name TEXT,
                email TEXT,
                phone TEXT,
                linkedin_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS ix_people_normalized_name
                ON people(normalized_name);

            CREATE INDEX IF NOT EXISTS ix_people_email
                ON people(email);

            CREATE INDEX IF NOT EXISTS ix_people_linkedin_url
                ON people(linkedin_url);

            CREATE TABLE IF NOT EXISTS company_people (
                company_person_id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                is_current INTEGER DEFAULT 1,
                valid_from TEXT,
                valid_to TEXT,
                source TEXT,
                observed_at TEXT,
                last_seen_at TEXT,
                confidence REAL,
                source_record_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(company_id),
                FOREIGN KEY(person_id) REFERENCES people(person_id),
                FOREIGN KEY(source_record_id) REFERENCES source_records(source_record_id),
                UNIQUE(company_id, person_id)
            );

            CREATE INDEX IF NOT EXISTS ix_company_people_company_id
                ON company_people(company_id);

            CREATE INDEX IF NOT EXISTS ix_company_people_person_id
                ON company_people(person_id);

            CREATE INDEX IF NOT EXISTS ix_company_people_company_current
                ON company_people(company_id, is_current);

            CREATE TABLE IF NOT EXISTS company_person_roles (
                company_person_role_id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_person_id INTEGER NOT NULL,
                role_type TEXT NOT NULL,
                title TEXT,
                ownership_pct REAL,
                is_current INTEGER DEFAULT 1,
                valid_from TEXT,
                valid_to TEXT,
                source TEXT,
                observed_at TEXT,
                last_seen_at TEXT,
                confidence REAL,
                source_record_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(company_person_id)
                    REFERENCES company_people(company_person_id),
                FOREIGN KEY(source_record_id) REFERENCES source_records(source_record_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ux_company_person_roles_active
                ON company_person_roles(
                    company_person_id,
                    role_type,
                    COALESCE(title, '')
                )
                WHERE is_current = 1 AND valid_to IS NULL;

            CREATE INDEX IF NOT EXISTS ix_company_person_roles_company_person
                ON company_person_roles(company_person_id);

            CREATE INDEX IF NOT EXISTS ix_company_person_roles_role_type
                ON company_person_roles(role_type);

            CREATE INDEX IF NOT EXISTS ix_company_person_roles_current
                ON company_person_roles(company_person_id, is_current);
            """
        )


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def raw_hash(raw: Any) -> str:
    return hashlib.sha256(json_dumps(raw).encode("utf-8")).hexdigest()


def create_job(
    *,
    job_type: str,
    source: str | None = None,
    input_name: str | None = None,
    total_items: int = 0,
    config: dict[str, Any] | None = None,
    db_path: Path | str = DB_PATH,
) -> int:
    initialize_database(db_path)
    now = utc_now()

    with get_connection(db_path) as conn:
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
                job_type,
                source,
                input_name,
                "RUNNING",
                now,
                total_items,
                json_dumps(config or {}),
                now,
            ),
        )
        return int(cursor.lastrowid)


def finish_job(
    *,
    job_id: int,
    status: str,
    processed_items: int,
    error_count: int = 0,
    db_path: Path | str = DB_PATH,
) -> None:
    initialize_database(db_path)

    with get_connection(db_path) as conn:
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
    *,
    job_id: int | None,
    company_id: int | None,
    source: str,
    source_file: str | None,
    source_row: int | None,
    external_id: str | None,
    raw: dict[str, Any],
    collected_at: str,
    db_path: Path | str = DB_PATH,
) -> SourceRecordResult:
    initialize_database(db_path)
    payload_hash = raw_hash(raw)

    with get_connection(db_path) as conn:
        existing = conn.execute(
            """
            SELECT source_record_id, company_id
            FROM source_records
            WHERE source = ?
              AND source_file = ?
              AND source_row = ?
              AND raw_hash = ?
            """,
            (source, source_file, source_row, payload_hash),
        ).fetchone()

        if existing is not None:
            existing_company_id = (
                int(existing["company_id"])
                if existing["company_id"] is not None
                else None
            )
            if company_id is not None and existing_company_id is None:
                conn.execute(
                    """
                    UPDATE source_records
                    SET company_id = ?
                    WHERE source_record_id = ?
                    """,
                    (company_id, existing["source_record_id"]),
                )
                existing_company_id = company_id

            return SourceRecordResult(
                source_record_id=int(existing["source_record_id"]),
                created=False,
                company_id=existing_company_id,
            )

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
                source,
                source_file,
                source_row,
                external_id,
                json_dumps(raw),
                payload_hash,
                collected_at,
            ),
        )
        return SourceRecordResult(
            source_record_id=int(cursor.lastrowid),
            created=True,
            company_id=company_id,
        )


def update_source_record_company(
    *,
    source_record_id: int,
    company_id: int,
    db_path: Path | str = DB_PATH,
) -> None:
    initialize_database(db_path)

    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE source_records
            SET company_id = ?
            WHERE source_record_id = ?
            """,
            (company_id, source_record_id),
        )


def create_job_item(
    *,
    job_id: int,
    company_id: int | None,
    item_key: str,
    input_row_number: int | None,
    input_payload: dict[str, Any],
    status: str,
    match_status: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    output_payload: dict[str, Any] | None = None,
    attempt_count: int = 1,
    db_path: Path | str = DB_PATH,
) -> None:
    initialize_database(db_path)
    now = utc_now()

    with get_connection(db_path) as conn:
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
            ON CONFLICT(job_id, item_key) DO UPDATE SET
                company_id = excluded.company_id,
                status = excluded.status,
                match_status = excluded.match_status,
                error_type = excluded.error_type,
                error_message = excluded.error_message,
                finished_at = excluded.finished_at,
                attempt_count = job_items.attempt_count + excluded.attempt_count,
                output_payload_json = excluded.output_payload_json
            """,
            (
                job_id,
                company_id,
                item_key,
                input_row_number,
                json_dumps(input_payload),
                status,
                match_status,
                error_type,
                error_message,
                now,
                now,
                attempt_count,
                json_dumps(output_payload or {}),
            ),
        )


def _find_company_by_identifier(
    conn: sqlite3.Connection,
    column: str,
    value: str | None,
) -> sqlite3.Row | None:
    if not value:
        return None

    return conn.execute(
        f"SELECT * FROM companies WHERE {column} = ?",
        (value,),
    ).fetchone()


def find_company_id_by_identifiers(
    *,
    registration_number: Any = None,
    tax_number: Any = None,
    db_path: Path | str = DB_PATH,
) -> int | None:
    initialize_database(db_path)
    normalized_registration = normalize_registration_number(registration_number)
    normalized_tax = normalize_tax_number(tax_number)

    with get_connection(db_path) as conn:
        ids: set[int] = set()

        reg_company = _find_company_by_identifier(
            conn,
            "registration_number",
            normalized_registration,
        )
        if reg_company is not None:
            ids.add(int(reg_company["company_id"]))

        tax_company = _find_company_by_identifier(
            conn,
            "tax_number",
            normalized_tax,
        )
        if tax_company is not None:
            ids.add(int(tax_company["company_id"]))

    if len(ids) > 1:
        raise IdentityConflictError(
            conflict_id=-1,
            message="registration_number and tax_number resolve to different companies",
        )

    return next(iter(ids), None)


def _record_identity_conflict(
    conn: sqlite3.Connection,
    *,
    identifier_type: str,
    identifier_value: str,
    company_id_a: int | None,
    company_id_b: int | None,
    source: str | None,
    details: dict[str, Any],
) -> int:
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO identity_conflicts(
            identifier_type,
            identifier_value,
            company_id_a,
            company_id_b,
            source,
            details_json,
            created_at
        )
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            identifier_type,
            identifier_value,
            company_id_a,
            company_id_b,
            source,
            json_dumps(details),
            now,
        ),
    )
    return int(cursor.lastrowid)


def _update_company_identity(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    canonical_name: str | None,
    registration_number: str | None,
    tax_number: str | None,
    country_code: str,
    source: str | None,
) -> None:
    row = conn.execute(
        "SELECT * FROM companies WHERE company_id = ?",
        (company_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Company does not exist: {company_id}")

    updates: dict[str, Any] = {}

    if canonical_name and not row["canonical_name"]:
        updates["canonical_name"] = canonical_name

    if country_code and not row["country_code"]:
        updates["country_code"] = country_code

    for column, value in (
        ("registration_number", registration_number),
        ("tax_number", tax_number),
    ):
        if not value:
            continue

        current_value = row[column]

        if current_value and current_value != value:
            conflict_id = _record_identity_conflict(
                conn,
                identifier_type=column,
                identifier_value=value,
                company_id_a=company_id,
                company_id_b=None,
                source=source,
                details={
                    "existing_value": current_value,
                    "incoming_value": value,
                    "reason": "identifier_value_conflict_on_existing_company",
                },
            )
            conn.commit()
            raise IdentityConflictError(
                conflict_id,
                f"{column} conflicts with existing company {company_id}",
            )

        if not current_value:
            updates[column] = value

    if not updates:
        return

    updates["updated_at"] = utc_now()
    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE companies SET {assignments} WHERE company_id = ?",
        (*updates.values(), company_id),
    )


def resolve_or_create_company(
    *,
    canonical_name: str | None = None,
    registration_number: Any = None,
    tax_number: Any = None,
    country_code: str = "SI",
    source: str | None = None,
    db_path: Path | str = DB_PATH,
) -> ResolveCompanyResult:
    initialize_database(db_path)
    normalized_registration = normalize_registration_number(registration_number)
    normalized_tax = normalize_tax_number(tax_number)

    with get_connection(db_path) as conn:
        reg_company = _find_company_by_identifier(
            conn,
            "registration_number",
            normalized_registration,
        )
        tax_company = _find_company_by_identifier(
            conn,
            "tax_number",
            normalized_tax,
        )

        if (
            reg_company is not None
            and tax_company is not None
            and reg_company["company_id"] != tax_company["company_id"]
        ):
            conflict_id = _record_identity_conflict(
                conn,
                identifier_type="registration_number+tax_number",
                identifier_value=(
                    f"{normalized_registration}|{normalized_tax}"
                ),
                company_id_a=int(reg_company["company_id"]),
                company_id_b=int(tax_company["company_id"]),
                source=source,
                details={
                    "registration_number": normalized_registration,
                    "tax_number": normalized_tax,
                    "reason": "identifiers_resolve_to_different_companies",
                },
            )
            conn.commit()
            raise IdentityConflictError(
                conflict_id,
                "registration_number and tax_number resolve to different companies",
            )

        existing = reg_company or tax_company

        if existing is not None:
            company_id = int(existing["company_id"])
            _update_company_identity(
                conn,
                company_id=company_id,
                canonical_name=canonical_name,
                registration_number=normalized_registration,
                tax_number=normalized_tax,
                country_code=country_code,
                source=source,
            )
            return ResolveCompanyResult(company_id=company_id, created=False)

        now = utc_now()
        cursor = conn.execute(
            """
            INSERT INTO companies(
                canonical_name,
                registration_number,
                tax_number,
                country_code,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                canonical_name,
                normalized_registration,
                normalized_tax,
                country_code,
                now,
                now,
            ),
        )
        return ResolveCompanyResult(
            company_id=int(cursor.lastrowid),
            created=True,
        )


def _update_person_if_blank(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    first_name: str | None,
    last_name: str | None,
    full_name: str | None,
    email: str | None,
    phone: str | None,
    linkedin_url: str | None,
) -> None:
    row = conn.execute(
        "SELECT * FROM people WHERE person_id = ?",
        (person_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Person does not exist: {person_id}")

    updates: dict[str, Any] = {}
    values = {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "normalized_name": normalize_person_name(full_name),
        "email": email,
        "phone": phone,
        "linkedin_url": linkedin_url,
    }

    for column, value in values.items():
        if value and not row[column]:
            updates[column] = value

    if not updates:
        return

    updates["updated_at"] = utc_now()
    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE people SET {assignments} WHERE person_id = ?",
        (*updates.values(), person_id),
    )


def resolve_or_create_person(
    *,
    first_name: Any = None,
    last_name: Any = None,
    full_name: Any = None,
    email: Any = None,
    phone: Any = None,
    linkedin_url: Any = None,
    db_path: Path | str = DB_PATH,
) -> ResolvePersonResult:
    initialize_database(db_path)
    first_name_text = str(first_name).strip() if first_name else None
    last_name_text = str(last_name).strip() if last_name else None
    full_name_text = str(full_name).strip() if full_name else None
    email_text = normalize_email(email)
    linkedin_text = normalize_linkedin_url(linkedin_url)
    phone_text = str(phone).strip() if phone else None

    if not full_name_text:
        full_name_text = " ".join(
            part for part in (first_name_text, last_name_text) if part
        ) or None

    with get_connection(db_path) as conn:
        if linkedin_text:
            row = conn.execute(
                """
                SELECT person_id
                FROM people
                WHERE linkedin_url = ?
                LIMIT 1
                """,
                (linkedin_text,),
            ).fetchone()

            if row is not None:
                person_id = int(row["person_id"])
                _update_person_if_blank(
                    conn,
                    person_id=person_id,
                    first_name=first_name_text,
                    last_name=last_name_text,
                    full_name=full_name_text,
                    email=email_text,
                    phone=phone_text,
                    linkedin_url=linkedin_text,
                )
                return ResolvePersonResult(person_id=person_id, created=False)

        if email_text:
            rows = conn.execute(
                """
                SELECT person_id
                FROM people
                WHERE email = ?
                """,
                (email_text,),
            ).fetchall()

            if len(rows) == 1:
                person_id = int(rows[0]["person_id"])
                _update_person_if_blank(
                    conn,
                    person_id=person_id,
                    first_name=first_name_text,
                    last_name=last_name_text,
                    full_name=full_name_text,
                    email=email_text,
                    phone=phone_text,
                    linkedin_url=linkedin_text,
                )
                return ResolvePersonResult(person_id=person_id, created=False)

        now = utc_now()
        cursor = conn.execute(
            """
            INSERT INTO people(
                first_name,
                last_name,
                full_name,
                normalized_name,
                email,
                phone,
                linkedin_url,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                first_name_text,
                last_name_text,
                full_name_text,
                normalize_person_name(full_name_text),
                email_text,
                phone_text,
                linkedin_text,
                now,
                now,
            ),
        )
        return ResolvePersonResult(
            person_id=int(cursor.lastrowid),
            created=True,
        )


def link_person_to_company(
    *,
    company_id: int,
    person_id: int,
    source: str | None = None,
    observed_at: str | None = None,
    confidence: float | None = None,
    source_record_id: int | None = None,
    db_path: Path | str = DB_PATH,
) -> LinkPersonCompanyResult:
    initialize_database(db_path)
    now = utc_now()
    observed = observed_at or now

    with get_connection(db_path) as conn:
        existing = conn.execute(
            """
            SELECT company_person_id
            FROM company_people
            WHERE company_id = ? AND person_id = ?
            """,
            (company_id, person_id),
        ).fetchone()

        if existing is not None:
            company_person_id = int(existing["company_person_id"])
            conn.execute(
                """
                UPDATE company_people
                SET is_current = 1,
                    source = COALESCE(?, source),
                    observed_at = COALESCE(observed_at, ?),
                    last_seen_at = ?,
                    confidence = COALESCE(?, confidence),
                    source_record_id = COALESCE(?, source_record_id),
                    updated_at = ?
                WHERE company_person_id = ?
                """,
                (
                    source,
                    observed,
                    observed,
                    confidence,
                    source_record_id,
                    now,
                    company_person_id,
                ),
            )
            return LinkPersonCompanyResult(
                company_person_id=company_person_id,
                created=False,
            )

        cursor = conn.execute(
            """
            INSERT INTO company_people(
                company_id,
                person_id,
                is_current,
                source,
                observed_at,
                last_seen_at,
                confidence,
                source_record_id,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                company_id,
                person_id,
                1,
                source,
                observed,
                observed,
                confidence,
                source_record_id,
                now,
                now,
            ),
        )
        return LinkPersonCompanyResult(
            company_person_id=int(cursor.lastrowid),
            created=True,
        )


def add_company_person_role(
    *,
    company_person_id: int,
    role_type: str,
    title: str | None = None,
    ownership_pct: float | None = None,
    source: str | None = None,
    observed_at: str | None = None,
    confidence: float | None = None,
    source_record_id: int | None = None,
    db_path: Path | str = DB_PATH,
) -> CompanyPersonRoleResult:
    initialize_database(db_path)
    role = str(role_type).strip().upper()

    if not role:
        raise ValueError("role_type is required")

    title_text = str(title).strip() if title else None
    now = utc_now()
    observed = observed_at or now

    with get_connection(db_path) as conn:
        existing = conn.execute(
            """
            SELECT company_person_role_id
            FROM company_person_roles
            WHERE company_person_id = ?
              AND role_type = ?
              AND COALESCE(title, '') = COALESCE(?, '')
              AND is_current = 1
              AND valid_to IS NULL
            """,
            (company_person_id, role, title_text),
        ).fetchone()

        if existing is not None:
            role_id = int(existing["company_person_role_id"])
            conn.execute(
                """
                UPDATE company_person_roles
                SET ownership_pct = COALESCE(?, ownership_pct),
                    source = COALESCE(?, source),
                    observed_at = COALESCE(observed_at, ?),
                    last_seen_at = ?,
                    confidence = COALESCE(?, confidence),
                    source_record_id = COALESCE(?, source_record_id),
                    updated_at = ?
                WHERE company_person_role_id = ?
                """,
                (
                    ownership_pct,
                    source,
                    observed,
                    observed,
                    confidence,
                    source_record_id,
                    now,
                    role_id,
                ),
            )
            return CompanyPersonRoleResult(
                company_person_role_id=role_id,
                created=False,
            )

        cursor = conn.execute(
            """
            INSERT INTO company_person_roles(
                company_person_id,
                role_type,
                title,
                ownership_pct,
                is_current,
                source,
                observed_at,
                last_seen_at,
                confidence,
                source_record_id,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                company_person_id,
                role,
                title_text,
                ownership_pct,
                1,
                source,
                observed,
                observed,
                confidence,
                source_record_id,
                now,
                now,
            ),
        )
        return CompanyPersonRoleResult(
            company_person_role_id=int(cursor.lastrowid),
            created=True,
        )
