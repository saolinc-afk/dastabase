from __future__ import annotations

from pathlib import Path
from typing import Any

from ajpes.eprs_browser import AjpesEprsBrowserSession, PlaywrightTimeoutError
from ajpes.eprs_parser import parse_company_detail
from database_v2 import (
    DB_PATH,
    get_connection,
    normalize_registration_number,
    normalize_tax_number,
)
from worker.models import AdapterContext, AdapterResult, AdapterStatus, EnrichmentTask


SOURCE = "ajpes_eprs"
TASK_TYPE = "company_registry"


class AjpesEprsAdapter:
    source = SOURCE
    task_types = (TASK_TYPE,)

    def __init__(
        self,
        *,
        browser_session: AjpesEprsBrowserSession | None = None,
        db_path: Path | str = DB_PATH,
    ) -> None:
        self.browser_session = browser_session or AjpesEprsBrowserSession()
        self.db_path = db_path

    def execute(
        self,
        task: EnrichmentTask,
        context: AdapterContext,
    ) -> AdapterResult:
        identifiers = self._lookup_identifiers(task, context.db_path)
        registration_number = identifiers.get("registration_number")
        tax_number = identifiers.get("tax_number")

        if not registration_number and not tax_number:
            return AdapterResult(
                status=AdapterStatus.REVIEW_REQUIRED,
                metadata={"reason": "missing_registration_or_tax_number"},
                error_type="MISSING_STRONG_IDENTIFIER",
                error_message="AJPES ePRS lookup requires registration_number or tax_number",
            )

        if not context.consume_source_budget(SOURCE):
            return AdapterResult(
                status=AdapterStatus.RETRYABLE_ERROR,
                metadata={"budget_exhausted": True},
                error_type="SOURCE_BUDGET_EXHAUSTED",
                error_message="AJPES ePRS daily source budget exhausted",
            )

        try:
            lookup = self.browser_session.lookup_company(
                registration_number=registration_number,
                tax_number=None if registration_number else tax_number,
            )
        except PlaywrightTimeoutError as exc:
            return AdapterResult(
                status=AdapterStatus.RETRYABLE_ERROR,
                error_type="AJPES_TIMEOUT",
                error_message=str(exc),
            )
        except Exception as exc:
            return AdapterResult(
                status=AdapterStatus.RETRYABLE_ERROR,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )

        metadata: dict[str, Any] = {
            "lookup": {
                "registration_number": registration_number,
                "tax_number": tax_number,
                "search_status": lookup.search.status,
            },
            "page_diagnostics": lookup.diagnostics or {},
        }

        if lookup.search.status == "LOGIN_REQUIRED":
            return AdapterResult(
                status=AdapterStatus.RETRYABLE_ERROR,
                metadata=metadata,
                error_type="AJPES_LOGIN_REQUIRED",
                error_message="AJPES ePRS session is not logged in or expired",
            )

        if lookup.search.status == "NO_RESULT":
            return AdapterResult(
                status=AdapterStatus.REVIEW_REQUIRED,
                metadata=metadata,
                error_type="AJPES_NO_RESULT",
                error_message="AJPES ePRS returned no result for the strong identifier",
            )

        if len(lookup.search.candidates) != 1 or not lookup.detail_html or not lookup.detail_url:
            metadata["candidates"] = [
                {
                    "name": candidate.name,
                    "registration_number": candidate.registration_number,
                    "tax_number": candidate.tax_number,
                    "detail_url": candidate.detail_url,
                }
                for candidate in lookup.search.candidates
            ]
            return AdapterResult(
                status=AdapterStatus.REVIEW_REQUIRED,
                metadata=metadata,
                error_type="AJPES_AMBIGUOUS_RESULT",
                error_message="AJPES ePRS did not return one unambiguous detail page",
            )

        parsed = parse_company_detail(
            lookup.detail_html,
            detail_url=lookup.detail_url,
            registration_number_hint=registration_number,
            tax_number_hint=tax_number,
        )
        conflict = self._identifier_conflict(task, parsed.company_record, context.db_path)
        metadata["source_entity_id"] = parsed.company_record.external_id
        metadata["raw"] = parsed.raw

        if conflict:
            metadata["identity_conflict"] = conflict
            return AdapterResult(
                status=AdapterStatus.REVIEW_REQUIRED,
                metadata=metadata,
                error_type="IDENTITY_CONFLICT",
                error_message=conflict,
            )

        return AdapterResult(
            status=AdapterStatus.SUCCESS,
            company_records=[parsed.company_record],
            metadata=metadata,
        )

    def _lookup_identifiers(
        self,
        task: EnrichmentTask,
        db_path: Path | str,
    ) -> dict[str, str | None]:
        registration_number = normalize_registration_number(
            task.payload.get("registration_number")
        )
        tax_number = normalize_tax_number(task.payload.get("tax_number"))

        if task.company_id is not None:
            with get_connection(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT registration_number, tax_number
                    FROM companies
                    WHERE company_id = ?
                    """,
                    (task.company_id,),
                ).fetchone()
            if row is not None:
                registration_number = registration_number or normalize_registration_number(
                    row["registration_number"]
                )
                tax_number = tax_number or normalize_tax_number(row["tax_number"])

        return {
            "registration_number": registration_number,
            "tax_number": tax_number,
        }

    def _identifier_conflict(
        self,
        task: EnrichmentTask,
        record,
        db_path: Path | str,
    ) -> str | None:
        if task.company_id is None:
            return None

        with get_connection(db_path) as conn:
            row = conn.execute(
                """
                SELECT registration_number, tax_number
                FROM companies
                WHERE company_id = ?
                """,
                (task.company_id,),
            ).fetchone()

        if row is None:
            return f"Task company_id {task.company_id} does not exist"

        existing_registration = normalize_registration_number(row["registration_number"])
        existing_tax = normalize_tax_number(row["tax_number"])

        if (
            existing_registration
            and record.registration_number
            and existing_registration != record.registration_number
        ):
            return (
                "AJPES registration_number conflicts with task company: "
                f"{existing_registration} != {record.registration_number}"
            )

        if existing_tax and record.tax_number and existing_tax != record.tax_number:
            return (
                "AJPES tax_number conflicts with task company: "
                f"{existing_tax} != {record.tax_number}"
            )

        return None


__all__ = ["AjpesEprsAdapter"]
