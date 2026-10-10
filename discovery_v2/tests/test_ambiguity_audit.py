import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from discovery_v2.ambiguity_audit import (
    build_ambiguity_audit, deterministic_sample, main, write_reports,
)
from discovery_v2.store import APPLICATION_ID, SCHEMA_VERSION


class AmbiguityAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.master = self.root/'master.db'
        self.results = self.root/'results.sqlite3'
        self._master_fixture()
        self._results_fixture()

    def tearDown(self):
        self.temporary.cleanup()

    def _master_fixture(self):
        conn = sqlite3.connect(self.master)
        conn.executescript('''
          CREATE TABLE companies_lite(
            id INTEGER PRIMARY KEY,company_name TEXT,tax_number TEXT,
            registration_number TEXT,address TEXT,municipality TEXT,
            revenue_2025 REAL,employees_2025 REAL,activity TEXT);
          CREATE TABLE website_discovery(
            id INTEGER PRIMARY KEY,company_id INTEGER,website TEXT,status TEXT,
            rule_version TEXT,evidence_json TEXT,ownership_json TEXT);
          CREATE TABLE email_discovery(
            id INTEGER PRIMARY KEY,company_id INTEGER,email TEXT,page_url TEXT,
            page_title TEXT,evidence_json TEXT);
        ''')
        names = ('Alpha','Beta','Gamma','Delta','Echo','Foxtrot','Golf','Hotel')
        conn.executemany('''INSERT INTO companies_lite VALUES
          (?,?,?,?,?,?,1000000,10,'C25')''', [
              (number, name+' d.o.o.', f'SI{number}', str(number),
               f'Road {number}', '1000 Ljubljana')
              for number, name in enumerate(names, 1)])
        conn.commit()
        conn.close()

    def _results_fixture(self):
        conn = sqlite3.connect(self.results)
        conn.executescript(Path('discovery_v2/schema.sql').read_text())
        conn.execute(f'PRAGMA application_id={APPLICATION_ID}')
        conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        conn.execute('''INSERT INTO discovery_runs VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          ('run', 'DISCOVERY_CONTACTS', 'FRESH_DISCOVERY', 'COMPLETED',
           'engine', 'rule', '{}', '{}', 'config', 'manifest',
           '2026-01-01', '2026-01-02', None))
        for company_id in range(1, 9):
            attempt = f'attempt-{company_id}'
            conn.execute('''INSERT INTO discovery_run_companies VALUES
              (?,?,?,?,?,?,?,?,?)''',
              ('run', company_id, 'fixture', company_id,
               json.dumps({'id': company_id,
                            'company_name': ('Alpha','Beta','Gamma','Delta','Echo',
                                             'Foxtrot','Golf','Hotel')[company_id-1]+' d.o.o.',
                            'tax_number': f'SI{company_id}',
                            'registration_number': str(company_id)}),
               'ELIGIBLE', None, 'PARTIAL', attempt))
            conn.execute('''INSERT INTO discovery_attempts VALUES
              (?,?,?,?,?,?,?,?,?)''',
              (attempt, 'run', company_id, 1, 'PARTIAL', '2026-01-01',
               '2026-01-02', '{}', json.dumps({'queries': [],
                'website_candidates': [], 'error':
                'Discovery incomplete; partial evidence retained'})))
            conn.execute('''INSERT INTO discovery_company_results VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (f'result-{company_id}', 'run', attempt, company_id, 'REVIEW',
               None, None, None, '[]', '{}', None, None, 'INCOMPLETE',
               '2026-01-02'))

        self._candidate(conn, 1, 'alpha.si', rank=1)
        conn.execute('''INSERT INTO discovery_observations VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          ('o-1-alpha-contact', 'run', 'attempt-1', 1, 'e-1-alpha-si',
           'WEBSITE_CANDIDATE', 'alpha.si', 'https://alpha.si/contact',
           json.dumps({'identity_match': True}), 'snippet_url', 'rule',
           'title/snippet', '2026-01-01'))
        self._candidate(conn, 1, 'shop.alpha.si', rank=2)

        self._candidate(conn, 2, 'beta.si', exact=('TAX_NUMBER',), rank=1)
        self._candidate(conn, 2, 'beta-alt.com', rank=2)
        self._candidate(conn, 2, 'bizi.si', rank=3)

        self._candidate(conn, 3, 'gamma.si', exact=('ADDRESS',), rank=1)
        self._candidate(conn, 3, 'gamma.com', exact=('ADDRESS',), rank=1)

        self._candidate(conn, 4, 'delta.si', exact=('REGISTRATION_NUMBER',), rank=2)
        self._candidate(conn, 4, 'delta-candidate.com', rank=1)

        echo_evidence = self._candidate(
            conn, 5, 'echo.si', exact=('LEGAL_NAME',), rank=2)
        self._email(conn, 5, echo_evidence, 'sales@echo.si', attributed=True)
        self._candidate(conn, 5, 'echo-company.com', exact=('LEGAL_NAME',), rank=1)

        self._candidate(conn, 6, 'foxtrot.si', exact=('ADDRESS',), rank=1)
        self._candidate(conn, 6, 'foxtrot.hr', exact=('ADDRESS',), rank=1)

        self._candidate(conn, 7, 'golf.si', conflict=('TAX_NUMBER',), rank=1)
        self._candidate(conn, 7, 'golf-company.com', exact=('ADDRESS',), rank=2)

        raw_evidence = self._candidate(conn, 8, 'random-one.com', rank=1)
        self._email(conn, 8, raw_evidence, 'raw@random-one.com')
        self._candidate(conn, 8, 'random-two.net', rank=2)
        conn.commit()
        conn.close()

    def _candidate(self, conn, company_id, domain, *, exact=(), conflict=(), rank=1):
        attempt = f'attempt-{company_id}'
        suffix = domain.replace('.', '-')
        evidence = f'e-{company_id}-{suffix}'
        observation = f'o-{company_id}-{suffix}'
        url = f'https://{domain}/company'
        conn.execute('''INSERT INTO discovery_evidence VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (evidence, 'run', attempt, company_id, 'SEARCH_RESULT', 'fixture',
           'UNASSESSED', '2026-01-01', 'LEGAL_NAME_CONTACT',
           f'Company {company_id}', rank, url, domain, 'Result', 'snippet',
           None, None, None, f'hash-{suffix}', '{}'))
        conn.execute('''INSERT INTO discovery_observations VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (observation, 'run', attempt, company_id, evidence,
           'WEBSITE_CANDIDATE', domain, f'https://{domain}/',
           json.dumps({'identity_match': True}), 'search_url', 'rule',
           'result_url', '2026-01-01'))
        for index, kind in enumerate(exact):
            conn.execute('''INSERT INTO discovery_observations VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (f'{observation}-exact-{index}', 'run', attempt, company_id,
               evidence, kind, f'value-{kind}', f'value-{kind}',
               json.dumps({'verification_status': 'EXACT_MATCH'}),
               'structured_identity', 'rule', 'title/snippet', '2026-01-01'))
        for index, kind in enumerate(conflict):
            conn.execute('''INSERT INTO discovery_observations VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (f'{observation}-conflict-{index}', 'run', attempt, company_id,
               evidence, kind, f'wrong-{kind}', f'wrong-{kind}',
               json.dumps({'verification_status': 'CONFLICT'}),
               'structured_identity', 'rule', 'title/snippet', '2026-01-01'))
        return evidence

    def _email(self, conn, company_id, evidence, email, attributed=False):
        attempt = f'attempt-{company_id}'
        observation = f'o-email-{company_id}-{email.replace("@", "-")}'
        conn.execute('''INSERT INTO discovery_observations VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (observation, 'run', attempt, company_id, evidence,
           'EMAIL_CANDIDATE', email, email, '{}', 'search_snippet', 'rule',
           'title/snippet', '2026-01-01'))
        if attributed:
            conn.execute('''INSERT INTO discovery_contacts VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (f'contact-{company_id}', 'run', attempt, company_id, 'EMAIL',
               email, email, observation, json.dumps([observation]), 'ATTRIBUTED',
               'fixture', 'rule', '[]', '[]', None, None, None, '{}'))

    def build(self):
        return build_ambiguity_audit(
            self.master, self.results, 'run', sample_size=100, seed='fixture')

    def test_ambiguity_categories_and_same_domain_variants(self):
        rows, samples, summary = self.build()
        by_id = {row['company_id']: row for row in rows}
        self.assertEqual(len(rows), 8)
        self.assertEqual(by_id[1]['diagnostic_category'], 'SAME_DOMAIN_VARIANTS')
        self.assertEqual(by_id[1]['plausible_host_count'], 2)
        self.assertEqual(by_id[1]['plausible_registrable_domain_count'], 1)
        self.assertEqual(by_id[1]['same_domain_variants'], 'alpha.si')
        self.assertEqual(by_id[1]['same_host_url_variant_domain_count'], 1)
        self.assertEqual(by_id[1]['all_candidate_url_count'], 3)
        self.assertEqual(by_id[1]['selected_result_id'], 'result-1')
        self.assertEqual(by_id[1]['selected_website_status'], 'REVIEW')
        self.assertEqual(by_id[1]['selected_contact_outcome'], 'INCOMPLETE')
        self.assertEqual(summary['candidate_cardinality'], {
            'EXACTLY_2': 8, '3_TO_5': 0, '6_TO_10': 0, 'OVER_10': 0})
        self.assertEqual(len(samples), 8)

    def test_directory_is_noise_and_exact_identity_is_clear_leader(self):
        rows, _, summary = self.build()
        row = next(item for item in rows if item['company_id'] == 2)
        details = {item['domain']: item for item in json.loads(
            row['candidate_details_json'])}
        self.assertEqual(row['diagnostic_category'], 'ONE_CLEAR_EVIDENCE_LEADER')
        self.assertEqual(row['leader_domain'], 'beta.si')
        self.assertTrue(details['beta.si']['exact_tax'])
        self.assertEqual(details['bizi.si']['candidate_type'],
                         'DIRECTORY_OR_COMPANY_DATABASE')
        self.assertTrue(details['bizi.si']['blocked_as_official'])
        self.assertEqual(row['plausible_host_count'], 2)
        self.assertEqual(row['all_candidate_host_count'], 3)
        self.assertEqual(summary['companies_by_candidate_type'][
            'DIRECTORY_OR_COMPANY_DATABASE'], 1)
        self.assertEqual(summary['category_by_cardinality'][
            'ONE_CLEAR_EVIDENCE_LEADER']['EXACTLY_2'], 3)

    def test_two_close_candidates_and_registration_leader(self):
        rows, _, _ = self.build()
        by_id = {row['company_id']: row for row in rows}
        self.assertEqual(by_id[3]['diagnostic_category'], 'TWO_CLOSE_CANDIDATES')
        self.assertEqual(by_id[4]['diagnostic_category'],
                         'ONE_CLEAR_EVIDENCE_LEADER')
        self.assertEqual(by_id[4]['leader_domain'], 'delta.si')

    def test_attributed_email_leader_is_separate_from_raw_email(self):
        rows, _, _ = self.build()
        row = next(item for item in rows if item['company_id'] == 5)
        details = {item['domain']: item for item in json.loads(
            row['candidate_details_json'])}
        self.assertEqual(row['leader_domain'], 'echo.si')
        self.assertTrue(details['echo.si']['attributed_email_domain_agreement'])
        self.assertFalse(details['echo.si']['raw_email_domain_agreement'])
        self.assertFalse(details['echo-company.com'][
            'attributed_email_domain_agreement'])

    def test_cross_country_collision_conflicts_and_no_support(self):
        rows, _, _ = self.build()
        by_id = {row['company_id']: row for row in rows}
        self.assertEqual(by_id[6]['diagnostic_category'], 'CROSS_COUNTRY_COLLISION')
        details = {item['domain']: item for item in json.loads(
            by_id[7]['candidate_details_json'])}
        self.assertTrue(details['golf.si']['identifier_conflict'])
        self.assertLess(details['golf.si']['score'],
                        details['golf-company.com']['score'])
        self.assertEqual(by_id[8]['diagnostic_category'],
                         'NO_MEANINGFUL_IDENTITY_SUPPORT')
        raw = {item['domain']: item for item in json.loads(
            by_id[8]['candidate_details_json'])}
        self.assertTrue(raw['random-one.com']['raw_email_domain_agreement'])
        self.assertFalse(raw['random-one.com'][
            'attributed_email_domain_agreement'])

    def test_sample_is_deterministic_and_stratified(self):
        rows, first, _ = self.build()
        second = deterministic_sample(rows, 5, 'same')
        third = deterministic_sample(rows, 5, 'same')
        self.assertEqual(second, third)
        self.assertEqual(len(first), 8)
        self.assertEqual({row['sample_stratum'] for row in first},
                         {row['diagnostic_category'] for row in rows})

    def test_read_only_zero_network_outputs_and_cli(self):
        paths = (self.master, self.results)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            rows, samples, summary = self.build()
            output = self.root/'reports'
            reports = write_reports(output, rows, samples, summary, paths)
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in paths})
        self.assertEqual(set(reports), {'ambiguity_summary.json',
            'ambiguity_companies.csv', 'ambiguity_sample_100.csv',
            'AMBIGUITY_REPORT.md'})
        header = (output/'ambiguity_companies.csv').read_text(
            encoding='utf-8-sig').splitlines()[0]
        self.assertNotIn('snippet_body', header)
        self.assertNotIn('evidence_payload_json', header)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            write_reports(output, rows, samples, summary, paths)

        cli_output = self.root/'cli'
        args = ['--master', str(self.master), '--results', str(self.results),
                '--run-id', 'run', '--output-dir', str(cli_output),
                '--seed', 'fixture']
        self.assertEqual(main(args), 0)
        with self.assertRaises(SystemExit) as stopped:
            main(args)
        self.assertEqual(stopped.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
