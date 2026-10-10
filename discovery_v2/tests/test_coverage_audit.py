import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from discovery_v2.coverage_audit import (build_audit, deterministic_sample, main,
                                         write_reports)
from discovery_v2.recovery_audit import (build_recovery_audit,
                                         main as recovery_main,
                                         write_recovery_reports)
from discovery_v2.store import APPLICATION_ID, SCHEMA_VERSION


class CoverageAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.master = self.root/'master.db'
        self.current = self.root/'current.sqlite3'
        self.reusable = self.root/'reusable.sqlite3'
        self._master_fixture()
        self._result_fixture(self.current, 'current', current=True)
        self._result_fixture(self.reusable, 'reusable', current=False)

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
        companies = [
            (1, 'Legacy solved', 2_000_000, 12, 'C10'),
            (2, 'Current solved', 3_000_000, 20, 'C25'),
            (3, 'Ambiguous', 800_000, 7, 'G46'),
            (4, 'Tiny no result', 50_000, .2, 'S96'),
            (5, 'Contact gap', 5_000_000, 35, 'C28'),
            (6, 'Bankrupt - v stečaju', 0, 0, 'F41'),
            (7, 'Candidate failed', 1_500_000, 11, 'M71'),
            (8, 'Never selected', 600_000, 4, 'N79'),
        ]
        conn.executemany('''INSERT INTO companies_lite VALUES
          (?,?,?,?,'Road 1','1000 Ljubljana',?,?,?)''',
          [(id_, name, f'SI{id_}', str(id_), revenue, employees, activity)
           for id_, name, revenue, employees, activity in companies])
        # Latest-row semantics: the older ERROR must not hide accepted coverage.
        conn.executemany('INSERT INTO website_discovery VALUES (?,?,?,?,?,?,?)', [
            (1, 1, '', 'ERROR', 'phase2-ownership-1', '[]', '{}'),
            (2, 1, 'https://legacy.si/', 'VERIFIED', 'phase2-ownership-1', '[]',
             json.dumps({'status': 'VERIFIED', 'scope': 'https://legacy.si/'})),
            (3, 3, 'https://candidate.example/', 'REVIEW', 'phase2-ownership-1', '[]', '{}'),
        ])
        attribution = {'publication': 'Legacy solved info@legacy.si',
                       'contact_block': {'target_entity': True, 'other_entity': False},
                       'visible_email': 'info@legacy.si'}
        conn.execute('INSERT INTO email_discovery VALUES (1,1,?,?,?,?)',
                     ('info@legacy.si', 'https://legacy.si/contact', 'Contact',
                      json.dumps(attribution)))
        # It is persisted but not attributable and must not count.
        conn.execute('INSERT INTO email_discovery VALUES (2,3,?,?,?,?)',
                     ('wrong@example.test', 'https://candidate.example/', 'Directory',
                      json.dumps({'publication': 'Other company'})))
        conn.commit()
        conn.close()

    def _empty_result(self, path, run_id):
        conn = sqlite3.connect(path)
        conn.executescript(Path('discovery_v2/schema.sql').read_text())
        conn.execute(f'PRAGMA application_id={APPLICATION_ID}')
        conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        conn.execute('''INSERT INTO discovery_runs VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (run_id, 'DISCOVERY_CONTACTS', 'FRESH_DISCOVERY', 'PARTIAL', 'engine', 'rule',
           '{}', '{}', 'config', 'manifest', '2026-01-01T00:00:00+00:00',
           '2026-01-01T00:00:01+00:00', None))
        return conn

    def _add(self, conn, run_id, company_id, position, company_status,
             *, attempt_status=None, diagnostics=None, website_status=None,
             website='', email='', phone='', contact_outcome='NO_ATTRIBUTED_CONTACT',
             search=False, candidate=False, email_candidate=False,
             snippet_origin=None, result_url='https://candidate.test/',
             eligibility_reason=None):
        eligibility = 'INELIGIBLE' if company_status == 'INELIGIBLE' else 'ELIGIBLE'
        conn.execute('''INSERT INTO discovery_run_companies VALUES (?,?,?,?,?,?,?,?,?)''',
          (run_id, company_id, 'fixture', position, json.dumps({'id': company_id,
           'company_name': f'Company {company_id}', 'tax_number': f'SI{company_id}',
           'registration_number': str(company_id)}), eligibility, eligibility_reason,
           company_status, None))
        if not attempt_status:
            return
        attempt_id = f'{run_id}-a-{company_id}'
        if diagnostics is None:
            diagnostics = {'queries': [], 'website_candidates': []}
        conn.execute('''INSERT INTO discovery_attempts VALUES (?,?,?,?,?,?,?,?,?)''',
          (attempt_id, run_id, company_id, 2 if company_id == 3 else 1, attempt_status,
           '2026-01-01T00:00:02+00:00', '2026-01-01T00:00:03+00:00', '{}',
           diagnostics if isinstance(diagnostics, str) else json.dumps(diagnostics)))
        evidence_id = f'{run_id}-e-{company_id}'
        if search or candidate or email_candidate:
            conn.execute('''INSERT INTO discovery_evidence VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (evidence_id, run_id, attempt_id, company_id,
               'SEARCH_RESULT' if search else 'FETCHED_PAGE', 'fixture', 'UNASSESSED',
               '2026-01-01T00:00:02+00:00', 'LEGAL_NAME_CONTACT', 'query', 1,
               result_url, result_url.split('/')[2], 'Candidate', 'snippet',
               None, None, None, 'hash', '{}'))
        observation = None
        if candidate:
            observation = f'{run_id}-o-web-{company_id}'
            method = {'SNIPPET_URL': 'snippet_url',
                      'SNIPPET_EMAIL_DOMAIN': 'email_domain'}.get(
                          snippet_origin, 'search_url')
            value = {'identity_match': True}
            if snippet_origin:
                value.update(candidate_origin=snippet_origin,
                             source_result_url=result_url, source_result_rank=1)
            conn.execute('''INSERT INTO discovery_observations VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (observation, run_id, attempt_id, company_id, evidence_id,
               'WEBSITE_CANDIDATE', 'candidate.test', 'https://candidate.test/',
               json.dumps(value), method, 'rule',
               'title/snippet' if snippet_origin else 'result_url',
               '2026-01-01T00:00:02+00:00'))
        if email_candidate:
            conn.execute('''INSERT INTO discovery_observations VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (f'{run_id}-o-email-{company_id}', run_id, attempt_id, company_id,
               evidence_id, 'EMAIL_CANDIDATE', 'maybe@candidate.test',
               'maybe@candidate.test', '{}', 'text', 'rule', 'body',
               '2026-01-01T00:00:02+00:00'))
        if website_status is None:
            return
        email_id = phone_id = None
        if email:
            email_id = f'{run_id}-email-{company_id}'
            conn.execute('''INSERT INTO discovery_contacts VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (email_id, run_id, attempt_id, company_id, 'EMAIL', email, email,
               f'{run_id}-o-email-{company_id}', '[]', 'ATTRIBUTED', 'fixture', 'rule',
               '[]', '[]', None, None, None, '{}'))
        if phone:
            phone_id = f'{run_id}-phone-{company_id}'
            conn.execute('''INSERT INTO discovery_contacts VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (phone_id, run_id, attempt_id, company_id, 'PHONE', phone, phone,
               f'{run_id}-o-phone-{company_id}', '[]', 'ATTRIBUTED', 'fixture', 'rule',
               '[]', '[]', None, None, None, '{}'))
        conn.execute('''INSERT INTO discovery_company_results VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (f'{run_id}-r-{company_id}', run_id, attempt_id, company_id, website_status,
           website or None, website or None, observation, '[]',
           json.dumps({'candidates': []}), email_id, phone_id, contact_outcome,
           '2026-01-01T00:00:04+00:00'))
        conn.execute('''UPDATE discovery_run_companies SET selected_attempt_id=?
          WHERE run_id=? AND company_id=?''', (attempt_id, run_id, company_id))

    def _result_fixture(self, path, run_id, current):
        conn = self._empty_result(path, run_id)
        if not current:
            self._add(conn, run_id, 2, 0, 'COMPLETED', attempt_status='COMPLETED',
                      website_status='VERIFIED', website='https://current.si/',
                      email='hello@current.si', phone='+3861234567')
            self._add(conn, run_id, 10, 1, 'PENDING')  # outside master boundary
            conn.commit(); conn.close(); return

        self._add(conn, run_id, 2, 0, 'COMPLETED', attempt_status='COMPLETED',
                  website_status='VERIFIED', website='https://current.si/',
                  email='hello@current.si', phone='+3861234567')
        conn.execute('''INSERT INTO discovery_attempts VALUES (?,?,?,?,?,?,?,?,?)''',
          (f'{run_id}-interrupted-2', run_id, 2, 0, 'INTERRUPTED',
           '2025-12-31T00:00:00+00:00', '2025-12-31T00:01:00+00:00', '{}', '{}'))
        ambiguous = {'queries': [{'status': 'COMPLETED', 'result_count': 2}],
            'website_candidates': [{'status': 'REVIEW',
              'assessment': {'relationship': 'AMBIGUOUS', 'p1a': {}}}]}
        # Historical result must not leak through the selected-attempt join.
        old_attempt = f'{run_id}-old-3'
        conn.execute('''INSERT INTO discovery_attempts VALUES (?,?,?,?,?,?,?,?,?)''',
          (old_attempt, run_id, 3, 1, 'COMPLETED', '2025-01-01', '2025-01-02', '{}', '{}'))
        conn.execute('''INSERT INTO discovery_company_results VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (f'{run_id}-old-result-3', run_id, old_attempt, 3, 'VERIFIED',
           'https://historical.invalid/', 'https://historical.invalid/', None, '[]', '{}',
           None, None, 'NO_ATTRIBUTED_CONTACT', '2025-01-02'))
        self._add(conn, run_id, 3, 1, 'COMPLETED', attempt_status='COMPLETED',
                  diagnostics=ambiguous, website_status='REVIEW', search=True, candidate=True,
                  snippet_origin='SNIPPET_URL',
                  result_url='https://www.bizi.si/AMBIGUOUS/')
        zero = {'queries': [{'status': 'COMPLETED', 'result_count': 0}],
                'website_candidates': []}
        self._add(conn, run_id, 4, 2, 'COMPLETED', attempt_status='COMPLETED',
                  diagnostics=zero, website_status='NOT_FOUND')
        partial = {'queries': [{'status': 'COMPLETED', 'result_count': 1},
                    {'status': 'SKIPPED', 'reason': 'Search query budget exhausted'}],
                   'fetch_errors': ['ReadTimeout: request timed out'],
                   'website_candidates': []}
        self._add(conn, run_id, 5, 3, 'PARTIAL', attempt_status='PARTIAL',
                  diagnostics=partial, website_status='HIGH', website='https://contact-gap.si/',
                  contact_outcome='INCOMPLETE', search=True, email_candidate=True)
        self._add(conn, run_id, 6, 4, 'INELIGIBLE', eligibility_reason='BANKRUPTCY')
        failed = {'queries': [{'status': 'FAILED'}],
                  'fetch_errors': ['ConnectionError: failed to resolve host'],
                  'website_candidates': [{'status': 'REVIEW', 'assessment': {
                    'relationship': 'STANDALONE', 'p1a': {'rule_id': 'OWN-X'}}}]}
        self._add(conn, run_id, 7, 5, 'FAILED', attempt_status='FAILED',
                  diagnostics=failed, search=True, candidate=True,
                  snippet_origin='SNIPPET_EMAIL_DOMAIN',
                  result_url='https://www.companywall.si/podjetje/candidate')
        self._add(conn, run_id, 9, 6, 'PENDING')  # outside master boundary
        conn.commit(); conn.close()

    def build(self):
        return build_audit(self.master, self.current, 'current',
                           [(self.reusable, 'reusable')], sample_size=2, seed='fixed')

    def add_recovery_fixture(self):
        conn = sqlite3.connect(self.current)
        attempt_id = conn.execute('''SELECT selected_attempt_id
          FROM discovery_run_companies WHERE run_id='current' AND company_id=5''').fetchone()[0]
        evidence_rows = [
            ('current-recovery-page-1', 'FETCHED_PAGE', None, None,
             'https://recover.test/about', 'https://recover.test/about'),
            ('current-recovery-page-2', 'FETCHED_PAGE', None, None,
             'https://recover.test/contact', 'https://recover.test/contact'),
            ('current-directory-result', 'SEARCH_RESULT', 'LEGAL_NAME_CONTACT', 2,
             'https://www.bizi.si/COMPANY-5/', None),
        ]
        for evidence_id, source_kind, query_type, rank, result_url, final_url in evidence_rows:
            conn.execute('''INSERT INTO discovery_evidence VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (evidence_id, 'current', attempt_id, 5, source_kind, 'fixture',
               'UNASSESSED', '2026-01-01T00:00:05+00:00', query_type,
               'Company 5 contact' if query_type else None, rank, result_url,
               result_url.split('/')[2], 'Stored recovery evidence', 'snippet',
               result_url if source_kind == 'FETCHED_PAGE' else None, final_url,
               200 if final_url else None, 'recovery-hash', '{}'))

        observations = [
            ('current-recovery-web-1', 'current-recovery-page-1',
             'WEBSITE_CANDIDATE', 'recover.test', 'https://recover.test/',
             {'identity_match': True}, 'canonical_link'),
            ('current-recovery-web-2', 'current-recovery-page-2',
             'WEBSITE_CANDIDATE', 'recover.test', 'https://recover.test/',
             {'identity_match': True}, 'html_link'),
            ('current-recovery-tax', 'current-recovery-page-1',
             'TAX_NUMBER', 'SI5', 'SI5', {'verification_status': 'EXACT_MATCH'}, 'text'),
            ('current-recovery-email-1', 'current-recovery-page-1',
             'EMAIL_CANDIDATE', 'sales@recover.test', 'sales@recover.test', {}, 'text'),
            ('current-recovery-email-2', 'current-recovery-page-2',
             'EMAIL_CANDIDATE', 'sales@recover.test', 'sales@recover.test', {}, 'text'),
            ('current-recovery-raw', 'current-recovery-page-1',
             'EMAIL_CANDIDATE', 'raw@raw.test', 'raw@raw.test', {}, 'text'),
            ('current-recovery-public', 'current-recovery-page-1',
             'EMAIL_CANDIDATE', 'person@gmail.com', 'person@gmail.com', {}, 'text'),
            ('current-directory-web', 'current-directory-result',
             'WEBSITE_CANDIDATE', 'bizi.si', 'https://www.bizi.si/COMPANY-5/',
             {'identity_match': True}, 'search_url'),
        ]
        for observation_id, evidence_id, kind, normalized, raw, value, method in observations:
            conn.execute('''INSERT INTO discovery_observations VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (observation_id, 'current', attempt_id, 5, evidence_id, kind,
               normalized, raw, json.dumps(value), method, 'fixture-rule', 'fixture',
               '2026-01-01T00:00:06+00:00'))
        conn.execute('''INSERT INTO discovery_contacts VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          ('current-recovery-contact', 'current', attempt_id, 5, 'EMAIL',
           'sales@recover.test', 'sales@recover.test', 'current-recovery-email-1',
           json.dumps(['current-recovery-email-1', 'current-recovery-email-2']),
           'ATTRIBUTED', 'fixture', 'fixture-rule', '[]', '[]', None, None, None, '{}'))
        conn.execute('''UPDATE discovery_attempts SET diagnostics_json=?
          WHERE run_id='current' AND company_id=7''',
          (json.dumps({'error': 'ReadTimeout: request timed out',
                       'queries': [{'status': 'COMPLETED', 'result_count': 1}],
                       'fetch_errors': ['ReadTimeout: request timed out']}),))
        conn.commit()
        conn.close()

    def test_population_funnel_cohorts_and_master_boundary(self):
        rows, summary, _, _ = self.build()
        by_id = {row['company_id']: row for row in rows}
        self.assertEqual(summary['funnel'], {
            'total_master_companies': 8,
            'usable_reusable_coverage': 3,
            'official_website_selected': 3,
            'attributable_email_found': 2,
            'attributable_phone_found': 1,
            'website_without_email': 1,
            'search_evidence_without_accepted_website': 2,
            'no_useful_candidate': 3,
            'unresolved_failed': 1,
            'unresolved_ineligible': 1,
            'unresolved_other': 3,
        })
        self.assertEqual({id_: by_id[id_]['cohort'] for id_ in range(1, 9)}, {
            1: 'SOLVED', 2: 'SOLVED', 3: 'AMBIGUOUS', 4: 'LOW_SIGNAL_LOW_VALUE',
            5: 'CONTACT_RECOVERY', 6: 'INELIGIBLE',
            7: 'HIGH_POTENTIAL_RECOVERY', 8: 'SEARCH_RECOVERY'})
        self.assertEqual(summary['master_boundary'], {
            'outside_master_result_company_count': 2,
            'outside_master_result_company_ids': [9, 10]})
        self.assertNotIn('historical.invalid', by_id[3]['official_website'])

    def test_overlapping_evidence_backed_reasons(self):
        rows, _, _, _ = self.build()
        by_id = {row['company_id']: set(row['diagnostic_reasons'].split('|')) for row in rows}
        self.assertTrue({'AMBIGUOUS_IDENTITY', 'CANDIDATES_NOT_VERIFIED',
                         'SEARCH_EVIDENCE_NO_ACCEPTED_WEBSITE'} <= by_id[3])
        self.assertTrue({'DISCOVERY_INCOMPLETE', 'FETCH_TIMEOUT',
                         'SEARCH_BUDGET_EXHAUSTED', 'WEBSITE_NO_ATTRIBUTED_EMAIL',
                         'EMAIL_CANDIDATE_NOT_ATTRIBUTED'} <= by_id[5])
        self.assertTrue({'ATTEMPT_FAILED', 'SEARCH_FAILED', 'FETCH_DNS_FAILURE',
                         'CANDIDATES_NOT_VERIFIED'} <= by_id[7])

    def test_snippet_candidate_support_is_diagnostic_only_and_preserves_provenance(self):
        rows, summary, _, snippet_rows = self.build()
        by_id = {row['company_id']: row for row in rows}

        url_supported = by_id[3]
        self.assertEqual(url_supported['cohort'], 'AMBIGUOUS')
        self.assertEqual(url_supported['official_website'], '')
        self.assertEqual(url_supported['snippet_url_candidate_count'], 1)
        self.assertEqual(url_supported['snippet_email_domain_candidate_count'], 0)
        self.assertEqual(url_supported['snippet_supported_domain_count'], 1)
        self.assertEqual(url_supported['identity_matched_snippet_support_count'], 1)
        self.assertEqual(url_supported['registered_directory_snippet_support_count'], 1)
        self.assertEqual(url_supported['unresolved_snippet_domain_support'], 'YES')
        self.assertTrue({'SNIPPET_URL_CANDIDATE',
                         'SNIPPET_DOMAIN_SUPPORT_UNRESOLVED',
                         'REGISTERED_DIRECTORY_SNIPPET_SUPPORT'} <=
                        set(url_supported['diagnostic_reasons'].split('|')))

        email_supported = by_id[7]
        self.assertEqual(email_supported['cohort'], 'HIGH_POTENTIAL_RECOVERY')
        self.assertEqual(email_supported['official_website'], '')
        self.assertEqual(email_supported['snippet_url_candidate_count'], 0)
        self.assertEqual(email_supported['snippet_email_domain_candidate_count'], 1)
        self.assertEqual(email_supported['unresolved_identity_matched_snippet_support'],
                         'YES')
        self.assertIn('SNIPPET_EMAIL_DOMAIN_CANDIDATE',
                      email_supported['diagnostic_reasons'].split('|'))

        self.assertEqual(summary['snippet_support']['observations'], 2)
        self.assertEqual(summary['snippet_support']['companies_with_snippet_support'], 2)
        self.assertEqual(
            summary['snippet_support']['unresolved_companies_with_snippet_support'], 2)
        self.assertEqual(summary['snippet_support']['origin_observations'], {
            'SNIPPET_EMAIL_DOMAIN': 1, 'SNIPPET_URL': 1})
        self.assertEqual(summary['snippet_support']['publisher_classifications'], {
            'COMPANY_DATABASE': 2})
        self.assertEqual(summary['snippet_support']['cohort_cross_tab']['AMBIGUOUS'][
            'companies_with_snippet_url'], 1)
        self.assertEqual(summary['snippet_support']['cohort_cross_tab'][
            'HIGH_POTENTIAL_RECOVERY']['companies_with_snippet_email_domain'], 1)

        support_by_company = {row['company_id']: row for row in snippet_rows}
        self.assertEqual(support_by_company[3]['candidate_origin'], 'SNIPPET_URL')
        self.assertEqual(support_by_company[3]['candidate_raw_value'], 'candidate.test')
        self.assertEqual(support_by_company[3]['provider'], 'fixture')
        self.assertEqual(support_by_company[3]['query_type'], 'LEGAL_NAME_CONTACT')
        self.assertEqual(support_by_company[3]['query_text'], 'query')
        self.assertEqual(support_by_company[3]['result_rank'], 1)
        self.assertEqual(support_by_company[3]['result_url'],
                         'https://www.bizi.si/AMBIGUOUS/')
        self.assertEqual(support_by_company[3]['publisher_classification'],
                         'COMPANY_DATABASE')
        self.assertTrue(support_by_company[3]['registered_directory_source'])
        self.assertEqual(support_by_company[7]['candidate_origin'],
                         'SNIPPET_EMAIL_DOMAIN')
        self.assertEqual(support_by_company[7]['candidate_raw_value'], 'candidate.test')
        self.assertEqual(support_by_company[7]['evidence_id'], 'current-e-7')
        self.assertEqual(support_by_company[7]['observation_id'], 'current-o-web-7')

    def test_older_artifact_uses_authoritative_extraction_method_without_reparsing(self):
        conn = sqlite3.connect(self.current)
        conn.execute("UPDATE discovery_observations SET value_json='{}' WHERE company_id=3")
        conn.commit(); conn.close()
        rows, _, _, snippet_rows = self.build()
        company = next(row for row in rows if row['company_id'] == 3)
        support = next(row for row in snippet_rows if row['company_id'] == 3)
        self.assertEqual(company['snippet_url_candidate_count'], 1)
        self.assertEqual(company['identity_matched_snippet_support_count'], 0)
        self.assertEqual(support['candidate_origin'], 'SNIPPET_URL')
        self.assertEqual(support['source_locator'], 'title/snippet')

    def test_extraction_method_wins_over_inconsistent_duplicated_origin(self):
        conn = sqlite3.connect(self.current)
        conn.execute('''UPDATE discovery_observations SET value_json=?
            WHERE company_id=3''', (json.dumps({
                'identity_match': True,
                'candidate_origin': 'SNIPPET_EMAIL_DOMAIN'}),))
        conn.commit(); conn.close()
        rows, _, _, snippet_rows = self.build()
        company = next(row for row in rows if row['company_id'] == 3)
        support = next(row for row in snippet_rows if row['company_id'] == 3)
        self.assertEqual(support['candidate_origin'], 'SNIPPET_URL')
        self.assertIn('MALFORMED_STORED_DIAGNOSTICS',
                      company['diagnostic_reasons'].split('|'))

    def test_status_breakdown_distinguishes_manifest_and_attempt_history(self):
        _, summary, _, _ = self.build()
        self.assertEqual(summary['current_run']['company_statuses'], {
            'COMPLETED': 3, 'FAILED': 1, 'INELIGIBLE': 1, 'PARTIAL': 1, 'PENDING': 1})
        self.assertEqual(summary['current_run']['attempt_statuses'], {
            'COMPLETED': 4, 'FAILED': 1, 'INTERRUPTED': 1, 'PARTIAL': 1})

    def test_duplicate_sources_deduplicate_coverage_and_exact_duplicates_fail(self):
        rows, summary, _, _ = self.build()
        self.assertEqual(len(rows), 8)
        self.assertEqual(summary['funnel']['official_website_selected'], 3)
        with self.assertRaisesRegex(ValueError, 'Duplicate Discovery source'):
            build_audit(self.master, self.current, 'current',
                        [(self.current, 'current')])

    def test_in_boundary_identity_mismatch_is_rejected(self):
        conn = sqlite3.connect(self.reusable)
        identity = json.loads(conn.execute('''SELECT identity_snapshot_json
            FROM discovery_run_companies WHERE company_id=2''').fetchone()[0])
        identity['tax_number'] = 'WRONG'
        conn.execute('''UPDATE discovery_run_companies SET identity_snapshot_json=?
            WHERE company_id=2''', (json.dumps(identity),))
        conn.commit(); conn.close()
        with self.assertRaisesRegex(ValueError, 'frozen tax_number'):
            self.build()
        with self.assertRaisesRegex(ValueError, 'frozen tax_number'):
            build_recovery_audit(self.master, self.reusable, 'reusable')

    def test_sampling_is_deterministic_and_seed_sensitive(self):
        rows, _, first, _ = self.build()
        second = deterministic_sample(rows, sample_size=2, seed='fixed')
        third = deterministic_sample(rows, sample_size=2, seed='other')
        self.assertEqual(first, second)
        self.assertEqual([(r['sample_cohort'], r['company_id']) for r in first],
                         [(r['sample_cohort'], r['company_id']) for r in second])
        self.assertNotEqual([r['sample_seed'] for r in first],
                            [r['sample_seed'] for r in third])

    def test_malformed_optional_diagnostics_fail_closed_without_losing_company(self):
        conn = sqlite3.connect(self.current)
        conn.execute("UPDATE discovery_attempts SET diagnostics_json='{' WHERE company_id=7")
        conn.commit(); conn.close()
        rows, _, _, _ = self.build()
        row = next(row for row in rows if row['company_id'] == 7)
        self.assertIn('MALFORMED_STORED_DIAGNOSTICS', row['diagnostic_reasons'])
        self.assertEqual(row['cohort'], 'HIGH_POTENTIAL_RECOVERY')

    def test_inputs_remain_byte_identical_and_no_network_is_reachable(self):
        paths = (self.master, self.current, self.reusable)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            rows, summary, samples, snippet_rows = self.build()
            output = self.root/'reports'
            reports = write_reports(output, rows, summary, samples, snippet_rows, paths)
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in paths})
        self.assertEqual(set(reports), {'coverage_companies.csv',
            'unresolved_companies.csv', 'samples.csv', 'snippet_support.csv',
            'summary.json'})
        self.assertEqual(json.loads((output/'summary.json').read_text())['funnel'],
                         summary['funnel'])
        snippet_header = (output/'snippet_support.csv').read_text(
            encoding='utf-8-sig').splitlines()[0]
        self.assertIn('evidence_id', snippet_header)
        self.assertIn('result_url', snippet_header)
        self.assertNotIn('snippet_body', snippet_header)

    def test_cli_writes_reports_and_refuses_silent_overwrite(self):
        output = self.root/'cli-reports'
        args = ['--master', str(self.master), '--current-results', str(self.current),
                '--current-run-id', 'current', '--reusable-results',
                f'{self.reusable}=reusable', '--output-dir', str(output),
                '--sample-size', '1', '--seed', 'cli']
        self.assertEqual(main(args), 0)
        with self.assertRaises(SystemExit) as stopped:
            main(args)
        self.assertEqual(stopped.exception.code, 2)

    def test_recovery_audit_separates_attributed_raw_public_and_multiple_evidence(self):
        self.add_recovery_fixture()
        partial, failed, summary = build_recovery_audit(
            self.master, self.current, 'current')
        row = next(item for item in partial if item['company_id'] == 5)
        details = {item['domain']: item for item in
                   json.loads(row['candidate_details_json'])}

        self.assertEqual(row['recovery_bucket'], 'STRONG_STORED_WEBSITE_CANDIDATE')
        self.assertEqual(row['attributed_email_domains'], 'recover.test')
        self.assertEqual(row['raw_unattributed_email_domains'],
                         'candidate.test|raw.test')
        self.assertEqual(row['public_email_domains'], 'gmail.com')
        self.assertEqual(row['multi_evidence_email_domains'], 'recover.test')
        self.assertEqual(row['strong_website_candidate_domains'], 'recover.test')
        self.assertEqual(row['strong_email_domain_candidates'], 'recover.test')
        self.assertTrue(details['recover.test']['strong_diagnostic_signal'])
        self.assertEqual(details['recover.test']['independent_evidence_count'], 2)
        self.assertIn('TAX_NUMBER', details['recover.test']['exact_identity_support'])
        self.assertTrue(details['bizi.si']['disallowed_official_domain'])
        self.assertEqual(details['bizi.si']['registered_directory_publishers'],
                         ['bizi.si'])
        self.assertNotIn('bizi.si', row['website_candidate_domains'])
        self.assertEqual(summary['partial']['with_attributed_email_domain_candidate'], 1)
        self.assertEqual(summary['partial']['with_raw_unattributed_email_domain'], 1)
        self.assertEqual(len(failed), 1)

    def test_recovery_audit_classifies_persisted_failure_and_stored_retry_value(self):
        self.add_recovery_fixture()
        _, failed, summary = build_recovery_audit(self.master, self.current, 'current')
        row = next(item for item in failed if item['company_id'] == 7)
        self.assertEqual(row['failure_category'], 'FETCH_TIMEOUT')
        self.assertEqual(row['failure_stage'], 'AFTER_USEFUL_CANDIDATE_EVIDENCE')
        self.assertEqual(row['stored_reinterpretation_first'], 'YES')
        self.assertEqual(row['failed_only_retry_candidate'], 'YES')
        self.assertEqual(row['retry_likely_requires_search'], 'NO')
        self.assertEqual(summary['failed']['failure_categories'], {'FETCH_TIMEOUT': 1})
        self.assertEqual(summary['failed']['potentially_recoverable_stored_only'], 1)

    def test_recovery_audit_is_read_only_offline_and_reports_compact_provenance(self):
        self.add_recovery_fixture()
        paths = (self.master, self.current)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            partial, failed, summary = build_recovery_audit(
                self.master, self.current, 'current')
            output = self.root/'recovery-reports'
            reports = write_recovery_reports(
                output, partial, failed, summary, paths)
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in paths})
        self.assertEqual(set(reports), {'recovery_summary.json',
            'partial_recovery.csv', 'failed_analysis.csv', 'RECOVERY_REPORT.md'})
        header = (output/'partial_recovery.csv').read_text(
            encoding='utf-8-sig').splitlines()[0]
        self.assertIn('candidate_details_json', header)
        self.assertNotIn('snippet_body', header)
        self.assertNotIn('payload_json', header)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            write_recovery_reports(output, partial, failed, summary, paths)

        cli_output = self.root/'recovery-cli'
        args = ['--master', str(self.master), '--results', str(self.current),
                '--run-id', 'current', '--output-dir', str(cli_output)]
        self.assertEqual(recovery_main(args), 0)
        with self.assertRaises(SystemExit) as stopped:
            recovery_main(args)
        self.assertEqual(stopped.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
