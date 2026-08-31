from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database_v2 import (
    can_use_source,
    create_enrichment_task,
    get_connection,
    get_source_usage,
    initialize_database,
    needs_enrichment,
    resolve_or_create_company,
    resolve_or_create_person,
    set_source_budget,
    update_enrichment_task,
)
from worker.adapters.fake import FakeAdapter
from worker.registry import AdapterRegistry
from worker.runner import WorkerRunner


def make_runner(db_path: Path) -> WorkerRunner:
    registry = AdapterRegistry()
    registry.register(FakeAdapter())
    return WorkerRunner(
        registry=registry,
        worker_id="test-worker",
        db_path=db_path,
        retry_base_minutes=1,
    )


def get_task(db_path: Path, task_id: int):
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM enrichment_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()


def create_company(db_path: Path, registration_number: str) -> int:
    return resolve_or_create_company(
        canonical_name=f"Company {registration_number}",
        registration_number=registration_number,
        source="test",
        db_path=db_path,
    ).company_id


class WorkerFoundationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "database_v2.db"
        initialize_database(self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_success_ingests_company_fact(self) -> None:
        task = create_enrichment_task(
            source="fake",
            task_type="success",
            payload={
                "canonical_name": "Foundation Test",
                "registration_number": "1234567000",
                "fact_key": "worker_foundation_score",
                "fact_value": 42,
            },
            db_path=self.db_path,
        )

        make_runner(self.db_path).execute_once()

        row = get_task(self.db_path, task.task_id)
        self.assertEqual(row["status"], "DONE")
        self.assertEqual(row["attempt_count"], 1)

        with get_connection(self.db_path) as conn:
            fact = conn.execute(
                """
                SELECT value_number
                FROM company_current_facts
                WHERE fact_key = ?
                """,
                ("worker_foundation_score",),
            ).fetchone()
        self.assertEqual(fact["value_number"], 42)

    def test_review_required_keeps_evidence(self) -> None:
        task = create_enrichment_task(
            source="fake",
            task_type="review",
            payload={"candidates": [{"name": "A"}, {"name": "B"}]},
            db_path=self.db_path,
        )

        make_runner(self.db_path).execute_once()

        row = get_task(self.db_path, task.task_id)
        self.assertEqual(row["status"], "REVIEW_REQUIRED")
        self.assertEqual(row["error_type"], "AMBIGUOUS_CANDIDATES")
        self.assertIn("ambiguous fake candidates", row["result_json"])

    def test_retryable_error_requeues_then_errors_at_max_attempts(self) -> None:
        task = create_enrichment_task(
            source="fake",
            task_type="retryable",
            max_attempts=2,
            db_path=self.db_path,
        )
        runner = make_runner(self.db_path)

        runner.execute_once()
        row = get_task(self.db_path, task.task_id)
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["attempt_count"], 1)
        self.assertIsNotNone(row["not_before"])

        update_enrichment_task(
            task_id=task.task_id,
            status="PENDING",
            result={},
            not_before=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            db_path=self.db_path,
        )
        runner.execute_once()

        row = get_task(self.db_path, task.task_id)
        self.assertEqual(row["status"], "ERROR")
        self.assertEqual(row["attempt_count"], 2)
        self.assertEqual(row["error_type"], "FAKE_TEMPORARY_ERROR")

    def test_permanent_error_goes_to_error(self) -> None:
        task = create_enrichment_task(
            source="fake",
            task_type="permanent_error",
            db_path=self.db_path,
        )

        make_runner(self.db_path).execute_once()

        row = get_task(self.db_path, task.task_id)
        self.assertEqual(row["status"], "ERROR")
        self.assertEqual(row["error_type"], "FAKE_PERMANENT_ERROR")

    def test_budget_blocks_claim_without_losing_task(self) -> None:
        set_source_budget(source="fake", daily_limit=2, db_path=self.db_path)

        companies = [create_company(self.db_path, f"99000000{i}") for i in range(3)]
        tasks = [
            create_enrichment_task(
                source="fake",
                task_type="budget",
                company_id=company_id,
                db_path=self.db_path,
            )
            for company_id in companies
        ]
        runner = make_runner(self.db_path)

        runner.execute_once()
        runner.execute_once()
        third = runner.execute_once()

        usage = get_source_usage("fake", db_path=self.db_path)
        self.assertEqual(usage.used_count, 2)
        self.assertIs(can_use_source("fake", db_path=self.db_path), False)
        self.assertIsNone(third)

        row = get_task(self.db_path, tasks[2].task_id)
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["attempt_count"], 0)

    def test_freshness_uses_recent_done_tasks(self) -> None:
        company_id = create_company(self.db_path, "880000001")
        task = create_enrichment_task(
            source="fake",
            task_type="success",
            company_id=company_id,
            db_path=self.db_path,
        )
        update_enrichment_task(
            task_id=task.task_id,
            status="DONE",
            result={},
            db_path=self.db_path,
        )

        self.assertIs(
            needs_enrichment(
                company_id=company_id,
                source="fake",
                task_type="success",
                max_age_days=30,
                db_path=self.db_path,
            ),
            False,
        )

        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                UPDATE enrichment_tasks
                SET finished_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (old_timestamp, old_timestamp, task.task_id),
            )

        self.assertIs(
            needs_enrichment(
                company_id=company_id,
                source="fake",
                task_type="success",
                max_age_days=30,
                db_path=self.db_path,
            ),
            True,
        )

    def test_active_dedupe_allows_new_task_after_done(self) -> None:
        company_id = create_company(self.db_path, "770000001")

        first = create_enrichment_task(
            source="fake",
            task_type="success",
            company_id=company_id,
            db_path=self.db_path,
        )
        duplicate = create_enrichment_task(
            source="fake",
            task_type="success",
            company_id=company_id,
            db_path=self.db_path,
        )

        self.assertEqual(duplicate.task_id, first.task_id)
        self.assertIs(duplicate.created, False)

        update_enrichment_task(
            task_id=first.task_id,
            status="DONE",
            result={},
            db_path=self.db_path,
        )
        next_task = create_enrichment_task(
            source="fake",
            task_type="success",
            company_id=company_id,
            db_path=self.db_path,
        )

        self.assertIs(next_task.created, True)
        self.assertNotEqual(next_task.task_id, first.task_id)

    def test_person_task_without_company_assumption(self) -> None:
        person_id = resolve_or_create_person(
            full_name="Test Person",
            email="test.person@example.com",
            db_path=self.db_path,
        ).person_id
        task = create_enrichment_task(
            source="fake",
            task_type="person",
            person_id=person_id,
            payload={"full_name": "Test Person", "email": "test.person@example.com"},
            db_path=self.db_path,
        )

        make_runner(self.db_path).execute_once()

        row = get_task(self.db_path, task.task_id)
        self.assertEqual(row["status"], "DONE")
        self.assertIsNone(row["company_id"])
        self.assertEqual(row["person_id"], person_id)

    def test_foreign_key_check_is_clean(self) -> None:
        company_id = create_company(self.db_path, "660000001")
        person_id = resolve_or_create_person(
            full_name="FK Person",
            db_path=self.db_path,
        ).person_id
        create_enrichment_task(
            source="fake",
            task_type="success",
            company_id=company_id,
            person_id=person_id,
            db_path=self.db_path,
        )

        with get_connection(self.db_path) as conn:
            rows = conn.execute("PRAGMA foreign_key_check").fetchall()

        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
