"""Offline tests for the read-only Discovery v2 CSV export."""
import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from discovery_v2.export import HEADERS, build_rows, main


class DiscoveryV2ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/'source.sqlite3'
        conn = sqlite3.connect(self.source)
        conn.execute('''CREATE TABLE companies_lite(id INTEGER PRIMARY KEY, company_name TEXT,
            tax_number TEXT, registration_number TEXT, address TEXT, municipality TEXT,
            revenue_2025 REAL, employees_2025 REAL)''')
        conn.executemany('INSERT INTO companies_lite VALUES (?,?,?,?,?,?,?,?)', [
            (1, 'ALFA d.o.o.', '111', '1001', 'Cesta 1', '1000 Ljubljana', 1000.5, 2.5),
            (2, 'BETA d.o.o.', '222', '1002', 'Cesta 2', '2000 Maribor', 2000, 4),
            (3, 'GAMA d.o.o.', '333', '1003', 'Cesta 3', '3000 Celje', None, None),
            (4, 'DELTA d.o.o.', '444', '1004', 'Cesta 4', '4000 Kranj', 4000, 8)])
        conn.commit(); conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def results(self, name, run_id, completed, records):
        path = self.root/name
        conn = sqlite3.connect(path)
        conn.executescript('''CREATE TABLE discovery_runs(run_id TEXT, status TEXT,
            engine_version TEXT, rule_version TEXT);
        CREATE TABLE discovery_run_companies(run_id TEXT, company_id INTEGER, status TEXT,
            selected_attempt_id TEXT);
        CREATE TABLE discovery_company_results(result_id TEXT, run_id TEXT, attempt_id TEXT,
            company_id INTEGER, website_status TEXT, official_website TEXT,
            website_observation_id TEXT, website_evidence_ids_json TEXT,
            default_email_contact_id TEXT, default_phone_contact_id TEXT,
            contact_outcome TEXT, completed_at TEXT);
        CREATE TABLE discovery_contacts(contact_id TEXT, run_id TEXT, attempt_id TEXT,
            company_id INTEGER, contact_type TEXT, normalized_value TEXT,
            primary_observation_id TEXT, supporting_observation_ids_json TEXT,
            attribution_status TEXT, rule_version TEXT, roles_json TEXT);''')
        conn.execute('INSERT INTO discovery_runs VALUES (?,?,?,?)',
                     (run_id, 'COMPLETED', 'engine-1', 'rule-1'))
        for index, record in enumerate(records):
            company_id = record['company_id']; attempt = f'{run_id}-a{index}'
            conn.execute('INSERT INTO discovery_run_companies VALUES (?,?,?,?)',
                         (run_id, company_id, 'COMPLETED', attempt))
            defaults = {}
            for kind in ('email', 'phone'):
                value = record.get(kind)
                contact_id = f'{run_id}-{kind}-{company_id}' if value else None
                defaults[kind] = contact_id
                if value:
                    conn.execute('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (contact_id, run_id, attempt, company_id, kind.upper(), value,
                         f'{kind}-obs-{company_id}', f'["{kind}-obs-{company_id}"]',
                         'ATTRIBUTED', 'contact-rule-1',
                         '["GENERAL"]' if kind == 'email' else '["UNKNOWN"]'))
            conn.execute('INSERT INTO discovery_company_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (f'{run_id}-result-{company_id}', run_id, attempt, company_id,
                 record.get('status', 'VERIFIED'), record.get('website'),
                 f'website-obs-{company_id}', f'["evidence-{company_id}"]',
                 defaults['email'], defaults['phone'], record.get('outcome', 'COMPLETE'),
                 record.get('completed', completed)))
        conn.commit(); conn.close()
        return path

    def test_usable_review_defaults_external_email_and_no_contacts(self):
        results = self.results('results.sqlite3', 'run-1', '2026-01-01T12:00:00+00:00', [
            {'company_id': 1, 'status': 'VERIFIED', 'website': 'https://alfa.si/',
             'email': 'office@external.eu', 'phone': '+38611234567'},
            {'company_id': 2, 'status': 'HIGH', 'website': 'https://beta.si/'},
            {'company_id': 3, 'status': 'MEDIUM', 'website': 'https://gama.si/'},
            {'company_id': 4, 'status': 'REVIEW', 'website': None,
             'email': 'info@delta.si'}])
        before = (self.source.read_bytes(), results.read_bytes())
        rows = build_rows(self.source, [results])
        by_id = {row['company_id']: row for row in rows}
        self.assertEqual([by_id[i]['website_status'] for i in (1, 2, 3, 4)],
                         ['VERIFIED', 'HIGH', 'MEDIUM', 'REVIEW'])
        self.assertEqual(by_id[4]['website'], '')
        self.assertEqual(by_id[1]['default_email'], 'office@external.eu')
        self.assertEqual(by_id[1]['email_attribution_status'], 'ATTRIBUTED')
        self.assertEqual(by_id[1]['default_phone'], '+38611234567')
        self.assertEqual((by_id[2]['default_email'], by_id[2]['default_phone']), ('', ''))
        self.assertEqual(by_id[1]['website_evidence_ids'], '["evidence-1"]')
        self.assertEqual(before, (self.source.read_bytes(), results.read_bytes()))

    def test_multiple_sources_newest_and_explicit_input_precedence(self):
        older = self.results('older.sqlite3', 'old', '2026-01-01T00:00:00+00:00', [
            {'company_id': 1, 'website': 'https://old.alfa.si/'}])
        newer = self.results('newer.sqlite3', 'new', '2026-02-01T00:00:00+00:00', [
            {'company_id': 1, 'website': 'https://new.alfa.si/'},
            {'company_id': 2, 'website': 'https://beta.si/'}])
        rows = build_rows(self.source, [older, newer])
        self.assertEqual({r['company_id']: r['website'] for r in rows},
                         {1: 'https://new.alfa.si/', 2: 'https://beta.si/'})
        explicit = build_rows(self.source, [older, newer], precedence='input-order')
        self.assertEqual({r['company_id']: r['website'] for r in explicit}[1],
                         'https://old.alfa.si/')

    def test_ambiguous_duplicate_requires_explicit_precedence(self):
        first = self.results('first.sqlite3', 'one', '', [
            {'company_id': 1, 'website': 'https://one.si/', 'completed': ''}])
        second = self.results('second.sqlite3', 'two', '', [
            {'company_id': 1, 'website': 'https://two.si/', 'completed': ''}])
        with self.assertRaisesRegex(ValueError, 'completed_at'):
            build_rows(self.source, [first, second])
        rows = build_rows(self.source, [second, first], precedence='input-order')
        self.assertEqual(rows[0]['website'], 'https://two.si/')

    def test_csv_cli_and_explicit_company_selection(self):
        results = self.results('results.sqlite3', 'run-1', '2026-01-01T00:00:00+00:00', [
            {'company_id': 1, 'website': 'https://alfa.si/', 'email': 'info@alfa.si'},
            {'company_id': 2, 'website': None, 'status': 'REVIEW'}])
        output = self.root/'export.csv'
        self.assertEqual(main(['--source', str(self.source), '--results', str(results),
                               '--company-id', '1', '--output', str(output)]), 0)
        with output.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle); rows = list(reader)
        self.assertEqual(reader.fieldnames, HEADERS)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['company_name'], rows[0]['default_email']),
                         ('ALFA d.o.o.', 'info@alfa.si'))

    def test_missing_schema_fails_instead_of_silent_export(self):
        bad = self.root/'bad.sqlite3'; sqlite3.connect(bad).close()
        with self.assertRaisesRegex(ValueError, 'incompatible schema'):
            build_rows(self.source, [bad])


if __name__ == '__main__':
    unittest.main()
