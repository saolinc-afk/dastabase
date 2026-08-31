from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ajpes.eprs_browser import AjpesLookupResult
from ajpes.eprs_parser import SearchCandidate, SearchResult, parse_company_detail
from database_v2 import (
    create_enrichment_task,
    get_connection,
    initialize_database,
    resolve_or_create_company,
    set_source_budget,
)
from worker.adapters.ajpes_eprs import AjpesEprsAdapter
from worker.registry import AdapterRegistry
from worker.runner import WorkerRunner


DETAIL_URL = "https://www.ajpes.si/prs/podjetjeSRG.asp?s=1&e=999001"
DETAIL_HTML = """
<html><body>
<div id="accordianBody1">
  <div class="row"><div class="col-sm-5 text-thin">Status subjekta</div><div class="col-sm-7">vpisan</div></div>
  <div class="row"><div class="col-sm-5 text-thin">Datum vpisa subjekta v sodni register</div><div class="col-sm-7">20.08.2025</div></div>
  <div class="row"><div class="col-sm-5 text-thin">Matična številka</div><div class="col-sm-7">7305800000</div></div>
  <div class="row"><div class="col-sm-5 text-thin">Ident. št. za DDV in davčna številka</div><div class="col-sm-7">SI 74465422</div></div>
  <div class="row"><div class="col-sm-5 text-thin">Firma</div><div class="col-sm-7">Example Registry d.o.o.</div></div>
  <div class="row"><div class="col-sm-5 text-thin">Skrajšana firma</div><div class="col-sm-7">Example d.o.o.</div></div>
  <div class="row"><div class="col-sm-5 text-thin">Poslovni naslov</div><div class="col-sm-7">Company Street 1, Ljubljana</div></div>
  <div class="row"><div class="col-sm-5 text-thin">Pravnoorganizacijska oblika</div><div class="col-sm-7">Družba z omejeno odgovornostjo d.o.o.</div></div>
  <div class="row"><div class="col-sm-5 text-thin">Osnovni kapital</div><div class="col-sm-7">7.500,00 EUR</div></div>
</div>
<div id="accordianBody2"><div class="row form-gray-print">
  <div class="col-md-6"><h4>DRUŽBENIKI</h4>
    <div style="page-break-inside: avoid;">
      <div class="row"><div class="col-sm-5 text-thin">Zap. št. družbenika</div><div class="col-sm-7">1203519</div></div>
      <div class="row"><div class="col-sm-5 text-thin">Identifikacijska številka</div><div class="col-sm-7">59507942 - pravna oseba ni vpisana v PRS</div></div>
      <div class="row"><div class="col-sm-5 text-thin">Firma</div><div class="col-sm-7">EXAMPLE HOLDING GMBH</div></div>
      <div class="row"><div class="col-sm-5 text-thin">Naslov</div><div class="col-sm-7">Legal Entity Street 2, Berlin</div></div>
      <div class="row"><div class="col-sm-5 text-thin">Datum vstopa</div><div class="col-sm-7">15.05.2025</div></div>
    </div>
    <div class="row"><div class="col-sm-7"><a href="rezultati_osebe.asp?tip=PO&amp;po_tip=4&amp;po_davcna=59507942">POVEZANE OSEBE</a></div></div>
  </div>
  <div class="col-md-6"><h4>POSLOVNI DELEŽI</h4>
    <div style="page-break-inside: avoid;">
      <div class="row"><div class="col-sm-5 text-thin">Zap. št. deleža</div><div class="col-sm-7">317075</div></div>
      <div class="row"><div class="col-sm-5 text-thin">Osnovni vložek</div><div class="col-sm-7">7.500,00 EUR</div></div>
      <div class="row"><div class="col-sm-5 text-thin">Delež v odstotku ali ulomku</div><div class="col-sm-7">100%</div></div>
      <div class="row"><div class="col-sm-5 text-thin">Imetniki</div><div class="col-sm-7">Zap.št. družbenika 1203519, EXAMPLE HOLDING GMBH</div></div>
    </div>
  </div>
</div></div>
<div id="accordianBody3">
  <div class="matchHeight">
    <div class="row"><div class="col-sm-5 text-thin">Zap. št. zastopnika</div><div class="col-sm-7">855641</div></div>
    <div class="row"><div class="col-sm-5 text-thin">Vrsta zastopnika</div><div class="col-sm-7">direktor</div></div>
    <div class="row"><div class="col-sm-5 text-thin">Osebno ime</div><div class="col-sm-7">REDACTED PERSON</div></div>
    <div class="row"><div class="col-sm-5 text-thin">Naslov</div><div class="col-sm-7">Personal Street 9, Redacted</div></div>
    <div class="row"><div class="col-sm-5 text-thin">Datum podelitve pooblastila</div><div class="col-sm-7">15.05.2025</div></div>
    <div class="row"><div class="col-sm-5 text-thin">Način zastopanja</div><div class="col-sm-7">samostojno</div></div>
    <a href="rezultati_osebe.asp?tip=FO&amp;fo_vir=3&amp;fo_tip=4&amp;fo_enota=999001&amp;fo_zapst=855641">POVEZANE OSEBE</a>
  </div>
  <div class="matchHeight">
    <div class="row"><div class="col-sm-5 text-thin">Zap. št. zastopnika</div><div class="col-sm-7">877978</div></div>
    <div class="row"><div class="col-sm-5 text-thin">Vrsta zastopnika</div><div class="col-sm-7">prokurist</div></div>
    <div class="row"><div class="col-sm-5 text-thin">Osebno ime</div><div class="col-sm-7">OTHER REDACTED</div></div>
    <div class="row"><div class="col-sm-5 text-thin">Datum podelitve pooblastila</div><div class="col-sm-7">06.05.2026</div></div>
  </div>
</div>
<div id="accordianBody4"></div>
<a href="podjetje.asp?s=1&amp;e=999001&amp;p=1">VPOGLED V PRS</a>
</body></html>
"""


