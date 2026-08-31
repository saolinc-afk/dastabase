from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from database_v2 import (
    DB_PATH,
    IdentityConflictError,
    add_company_ownership,
    add_company_person_role,
    get_connection,
    initialize_database,
    json_dumps,
    upsert_source_record,
    resolve_or_create_company,
    resolve_or_create_person,
    link_person_to_company,
    utc_now,
)
from ingestion.models import CompanyRecord, FactRecord, IngestCompanyResult


CURRENT_COMPANY_FIELDS = (
    "canonical_name",
    "registration_number",
    "tax_number",
    "country_code",
    "website",
    "email",
    "phone",
    "address",
    "municipality",
    "activity",
)


def _blank(value: Any) -> bool:
    return value is None or value == ""


def _typed_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"value_text": str(value)}

    if isinstance(value, (int, float)):
        return {"value_number": float(value)}

    if isinstance(value, (dict, list, tuple)):
        return {"value_json": json_dumps(value)}

    return {"value_text": str(value)}


def _find_existing_source_record_observation(
    conn: sqlite3.Connection,
    *,
    source_record_id: int,
    fact_key: str,
    value_year: int | None,
) -> int | None:
    row = conn.execute(
        """
        SELECT observation_id
        FROM company_observations
        WHERE source_record_id = ?
          AND fact_key = ?
          AND COALESCE(value_year, -1) = COALESCE(?, -1)
        """,
        (source_record_id, fact_key, value_year),
    ).fetchone()

    return int(row["observation_id"]) if row is not None else None


def _find_existing_value_observation(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    source: str,
    fact_key: str,
    value_year: int | None,
    value_columns: dict[str, Any],
    model_or_rule_version: str | None,
) -> int | None:
    row = conn.execute(
        """
        SELECT observation_id
        FROM company_observations
        WHERE company_id = ?
          AND source = ?
          AND fact_key = ?
          AND COALESCE(value_year, -1) = COALESCE(?, -1)
          AND COALESCE(value_text, '') = COALESCE(?, '')
          AND COALESCE(value_number, -999999999999.0) = COALESCE(?, -999999999999.0)
          AND COALESCE(value_json, '') = COALESCE(?, '')
          AND COALESCE(model_or_rule_version, '') = COALESCE(?, '')
        """,
        (
            company_id,
            source,
            fact_key,
            value_year,
            value_columns.get("value_text"),
            value_columns.get("value_number"),
            value_columns.get("value_json"),
            model_or_rule_version,
        ),
    ).fetchone()

    return int(row["observation_id"]) if row is not None else None


def _find_person_id_by_source_role(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    source: str,
    source_role_id: str,
) -> int | None:
    row = conn.execute(
        """
        SELECT cp.person_id
        FROM company_person_roles cpr
        JOIN company_people cp
          ON cp.company_person_id = cpr.company_person_id
        WHERE cp.company_id = ?
          AND cpr.source = ?
          AND cpr.source_role_id = ?
          AND cpr.is_current = 1
          AND cpr.valid_to IS NULL
        LIMIT 1
        """,
        (company_id, source, source_role_id),
    ).fetchone()

    return int(row["person_id"]) if row is not None else None


