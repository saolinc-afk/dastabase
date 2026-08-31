from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FactRecord:
    fact_key: str
    value: Any
    value_year: int | None = None
    confidence: float | None = None
    evidence: Any | None = None
    observed_at: str | None = None
    verified_at: str | None = None
    model_or_rule_version: str | None = None


@dataclass(frozen=True)
class PersonRoleRecord:
    role_type: str
    title: str | None = None
    ownership_pct: float | None = None
    is_current: bool = True
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float | None = None
    observed_at: str | None = None
    source_role_id: str | None = None
    evidence: Any | None = None


@dataclass(frozen=True)
class PersonRecord:
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    roles: list[PersonRoleRecord] = field(default_factory=list)


@dataclass(frozen=True)
class OwnershipRecord:
    source: str
    source_shareholder_id: str | None = None
    source_share_id: str | None = None
    owner_person_id: int | None = None
    owner_company_id: int | None = None
    owner_raw_name: str | None = None
    owner_raw_identifier: str | None = None
    owner_type: str | None = None
    share_raw: str | None = None
    share_percent: float | None = None
    nominal_value_raw: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    is_current: bool = True
    evidence_url: str | None = None
    evidence: Any | None = None
    observed_at: str | None = None


@dataclass(frozen=True)
class CompanyRecord:
    source: str
    external_id: str | None = None
    external_url: str | None = None
    canonical_name: str | None = None
    registration_number: str | None = None
    tax_number: str | None = None
    country_code: str = "SI"
    website: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    municipality: str | None = None
    activity: str | None = None
    observed_at: str | None = None
    verified_at: str | None = None
    source_record_id: int | None = None
    job_id: int | None = None
    raw_data: dict[str, Any] | None = None
    facts: list[FactRecord] = field(default_factory=list)
    people: list[PersonRecord] = field(default_factory=list)
    ownership: list[OwnershipRecord] = field(default_factory=list)


@dataclass(frozen=True)
class IngestCompanyResult:
    company_id: int | None
    status: str
    created_company: bool = False
    updated_fields: tuple[str, ...] = ()
    facts_written: int = 0
    people_linked: int = 0
    roles_written: int = 0
    ownership_written: int = 0
    conflicts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
