"""Tests for the read-only 4xxx/5xxx production master export."""
import csv
import hashlib
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from discovery_v2.master_export import build, main, write_output


class MasterExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.sqlite3'
        self._make_source()
        self.pilot = self._make_results(
            'pilot.sqlite3', 'pilot-run', [(0, 'COMPLETED', {
                'website': 'https://discovery-alfa.si/', 'website_status': 'HIGH',
                'email': 'new@alfa.si', 'phone': '+38640111000',
            })],
        )
        self.batch100 = self._make_results(
            'batch100.sqlite3', 'batch-run', [(1, 'PARTIAL', {
                'website': 'https://candidate-beta.si/', 'website_status': 'REVIEW',
                'email': 'sales@external-mail.example', 'phone': '+38640222000',
            })],
        )
        self.big459 = self._make_results(
            'big459.sqlite3', 'big-run', [(2, 'FAILED', None)],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _make_source(self, *, canonical_phone=False):
        connection = sqlite3.connect(self.source)
        phone = ', phone TEXT' if canonical_phone else ''
        connection.executescript(f'''CREATE TABLE companies_lite(
            id INTEGER PRIMARY KEY, company_name TEXT, tax_number TEXT,
            registration_number TEXT, address TEXT, municipality TEXT,
            revenue_2025 REAL, employees_2025 REAL{phone});
        CREATE TABLE website_discovery(
            id INTEGER PRIMARY KEY, company_id INTEGER, website TEXT, status TEXT);
        CREATE TABLE email_discovery(
            id INTEGER PRIMARY KEY, company_id INTEGER, email TEXT, confidence TEXT);''')
        values = [
            (0, '=ALFA d.o.o.', '111', '1000', 'Cesta 1', '4000 Kranj', 10, 1),
            (1, 'BETA d.o.o.', '222', '1001', 'Cesta 2', '5000 Nova Gorica', 20, 2),
            (2, 'GAMA d.o.o.', '333', '1002', 'Cesta 3', '4001 Kranj', 30, 3),
            (3, 'DELTA d.o.o.', '444', '1003', 'Cesta 4', '5999 Drugje', 40, 4),
            (4, 'OUTSIDE d.o.o.', '555', '1004', 'Cesta 5', '1000 Ljubljana', 50, 5),
        ]
        if canonical_phone:
            values = [(*value, '+38640999000' if value[0] == 1 else None) for value in values]
        placeholders = ','.join('?' for _ in values[0])
        connection.executemany(f'INSERT INTO companies_lite VALUES ({placeholders})', values)
        connection.executemany('INSERT INTO website_discovery VALUES (?,?,?,?)', [
            (1, 0, 'https://old-alfa.si/', 'VERIFIED'),
            (2, 0, 'https://newest-old-alfa.si/', 'VERIFIED'),
            (3, 1, 'https://unaccepted-beta.si/', 'REVIEW'),
            (4, 2, 'https://sparrow-gama.si/', 'VERIFIED'),
        ])
        connection.executemany('INSERT INTO email_discovery VALUES (?,?,?,?)', [
            (1, 0, 'z@alfa.si', 'HIGH'),
            (2, 0, 'a@alfa.si', 'VERIFIED'),
            (3, 4, 'outside@example.si', 'HIGH'),
        ])
        connection.commit()
        connection.close()

    def _make_results(self, filename, run_id, records):
        path = self.root / filename
        connection = sqlite3.connect(path)
        connection.executescript('''CREATE TABLE discovery_runs(run_id TEXT);
        CREATE TABLE discovery_run_companies(
            run_id TEXT, company_id INTEGER, status TEXT,
            selected_attempt_id TEXT, manifest_position INTEGER);
        CREATE TABLE discovery_company_results(
            result_id TEXT, run_id TEXT, attempt_id TEXT, company_id INTEGER,
            official_website TEXT, website_status TEXT, contact_outcome TEXT,
            default_email_contact_id TEXT, default_phone_contact_id TEXT,
            completed_at TEXT);
        CREATE TABLE discovery_contacts(
            contact_id TEXT, run_id TEXT, attempt_id TEXT, company_id INTEGER,
            contact_type TEXT, normalized_value TEXT, attribution_status TEXT,
            roles_json TEXT);''')
        connection.execute('INSERT INTO discovery_runs VALUES (?)', (run_id,))
        for position, (company_id, status, result) in enumerate(records):
            attempt = f'{run_id}-attempt-{position}' if result is not None else None
            connection.execute(
                'INSERT INTO discovery_run_companies VALUES (?,?,?,?,?)',
                (run_id, company_id, status, attempt, position),
            )
            if result is None:
                continue
            email_id = self._contact(connection, run_id, attempt, company_id, 'EMAIL',
                                     result.get('email'), 'GENERAL')
            phone_id = self._contact(connection, run_id, attempt, company_id, 'PHONE',
                                     result.get('phone'), 'UNKNOWN')
            connection.execute(
                'INSERT INTO discovery_company_results VALUES (?,?,?,?,?,?,?,?,?,?)',
                (f'{run_id}-result-{company_id}', run_id, attempt, company_id,
                 result.get('website'), result.get('website_status'),
                 result.get('contact_outcome', 'COMPLETE'), email_id, phone_id,
                 result.get('completed_at', '2026-09-01T10:00:00+00:00')),
            )
        connection.commit()
        connection.close()
        return path

    @staticmethod
    def _contact(connection, run_id, attempt, company_id, kind, value, role):
        if not value:
            return None
        contact_id = f'{run_id}-{kind.lower()}-{company_id}'
        connection.execute(
            'INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?)',
            (contact_id, run_id, attempt, company_id, kind, value,
             'ATTRIBUTED', f'["{role}"]'),
        )
        return contact_id

    @staticmethod
    def _digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _build(self):
        return build(self.source, self.pilot, self.batch100, self.big459,
                     expected_total=4, expected_selected=3)

    def test_combines_population_defaults_precedence_and_stats(self):
        before = {path: self._digest(path) for path in
                  (self.source, self.pilot, self.batch100, self.big459)}
        rows, stats, phone_column, _ = self._build()
        by_id = {row['company_id']: row for row in rows}

        self.assertEqual([row['company_id'] for row in rows], [0, 2, 1, 3])
        self.assertEqual(by_id[0]['sparrow_emails'], 'a@alfa.si | z@alfa.si')
        self.assertEqual(by_id[0]['sparrow_email_confidences'], 'VERIFIED | HIGH')
        self.assertEqual(by_id[0]['best_email'], 'a@alfa.si | z@alfa.si')
        self.assertEqual(by_id[0]['best_email_source'], 'SPARROW_0.9')
        self.assertEqual(by_id[0]['new_discovery_email'], 'NO')
        self.assertEqual(by_id[0]['best_website'], 'https://discovery-alfa.si/')
        self.assertEqual(by_id[0]['best_website_source'], 'DISCOVERY_V2')
        self.assertEqual(by_id[1]['new_discovery_email'], 'YES')
        self.assertEqual(by_id[1]['best_email'], 'sales@external-mail.example')
        self.assertEqual(by_id[1]['discovery_email_role'], 'GENERAL')
        self.assertEqual(by_id[1]['best_website'], '')
        self.assertEqual(by_id[2]['best_website'], 'https://sparrow-gama.si/')
        self.assertEqual(by_id[2]['best_website_source'], 'SPARROW_0.9')
        self.assertEqual(by_id[2]['discovery_v2_run_status'], 'FAILED')
        self.assertEqual(by_id[3]['discovery_v2_selected'], 'NO')
        self.assertEqual(by_id[3]['discovery_v2_batch'], '')
        self.assertEqual(by_id[1]['best_phone'], '+38640222000')
        self.assertIsNone(phone_column)
        self.assertEqual(stats, {
            'TOTAL_45XX': 4, 'OLD_EMAIL_COMPANIES': 1,
            'NO_OLD_EMAIL_COMPANIES': 3, 'DISCOVERY_SELECTED': 3,
            'DISCOVERY_RESULTS': 2, 'DISCOVERY_FAILED': 1,
            'NEW_DISCOVERY_EMAILS': 1, 'DISCOVERY_DEFAULT_PHONES': 2,
            'DISCOVERY_USABLE_WEBSITES': 1, 'VERIFIED': 0, 'HIGH': 1,
            'MEDIUM': 0, 'REVIEW': 1,
        })
        self.assertEqual(before, {path: self._digest(path) for path in before})

    def test_csv_is_bom_safe_has_review_columns_and_cli_reports_stats(self):
        output = self.root / 'master.csv'
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([
                '--source', str(self.source), '--pilot', str(self.pilot),
                '--batch100', str(self.batch100), '--big459', str(self.big459),
                '--output', str(output), '--expected-total', '4',
                '--expected-selected', '3',
            ])
        self.assertEqual(code, 0)
        self.assertTrue(output.read_bytes().startswith(b'\xef\xbb\xbf'))
        with output.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['company_name'], "'=ALFA d.o.o.")
        self.assertEqual(rows[0]['audit_note'], '')
        self.assertIn('TOTAL_45XX=4', stdout.getvalue())
        self.assertIn('NEW_DISCOVERY_EMAILS=1', stdout.getvalue())

    def test_all_usable_discovery_website_statuses_are_accepted(self):
        for status in ('VERIFIED', 'HIGH', 'MEDIUM'):
            connection = sqlite3.connect(self.pilot)
            connection.execute(
                'UPDATE discovery_company_results SET website_status=?', (status,)
            )
            connection.commit()
            connection.close()
            rows, _, _, _ = self._build()
            alfa = next(row for row in rows if row['company_id'] == 0)
            self.assertEqual(alfa['best_website'], 'https://discovery-alfa.si/')
            self.assertEqual(alfa['best_website_source'], 'DISCOVERY_V2')

    def test_overlap_and_population_invariants_fail_closed(self):
        overlap = self._make_results(
            'overlap.sqlite3', 'overlap-run', [(0, 'COMPLETED', None)],
        )
        with self.assertRaisesRegex(ValueError, 'batch overlap'):
            build(self.source, self.pilot, overlap, self.big459, 4, 3)
        with self.assertRaisesRegex(ValueError, 'TOTAL_45XX expected 839, found 4'):
            build(self.source, self.pilot, self.batch100, self.big459)
        with self.assertRaisesRegex(ValueError, 'DISCOVERY_SELECTED expected 4, found 3'):
            build(self.source, self.pilot, self.batch100, self.big459, 4, 4)

    def test_missing_schema_and_invalid_default_contact_fail_closed(self):
        bad = self.root / 'bad.sqlite3'
        sqlite3.connect(bad).close()
        with self.assertRaisesRegex(ValueError, 'incompatible schema'):
            build(self.source, bad, self.batch100, self.big459, 4, 3)

        connection = sqlite3.connect(self.pilot)
        connection.execute("UPDATE discovery_contacts SET attribution_status='UNATTRIBUTED'")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(ValueError, 'invalid persisted default'):
            self._build()

    def test_output_cannot_replace_an_input_database(self):
        rows, _, phone_column, inputs = self._build()
        with self.assertRaisesRegex(ValueError, 'must differ'):
            write_output(self.source, rows, phone_column, inputs)

    def test_optional_canonical_phone_precedes_discovery_phone(self):
        for path in (self.source, self.pilot, self.batch100, self.big459):
            path.unlink()
        self._make_source(canonical_phone=True)
        self.pilot = self._make_results('pilot.sqlite3', 'pilot-run', [
            (0, 'COMPLETED', {'website_status': 'REVIEW'})])
        self.batch100 = self._make_results('batch100.sqlite3', 'batch-run', [
            (1, 'COMPLETED', {'website_status': 'REVIEW', 'phone': '+38640222000'})])
        self.big459 = self._make_results('big459.sqlite3', 'big-run', [
            (2, 'FAILED', None)])
        rows, _, phone_column, _ = self._build()
        beta = next(row for row in rows if row['company_id'] == 1)
        self.assertEqual(phone_column, 'phone')
        self.assertEqual(beta['canonical_phone'], '+38640999000')
        self.assertEqual(beta['best_phone'], '+38640999000')
        self.assertEqual(beta['best_phone_source'], 'CANONICAL')


if __name__ == '__main__':
    unittest.main()