def _insert_observation(
    conn: sqlite3.Connection,
    *,
    record: CompanyRecord,
    company_id: int,
    fact: FactRecord,
    collected_at: str,
    source_record_id: int | None = None,
) -> tuple[int, bool]:
    value_columns = _typed_value(fact.value)
    effective_source_record_id = source_record_id or record.source_record_id

    if effective_source_record_id is not None:
        existing_id = _find_existing_source_record_observation(
            conn,
            source_record_id=effective_source_record_id,
            fact_key=fact.fact_key,
            value_year=fact.value_year,
        )
        if existing_id is not None:
            return existing_id, False
    else:
        existing_id = _find_existing_value_observation(
            conn,
            company_id=company_id,
            source=record.source,
            fact_key=fact.fact_key,
            value_year=fact.value_year,
            value_columns=value_columns,
            model_or_rule_version=fact.model_or_rule_version,
        )
        if existing_id is not None:
            return existing_id, False

    cursor = conn.execute(
        """
        INSERT INTO company_observations(
            company_id,
            job_id,
            source_record_id,
            fact_key,
            value_text,
            value_number,
            value_json,
            value_year,
            source,
            collected_at,
            observed_at,
            verified_at,
            confidence,
            evidence_json,
            model_or_rule_version
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            company_id,
            record.job_id,
            effective_source_record_id,
            fact.fact_key,
            value_columns.get("value_text"),
            value_columns.get("value_number"),
            value_columns.get("value_json"),
            fact.value_year,
            record.source,
            collected_at,
            fact.observed_at or record.observed_at or collected_at,
            fact.verified_at or record.verified_at,
            fact.confidence,
            json_dumps(fact.evidence) if fact.evidence is not None else None,
            fact.model_or_rule_version,
        ),
    )
    return int(cursor.lastrowid), True


def _upsert_current_fact(
    conn: sqlite3.Connection,
    *,
    record: CompanyRecord,
    company_id: int,
    fact: FactRecord,
    observation_id: int,
    collected_at: str,
) -> None:
    value_columns = _typed_value(fact.value)
    existing = conn.execute(
        """
        SELECT current_fact_id
        FROM company_current_facts
        WHERE company_id = ?
          AND fact_key = ?
          AND COALESCE(value_year, -1) = COALESCE(?, -1)
        """,
        (company_id, fact.fact_key, fact.value_year),
    ).fetchone()

    payload = {
        "observation_id": observation_id,
        "value_text": value_columns.get("value_text"),
        "value_number": value_columns.get("value_number"),
        "value_json": value_columns.get("value_json"),
        "source": record.source,
        "confidence": fact.confidence,
        "evidence_json": json_dumps(fact.evidence) if fact.evidence is not None else None,
        "observed_at": fact.observed_at or record.observed_at or collected_at,
        "verified_at": fact.verified_at or record.verified_at,
        "updated_at": utc_now(),
    }

    if existing is not None:
        assignments = ", ".join(f"{column} = ?" for column in payload)
        conn.execute(
            f"UPDATE company_current_facts SET {assignments} WHERE current_fact_id = ?",
            (*payload.values(), int(existing["current_fact_id"])),
        )
        return

    conn.execute(
        """
        INSERT INTO company_current_facts(
            company_id,
            fact_key,
            value_year,
            observation_id,
            value_text,
            value_number,
            value_json,
            source,
            confidence,
            evidence_json,
            observed_at,
            verified_at,
            updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            company_id,
            fact.fact_key,
            fact.value_year,
            payload["observation_id"],
            payload["value_text"],
            payload["value_number"],
            payload["value_json"],
            payload["source"],
            payload["confidence"],
            payload["evidence_json"],
            payload["observed_at"],
            payload["verified_at"],
            payload["updated_at"],
        ),
    )