class FakeAjpesBrowser:
    def __init__(self, lookup: AjpesLookupResult):
        self.lookup = lookup
        self.calls = 0

    def lookup_company(self, *, registration_number=None, tax_number=None):
        self.calls += 1
        return self.lookup


def ok_lookup() -> AjpesLookupResult:
    return AjpesLookupResult(
        search=SearchResult(
            status="OK",
            candidates=[
                SearchCandidate(
                    name="Example Registry d.o.o.",
                    registration_number="7305800000",
                    tax_number="74465422",
                    detail_url=DETAIL_URL,
                )
            ],
        ),
        detail_url=DETAIL_URL,
        detail_html=DETAIL_HTML,
        diagnostics={
            "ajpes_pages_before_acquire": 1,
            "ajpes_pages_after_acquire": 2,
            "ajpes_pages_after_lookup": 2,
            "lookup_opened_new_ajpes_pages": False,
            "single_dedicated_page_reused": True,
        },
    )


class AjpesEprsAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "database_v2.db"
        initialize_database(self.db_path)
        set_source_budget(source="ajpes_eprs", daily_limit=None, db_path=self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def runner(self, lookup: AjpesLookupResult) -> WorkerRunner:
        registry = AdapterRegistry()
        registry.register(
            AjpesEprsAdapter(
                browser_session=FakeAjpesBrowser(lookup),
                db_path=self.db_path,
            )
        )
        return WorkerRunner(registry=registry, worker_id="ajpes-test", db_path=self.db_path)

    def create_company(self, registration_number: str = "7305800000", tax_number: str = "74465422") -> int:
        return resolve_or_create_company(
            canonical_name="Local Company",
            registration_number=registration_number,
            tax_number=tax_number,
            source="test",
            db_path=self.db_path,
        ).company_id

    def test_company_parse(self) -> None:
        parsed = parse_company_detail(DETAIL_HTML, detail_url=DETAIL_URL)

        self.assertEqual(parsed.company_record.canonical_name, "Example Registry d.o.o.")
        self.assertEqual(parsed.company_record.registration_number, "7305800000")
        self.assertEqual(parsed.company_record.tax_number, "74465422")
        self.assertEqual(parsed.company_record.address, "Company Street 1, Ljubljana")

    def test_physical_person_director_valid_from(self) -> None:
        people = parse_company_detail(DETAIL_HTML, detail_url=DETAIL_URL).company_record.people

        director = people[0].roles[0]
        self.assertEqual(director.title, "direktor")
        self.assertEqual(director.valid_from, "2025-05-15")
        self.assertEqual(director.source_role_id, "855641")

    def test_same_company_multiple_role_records(self) -> None:
        record = parse_company_detail(DETAIL_HTML, detail_url=DETAIL_URL).company_record
        titles = sorted(person.roles[0].title for person in record.people)

        self.assertEqual(titles, ["direktor", "prokurist"])

    def test_legal_entity_shareholder_and_share(self) -> None:
        record = parse_company_detail(DETAIL_HTML, detail_url=DETAIL_URL).company_record
        ownership = record.ownership[0]

        self.assertEqual(ownership.owner_type, "LEGAL_ENTITY")
        self.assertEqual(ownership.owner_raw_name, "EXAMPLE HOLDING GMBH")
        self.assertEqual(ownership.source_shareholder_id, "1203519")
        self.assertEqual(ownership.source_share_id, "317075")
        self.assertEqual(ownership.share_raw, "100%")
        self.assertEqual(ownership.share_percent, 100.0)
        self.assertEqual(ownership.nominal_value_raw, "7.500,00 EUR")

    def test_unresolved_legal_entity_owner_does_not_create_fake_company(self) -> None:
        company_id = self.create_company()
        create_enrichment_task(
            source="ajpes_eprs",
            task_type="company_registry",
            company_id=company_id,
            db_path=self.db_path,
        )

        self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            companies = conn.execute("SELECT COUNT(*) AS count FROM companies").fetchone()
            ownership = conn.execute("SELECT owner_company_id, owner_raw_name FROM company_ownership").fetchone()
        self.assertEqual(companies["count"], 1)
        self.assertIsNone(ownership["owner_company_id"])
        self.assertEqual(ownership["owner_raw_name"], "EXAMPLE HOLDING GMBH")

    def test_idempotent_repeated_enrichment(self) -> None:
        company_id = self.create_company()
        for _ in range(2):
            create_enrichment_task(
                source="ajpes_eprs",
                task_type="company_registry",
                company_id=company_id,
                db_path=self.db_path,
            )
            self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            counts = {
                "companies": conn.execute("SELECT COUNT(*) AS count FROM companies").fetchone()["count"],
                "roles": conn.execute("SELECT COUNT(*) AS count FROM company_person_roles").fetchone()["count"],
                "ownership": conn.execute("SELECT COUNT(*) AS count FROM company_ownership").fetchone()["count"],
                "source_identity": conn.execute("SELECT COUNT(*) AS count FROM company_source_identities").fetchone()["count"],
            }
        self.assertEqual(counts["companies"], 1)
        self.assertEqual(counts["roles"], 2)
        self.assertEqual(counts["ownership"], 1)
        self.assertEqual(counts["source_identity"], 1)

    def test_repeated_enrichment_reuses_same_source_record(self) -> None:
        company_id = self.create_company()
        for _ in range(2):
            create_enrichment_task(
                source="ajpes_eprs",
                task_type="company_registry",
                company_id=company_id,
                db_path=self.db_path,
            )
            self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM source_records").fetchone()
        self.assertEqual(row["count"], 1)

    def test_identifier_conflict_goes_to_review_required(self) -> None:
        company_id = self.create_company(registration_number="1111111000")
        task = create_enrichment_task(
            source="ajpes_eprs",
            task_type="company_registry",
            company_id=company_id,
            db_path=self.db_path,
        )

        self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT status, error_type FROM enrichment_tasks WHERE task_id = ?", (task.task_id,)).fetchone()
        self.assertEqual(row["status"], "REVIEW_REQUIRED")
        self.assertEqual(row["error_type"], "IDENTITY_CONFLICT")

    def test_missing_identifier_goes_to_review_required(self) -> None:
        task = create_enrichment_task(
            source="ajpes_eprs",
            task_type="company_registry",
            payload={},
            db_path=self.db_path,
        )

        self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT status, error_type FROM enrichment_tasks WHERE task_id = ?", (task.task_id,)).fetchone()
        self.assertEqual(row["status"], "REVIEW_REQUIRED")
        self.assertEqual(row["error_type"], "MISSING_STRONG_IDENTIFIER")

    def test_personal_address_does_not_become_profile_field(self) -> None:
        company_id = self.create_company()
        create_enrichment_task(
            source="ajpes_eprs",
            task_type="company_registry",
            company_id=company_id,
            db_path=self.db_path,
        )
        self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            people_columns = [row["name"] for row in conn.execute("PRAGMA table_info(people)").fetchall()]
            evidence = conn.execute("SELECT evidence_json FROM company_person_roles WHERE source_role_id = '855641'").fetchone()
        self.assertNotIn("address", people_columns)
        self.assertIn("[REDACTED]", evidence["evidence_json"])
        self.assertIn("personal_address_present", evidence["evidence_json"])

    def test_foreign_key_check_is_clean(self) -> None:
        company_id = self.create_company()
        create_enrichment_task(
            source="ajpes_eprs",
            task_type="company_registry",
            company_id=company_id,
            db_path=self.db_path,
        )
        self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(rows, [])

    def test_source_entity_id_becomes_source_identity(self) -> None:
        company_id = self.create_company()
        create_enrichment_task(
            source="ajpes_eprs",
            task_type="company_registry",
            company_id=company_id,
            db_path=self.db_path,
        )
        self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT source, external_id FROM company_source_identities").fetchone()
        self.assertEqual(row["source"], "ajpes_eprs")
        self.assertEqual(row["external_id"], "999001")

    def test_relation_ids_are_not_global_person_ids(self) -> None:
        company_id = self.create_company()
        create_enrichment_task(
            source="ajpes_eprs",
            task_type="company_registry",
            company_id=company_id,
            db_path=self.db_path,
        )
        self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            person_ids = [row["person_id"] for row in conn.execute("SELECT person_id FROM people ORDER BY person_id").fetchall()]
            role_ids = [row["source_role_id"] for row in conn.execute("SELECT source_role_id FROM company_person_roles ORDER BY source_role_id").fetchall()]
        self.assertNotIn(855641, person_ids)
        self.assertEqual(role_ids, ["855641", "877978"])

    def test_repeated_lookup_does_not_duplicate_current_ownership_record(self) -> None:
        company_id = self.create_company()
        for _ in range(2):
            create_enrichment_task(
                source="ajpes_eprs",
                task_type="company_registry",
                company_id=company_id,
                db_path=self.db_path,
            )
            self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT source_shareholder_id, source_share_id, COUNT(*) AS count
                FROM company_ownership
                WHERE is_current = 1 AND valid_to IS NULL
                GROUP BY source_shareholder_id, source_share_id
                """
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 1)

    def test_single_dedicated_page_diagnostics_are_preserved(self) -> None:
        company_id = self.create_company()
        task = create_enrichment_task(
            source="ajpes_eprs",
            task_type="company_registry",
            company_id=company_id,
            db_path=self.db_path,
        )
        self.runner(ok_lookup()).execute_once()

        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT result_json FROM enrichment_tasks WHERE task_id = ?", (task.task_id,)).fetchone()
        self.assertIn('"lookup_opened_new_ajpes_pages": false', row["result_json"])
        self.assertIn('"single_dedicated_page_reused": true', row["result_json"])


if __name__ == "__main__":
    unittest.main()
