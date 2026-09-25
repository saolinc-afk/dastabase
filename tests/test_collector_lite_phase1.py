"""Offline regression tests; temporary databases and synthetic result HTML only."""
import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collector_lite as collector
import database_lite as db


def row(reg="1234567", tax="SI12345678", gvin="123", financials=""):
    return f'''<li class="newsearchIcon"><h3><a href="/detail?CompanyId={gvin}">Example d.o.o.</a></h3>
    <div class="address">Street 1\n1000 Ljubljana</div>
    <span class="registrationNumber">Matična: {reg}</span>
    <span class="taxNumber">Davčna: {tax}</span>{financials}</li>'''


def values(year=None):
    return "".join(f'<div class="advanceResultDataDisplaySubjektFix" '
                   f'title="{label} {year or ""}">{value}</div>'
                   for label, value in (("Prihodki", "1.234,50"), ("Dobiček", "-12,50"), ("Zaposleni", "2,25")))


class ParserTests(unittest.TestCase):
    def test_confirmed_year_and_numbers(self):
        result = collector.parse_company_row(row(financials=values(2025)))
        self.assertEqual((result["revenue_2025"], result["profit_2025"], result["employees_2025"]), (1234.5, -12.5, 2.25))
        self.assertEqual(result["municipality"], "1000 Ljubljana")
        self.assertEqual(result["gvin_detail_url"], "https://www.gvin.com/detail?CompanyId=123")
        self.assertTrue(result["collected_at"])

    def test_configured_year_preserves_raw(self):
        result = collector.parse_company_row(row(financials=values()))
        self.assertEqual(result["revenue_2025"], 1234.5)
        self.assertEqual(result["financial_status"], "OK")
        self.assertEqual(json.loads(result["financial_raw_json"])["cells"][0]["raw"], "1.234,50")

    def test_heading_mapping_survives_reordered_columns(self):
        headings = ["Povprečno št...", "Kapital", "Celotni prih...", "Sredstva", "Čisti poslov..."]
        raw = ["16,36", "8.454.590,36", "777.705,55", "9.071.513,49", "-127.150,37"]
        cells = ''.join(f'<div class="advanceResultDataDisplaySubjektFix">{v}</div>' for v in raw)
        result = collector.parse_company_row(row(financials=cells), headings)
        self.assertEqual(result['assets_2025'], 9071513.49)
        self.assertEqual(result['capital_2025'], 8454590.36)
        self.assertEqual(result['revenue_2025'], 777705.55)
        self.assertEqual(result['profit_2025'], -127150.37)
        self.assertEqual(result['employees_2025'], 16.36)
        self.assertEqual(result['financial_status'], 'OK')
        provenance = json.loads(result['financial_raw_json'])
        self.assertEqual(provenance['headers'], headings)
        self.assertEqual(provenance['configured_year'], 2025)
        self.assertEqual(provenance['year_source'], 'user_configuration')
        self.assertEqual([c['raw'] for c in provenance['cells']], raw)

    def test_partial_and_parse_error(self):
        result = collector.parse_company_row(row(financials=values().replace('2,25', '-')))
        self.assertEqual(result['financial_status'], 'PARTIAL')
        result = collector.parse_company_row(row(financials=values().replace('2,25', 'broken')))
        self.assertEqual(result['financial_status'], 'PARSE_ERROR')
        self.assertIsNone(result['employees_2025'])

    def test_position_alone_is_not_metric_evidence(self):
        html = row(financials=''.join(f'<div class="advanceResultDataDisplaySubjektFix">{v}</div>' for v in ('2025', '10', '5')))
        result = collector.parse_company_row(html)
        self.assertIsNone(result["revenue_2025"])
        self.assertEqual(result["financial_status"], "PARSE_ERROR")

    def test_explicit_column_headings(self):
        html = row(financials=''.join(f'<div class="advanceResultDataDisplaySubjektFix">{v}</div>' for v in ('100', '10', '5')))
        result = collector.parse_company_row(html, ["Prihodki 2025", "Dobiček 2025", "Zaposleni 2025"])
        self.assertEqual(result["employees_2025"], 5)

    def test_optional_elements_and_duplicate_metric(self):
        result = collector.parse_company_row('<h3><a>Example</a></h3>')
        self.assertEqual(result["tax_number"], "")
        self.assertIsNone(result["employees_2025"])
        duplicate = values(2025) + '<div class="advanceResultDataDisplaySubjektFix" title="Revenue 2025">2</div>'
        result = collector.parse_company_row(row(financials=duplicate))
        self.assertIsNone(result["revenue_2025"])

    def test_no_detail_navigation_code(self):
        tree = ast.parse(Path(collector.__file__).read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        self.assertFalse(any(isinstance(n.func, ast.Attribute) and n.func.attr in ('goto', 'new_page') for n in calls))
        self.assertFalse(hasattr(collector, 'enrich_company_from_detail'))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, "DB_PATH", Path(self.temp.name) / "test.db")
        self.patch.start()
        db.initialize_database()
        self.company = collector.parse_company_row(row(financials=values(2025)))

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def records(self):
        conn = db.get_connection()
        try:
            return [dict(r) for r in conn.execute('SELECT * FROM companies_lite')]
        finally:
            conn.close()

    def test_upsert_preserves_id_and_children(self):
        self.assertEqual(db.save_company(self.company), "inserted")
        company_id = self.records()[0]['id']
        conn = db.get_connection()
        conn.execute("INSERT INTO website_discovery(company_id, website) VALUES (?, 'https://example.si')", (company_id,))
        conn.commit()
        conn.close()
        self.company['company_name'] = 'New name'
        self.assertEqual(db.save_company(self.company), "updated")
        self.assertEqual(self.records()[0]['id'], company_id)
        self.assertEqual(self.records()[0]['company_name'], 'New name')
        self.assertEqual(len(self.records()), 1)

    def test_2025_fields_and_raw_persist_on_update(self):
        db.save_company(self.company)
        original_id = self.records()[0]['id']
        updated = dict(self.company, assets_2025=9071513.49, capital_2025=8454590.36, employees_2025=16.36)
        self.assertEqual(db.save_company(updated, return_id=True), ('updated', original_id))
        stored = self.records()[0]
        for key in ('assets_2025', 'capital_2025', 'employees_2025', 'revenue_2025', 'profit_2025', 'financial_raw_json'):
            self.assertEqual(stored[key], updated[key])

    def test_tax_fallback_and_conflict(self):
        first = dict(self.company, registration_number='', gvin_company_id='')
        db.save_company(first)
        original_id = self.records()[0]['id']
        self.assertEqual(db.save_company(self.company), "updated")
        self.assertEqual(self.records()[0]['id'], original_id)
        with self.assertRaises(db.IdentityConflict):
            db.save_company(dict(self.company, tax_number='87654321'))
        self.assertEqual(self.records()[0]['tax_number'], '12345678')

    def test_cross_identity_and_missing_identifiers(self):
        db.save_company(self.company)
        db.save_company(dict(self.company, registration_number='9999999', tax_number='99999999', gvin_company_id='999'))
        with self.assertRaises(db.IdentityConflict):
            db.save_company(dict(self.company, tax_number='99999999'))
        with self.assertRaises(db.IdentityConflict):
            db.save_company(dict(self.company, registration_number='', tax_number='-', gvin_company_id=''))
        self.assertEqual(len(self.records()), 2)

    def test_missing_financials_do_not_delete_existing(self):
        db.save_company(self.company)
        db.save_company(collector.parse_company_row(row()))
        self.assertEqual(self.records()[0]['revenue_2025'], 1234.5)

    def test_interrupted_page_rolls_back(self):
        db.save_company(self.company)
        conn = db.get_connection()
        try:
            with self.assertRaises(KeyboardInterrupt):
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    db.save_company(dict(self.company, company_name='Not committed'), conn)
                    db.save_company(dict(self.company, registration_number='8888888', tax_number='88888888', gvin_company_id='888'), conn)
                    raise KeyboardInterrupt()
        finally:
            conn.close()
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]['company_name'], 'Example d.o.o.')

    def test_additive_migration_preserves_legacy_row(self):
        conn = db.get_connection()
        for column in ('gvin_detail_url', 'financial_raw_json', 'financial_status', 'assets_2025', 'capital_2025'):
            conn.execute(f'ALTER TABLE companies_lite DROP COLUMN {column}')
        conn.execute("INSERT INTO companies_lite(id, company_name, registration_number) VALUES (42, 'Legacy', '7777777')")
        conn.commit()
        conn.close()
        db.initialize_database()
        db.initialize_database()
        self.assertEqual(self.records()[0]['id'], 42)
        db.save_company(dict(self.company, registration_number='7777777'))
        self.assertEqual(self.records()[0]['id'], 42)


if __name__ == '__main__':
    unittest.main()
