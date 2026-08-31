from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from ingestion.models import CompanyRecord, PersonRecord


class AdapterStatus(str, Enum):
    SUCCESS = "SUCCESS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SKIPPED = "SKIPPED"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"


@dataclass(frozen=True)
class EnrichmentTask:
    task_id: int
    company_id: int | None
    person_id: int | None
    source: str
    task_type: str
    status: str
    priority: int
    payload: dict[str, Any]
    result: dict[str, Any]
    attempt_count: int
    max_attempts: int
    not_before: str | None
    claimed_at: str | None
    claimed_by: str | None
    started_at: str | None
    finished_at: str | None
    error_type: str | None
    error_message: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "EnrichmentTask":
        return cls(
            task_id=int(row["task_id"]),
            company_id=row["company_id"],
            person_id=row["person_id"],
            source=str(row["source"]),
            task_type=str(row["task_type"]),
            status=str(row["status"]),
            priority=int(row["priority"]),
            payload=json.loads(row["payload_json"] or "{}"),
            result=json.loads(row["result_json"] or "{}"),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            not_before=row["not_before"],
            claimed_at=row["claimed_at"],
            claimed_by=row["claimed_by"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_type=row["error_type"],
            error_message=row["error_message"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True)
class AdapterResult:
    status: AdapterStatus
    company_records: list[CompanyRecord] = field(default_factory=list)
    person_records: list[PersonRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class AdapterContext:
    db_path: Path | str
    worker_id: str
    consume_source_budget: Callable[[str], bool]


class EnrichmentAdapter(Protocol):
    source: str
    task_types: tuple[str, ...]

    def execute(
        self,
        task: EnrichmentTask,
        context: AdapterContext,
    ) -> AdapterResult:
        ...
