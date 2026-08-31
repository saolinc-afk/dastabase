from __future__ import annotations

from ingestion.models import CompanyRecord, FactRecord, PersonRecord
from worker.models import AdapterContext, AdapterResult, AdapterStatus, EnrichmentTask


class FakeAdapter:
    source = "fake"
    task_types = (
        "success",
        "review",
        "retryable",
        "permanent_error",
        "budget",
        "person",
    )

    def execute(
        self,
        task: EnrichmentTask,
        context: AdapterContext,
    ) -> AdapterResult:
        if task.task_type == "success":
            return AdapterResult(
                status=AdapterStatus.SUCCESS,
                company_records=[
                    CompanyRecord(
                        source=task.source,
                        canonical_name=task.payload.get(
                            "canonical_name",
                            "Fake Company",
                        ),
                        registration_number=task.payload.get("registration_number"),
                        tax_number=task.payload.get("tax_number"),
                        facts=[
                            FactRecord(
                                fact_key=task.payload.get("fact_key", "fake_score"),
                                value=task.payload.get("fact_value", 1),
                            )
                        ],
                    )
                ],
                metadata={"mode": "success"},
            )

        if task.task_type == "review":
            return AdapterResult(
                status=AdapterStatus.REVIEW_REQUIRED,
                metadata={
                    "reason": "ambiguous fake candidates",
                    "candidates": task.payload.get("candidates", []),
                    "evidence": task.payload.get("evidence", {}),
                },
                error_type="AMBIGUOUS_CANDIDATES",
                error_message="Fake adapter requires review",
            )

        if task.task_type == "retryable":
            return AdapterResult(
                status=AdapterStatus.RETRYABLE_ERROR,
                error_type="FAKE_TEMPORARY_ERROR",
                error_message="Fake retryable error",
            )

        if task.task_type == "permanent_error":
            return AdapterResult(
                status=AdapterStatus.PERMANENT_ERROR,
                error_type="FAKE_PERMANENT_ERROR",
                error_message="Fake permanent error",
            )

        if task.task_type == "budget":
            if not context.consume_source_budget(task.source):
                return AdapterResult(
                    status=AdapterStatus.RETRYABLE_ERROR,
                    metadata={"budget_exhausted": True},
                    error_type="SOURCE_BUDGET_EXHAUSTED",
                    error_message="Daily source budget exhausted",
                )

            return AdapterResult(
                status=AdapterStatus.SUCCESS,
                metadata={"external_access": True},
            )

        if task.task_type == "person":
            return AdapterResult(
                status=AdapterStatus.SUCCESS,
                person_records=[
                    PersonRecord(
                        full_name=task.payload.get("full_name", "Fake Person"),
                        email=task.payload.get("email"),
                    )
                ],
                metadata={"mode": "person"},
            )

        return AdapterResult(
            status=AdapterStatus.SKIPPED,
            error_type="UNSUPPORTED_FAKE_TASK",
            error_message=f"Unsupported fake task type: {task.task_type}",
        )