def _fill_company_current_fields(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    record: CompanyRecord,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    row = conn.execute(
        "SELECT * FROM companies WHERE company_id = ?",
        (company_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Company does not exist: {company_id}")

    updates: dict[str, Any] = {}
    warnings: list[str] = []

    for column in CURRENT_COMPANY_FIELDS:
        incoming = getattr(record, column)
        if _blank(incoming):
            continue

        current = row[column]
        if _blank(current):
            updates[column] = incoming
        elif str(current).strip() != str(incoming).strip():
            warnings.append(
                f"{column} conflict kept existing value "
                f"{current!r}; incoming value {incoming!r}"
            )

    if updates:
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE companies SET {assignments} WHERE company_id = ?",
            (*updates.values(), company_id),
        )

    return tuple(column for column in updates if column != "updated_at"), tuple(warnings)


def _upsert_source_identity(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    record: CompanyRecord,
    observed_at: str,
) -> tuple[bool, str | None]:
    if not record.external_id:
        return False, None

    existing = conn.execute(
        """
        SELECT company_id
        FROM company_source_identities
        WHERE source = ? AND external_id = ?
        """,
        (record.source, record.external_id),
    ).fetchone()

    if existing is not None and int(existing["company_id"]) != company_id:
        now = utc_now()
        conn.execute(
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
                "source_identity",
                f"{record.source}:{record.external_id}",
                int(existing["company_id"]),
                company_id,
                record.source,
                json_dumps(
                    {
                        "source": record.source,
                        "external_id": record.external_id,
                        "reason": "source_external_id_resolves_to_different_company",
                    }
                ),
                now,
            ),
        )
        return (
            False,
            f"{record.source} external_id {record.external_id} already belongs "
            "to another company",
        )

    conn.execute(
        """
        INSERT INTO company_source_identities(
            company_id,
            source,
            external_id,
            external_url,
            observed_at,
            last_seen_at
        )
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(source, external_id) DO UPDATE SET
            external_url = COALESCE(excluded.external_url, external_url),
            last_seen_at = excluded.last_seen_at
        """,
        (
            company_id,
            record.source,
            record.external_id,
            record.external_url,
            observed_at,
            observed_at,
        ),
    )
    return True, None


def ingest_company(
    record: CompanyRecord,
    *,
    db_path: Path | str = DB_PATH,
) -> IngestCompanyResult:
    initialize_database(db_path)

    if not record.registration_number and not record.tax_number:
        return IngestCompanyResult(
            company_id=None,
            status="UNRESOLVED",
            warnings=("No registration_number or tax_number; name matching is disabled",),
        )

    observed_at = record.observed_at or utc_now()

    try:
        resolved = resolve_or_create_company(
            canonical_name=record.canonical_name,
            registration_number=record.registration_number,
            tax_number=record.tax_number,
            country_code=record.country_code,
            source=record.source,
            db_path=db_path,
        )
    except IdentityConflictError as exc:
        return IngestCompanyResult(
            company_id=None,
            status="IDENTITY_CONFLICT",
            conflicts=(f"{exc}; conflict_id={exc.conflict_id}",),
        )

    warnings: list[str] = []
    updated_fields: list[str] = []
    facts_written = 0
    people_linked = 0
    roles_written = 0
    ownership_written = 0
    source_record_id = record.source_record_id

    if record.raw_data is not None and source_record_id is None:
        source_record = upsert_source_record(
            job_id=record.job_id,
            company_id=resolved.company_id,
            source=record.source,
            source_file=None,
            source_row=None,
            external_id=record.external_id,
            raw=record.raw_data,
            collected_at=observed_at,
            db_path=db_path,
        )
        source_record_id = source_record.source_record_id

    with get_connection(db_path) as conn:
        fields, field_warnings = _fill_company_current_fields(
            conn,
            company_id=resolved.company_id,
            record=record,
        )
        updated_fields.extend(fields)
        warnings.extend(field_warnings)

        _identity_written, identity_warning = _upsert_source_identity(
            conn,
            company_id=resolved.company_id,
            record=record,
            observed_at=observed_at,
        )
        if identity_warning:
            return IngestCompanyResult(
                company_id=resolved.company_id,
                status="IDENTITY_CONFLICT",
                created_company=resolved.created,
                conflicts=(identity_warning,),
                warnings=tuple(warnings),
            )

        for fact in record.facts:
            if not fact.fact_key or fact.value is None:
                continue

            observation_id, created = _insert_observation(
                conn,
                record=record,
                company_id=resolved.company_id,
                fact=fact,
                collected_at=observed_at,
                source_record_id=source_record_id,
            )
            _upsert_current_fact(
                conn,
                record=record,
                company_id=resolved.company_id,
                fact=fact,
                observation_id=observation_id,
                collected_at=observed_at,
            )
            if created:
                facts_written += 1

    for person in record.people:
        existing_person_id = None
        role_source_ids = [role.source_role_id for role in person.roles if role.source_role_id]
        if role_source_ids:
            with get_connection(db_path) as conn:
                existing_person_id = _find_person_id_by_source_role(
                    conn,
                    company_id=resolved.company_id,
                    source=record.source,
                    source_role_id=role_source_ids[0],
                )

        if existing_person_id is None:
            person_result = resolve_or_create_person(
                first_name=person.first_name,
                last_name=person.last_name,
                full_name=person.full_name,
                email=person.email,
                phone=person.phone,
                linkedin_url=person.linkedin_url,
                db_path=db_path,
            )
            person_id = person_result.person_id
        else:
            person_id = existing_person_id

        link_result = link_person_to_company(
            company_id=resolved.company_id,
            person_id=person_id,
            source=record.source,
            observed_at=observed_at,
            source_record_id=source_record_id,
            db_path=db_path,
        )
        if link_result.created:
            people_linked += 1

        for role in person.roles:
            role_result = add_company_person_role(
                company_person_id=link_result.company_person_id,
                role_type=role.role_type,
                title=role.title,
                ownership_pct=role.ownership_pct,
                source_role_id=role.source_role_id,
                source=record.source,
                observed_at=role.observed_at or observed_at,
                valid_from=role.valid_from,
                valid_to=role.valid_to,
                is_current=role.is_current,
                confidence=role.confidence,
                evidence=role.evidence,
                source_record_id=source_record_id,
                db_path=db_path,
            )
            if role_result.created:
                roles_written += 1

    for ownership in record.ownership:
        ownership_result = add_company_ownership(
            owned_company_id=resolved.company_id,
            source=ownership.source,
            source_shareholder_id=ownership.source_shareholder_id,
            source_share_id=ownership.source_share_id,
            owner_person_id=ownership.owner_person_id,
            owner_company_id=ownership.owner_company_id,
            owner_raw_name=ownership.owner_raw_name,
            owner_raw_identifier=ownership.owner_raw_identifier,
            owner_type=ownership.owner_type,
            share_raw=ownership.share_raw,
            share_percent=ownership.share_percent,
            nominal_value_raw=ownership.nominal_value_raw,
            valid_from=ownership.valid_from,
            valid_to=ownership.valid_to,
            is_current=ownership.is_current,
            evidence_url=ownership.evidence_url,
            evidence=ownership.evidence,
            observed_at=ownership.observed_at or observed_at,
            source_record_id=source_record_id,
            db_path=db_path,
        )
        if ownership_result.created:
            ownership_written += 1

    return IngestCompanyResult(
        company_id=resolved.company_id,
        status="CREATED" if resolved.created else "RESOLVED",
        created_company=resolved.created,
        updated_fields=tuple(updated_fields),
        facts_written=facts_written,
        people_linked=people_linked,
        roles_written=roles_written,
        ownership_written=ownership_written,
        warnings=tuple(warnings),
    )
