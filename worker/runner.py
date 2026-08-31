from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from database_v2 import (
    DB_PATH,
    claim_next_enrichment_task,
    consume_source_budget,
    resolve_or_create_person,
    update_enrichment_task,
)
from ingestion.writer import ingest_company
from worker.models import AdapterContext, AdapterResult, AdapterStatus, EnrichmentTask
from worker.registry import AdapterRegistry


class WorkerRunner:
    def __init__(
        self,
        *,
        registry: AdapterRegistry,
        worker_id: str,
        db_path: Path | str = DB_PATH,
        retry_base_minutes: int = 15,
    ) -> None:
        self.registry = registry
        self.worker_id = worker_id
        self.db_path = db_path
        self.retry_base_minutes = retry_base_minutes

    def execute_once(self) -> EnrichmentTask | None:
        row = claim_next_enrichment_task(
            worker_id=self.worker_id,
            db_path=self.db_path,
        )
        if row is None:
            return None

        task = EnrichmentTask.from_row(row)
        adapter = self.registry.get(task.source, task.task_type)

        if adapter is None:
            update_enrichment_task(
                task_id=task.task_id,
                status="SKIPPED",
                result={"reason": "No adapter registered"},
                error_type="NO_ADAPTER",
                error_message=f"No adapter for {task.source}/{task.task_type}",
                db_path=self.db_path,
            )
            return task

        context = AdapterContext(
            db_path=self.db_path,
            worker_id=self.worker_id,
            consume_source_budget=lambda source=task.source: consume_source_budget(
                source,
                db_path=self.db_path,
            ),
        )

        try:
            result = adapter.execute(task, context)
        except Exception as exc:
            result = AdapterResult(
                status=AdapterStatus.RETRYABLE_ERROR,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )

        self._record_result(task, result)
        return task

    def _record_result(self, task: EnrichmentTask, result: AdapterResult) -> None:
        result_payload: dict[str, Any] = {
            "adapter_status": result.status.value,
            "warnings": result.warnings,
            "metadata": result.metadata,
        }
        if "page_diagnostics" in result.metadata:
            result_payload["page_diagnostics"] = result.metadata["page_diagnostics"]
        ingest_results: list[dict[str, Any]] = []

        for company_record in result.company_records:
            ingest_result = ingest_company(company_record, db_path=self.db_path)
            ingest_results.append(
                {
                    "company_id": ingest_result.company_id,
                    "status": ingest_result.status,
                    "created_company": ingest_result.created_company,
                    "updated_fields": list(ingest_result.updated_fields),
                    "facts_written": ingest_result.facts_written,
                    "people_linked": ingest_result.people_linked,
                    "roles_written": ingest_result.roles_written,
                    "ownership_written": ingest_result.ownership_written,
                    "conflicts": list(ingest_result.conflicts),
                    "warnings": list(ingest_result.warnings),
                }
            )

        person_ids: list[int] = []
        for person_record in result.person_records:
            person_result = resolve_or_create_person(
                first_name=person_record.first_name,
                last_name=person_record.last_name,
                full_name=person_record.full_name,
                email=person_record.email,
                phone=person_record.phone,
                linkedin_url=person_record.linkedin_url,
                db_path=self.db_path,
            )
            person_ids.append(person_result.person_id)

        if ingest_results:
            result_payload["ingest_results"] = ingest_results

        if person_ids:
            result_payload["person_ids"] = person_ids

        if result.status == AdapterStatus.SUCCESS:
            if any(item["status"] == "IDENTITY_CONFLICT" for item in ingest_results):
                update_enrichment_task(
                    task_id=task.task_id,
                    status="REVIEW_REQUIRED",
                    result=result_payload,
                    error_type="IDENTITY_CONFLICT",
                    error_message="Ingestion detected an identity conflict",
                    db_path=self.db_path,
                )
                return

            update_enrichment_task(
                task_id=task.task_id,
                status="DONE",
                result=result_payload,
                db_path=self.db_path,
            )
            return

        if result.status == AdapterStatus.REVIEW_REQUIRED:
            update_enrichment_task(
                task_id=task.task_id,
                status="REVIEW_REQUIRED",
                result=result_payload,
                error_type=result.error_type,
                error_message=result.error_message,
                db_path=self.db_path,
            )
            return

        if result.status == AdapterStatus.SKIPPED:
            update_enrichment_task(
                task_id=task.task_id,
                status="SKIPPED",
                result=result_payload,
                error_type=result.error_type,
                error_message=result.error_message,
                db_path=self.db_path,
            )
            return

        if result.status == AdapterStatus.RETRYABLE_ERROR:
            if task.attempt_count < task.max_attempts:
                update_enrichment_task(
                    task_id=task.task_id,
                    status="PENDING",
                    result=result_payload,
                    error_type=result.error_type,
                    error_message=result.error_message,
                    not_before=self._retry_not_before(task),
                    db_path=self.db_path,
                )
                return

            update_enrichment_task(
                task_id=task.task_id,
                status="ERROR",
                result=result_payload,
                error_type=result.error_type or "MAX_ATTEMPTS",
                error_message=result.error_message,
                db_path=self.db_path,
            )
            return

        update_enrichment_task(
            task_id=task.task_id,
            status="ERROR",
            result=result_payload,
            error_type=result.error_type or "PERMANENT_ERROR",
            error_message=result.error_message,
            db_path=self.db_path,
        )

    def _retry_not_before(self, task: EnrichmentTask) -> str:
        delay = self.retry_base_minutes * max(task.attempt_count, 1)
        return (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat()
