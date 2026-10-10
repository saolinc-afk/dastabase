import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from discovery_v2.ambiguity_review import (
    build_review, main, stratified_sample, write_review,
)
from discovery_v2.store import APPLICATION_ID, SCHEMA_VERSION


class AmbiguityReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.master = self.root/'master.db'
        self.results = self.root/'results.sqlite3'
        self.companies = self.root/'ambiguity_companies.csv'
        categories = (
            [('NO_MEANINGFUL_IDENTITY_SUPPORT', 35),
             ('THIRD_PARTY_NOISE_DOMINATES', 25),
             ('ONE_MODERATE_LEADER', 25),
             ('ONE_CLEAR_EVIDENCE_LEADER', 20),
             ('MANY_LOW_SIGNAL_CANDIDATES', 3),
             ('CROSS_COUNTRY_COLLISION', 3),
             ('TWO_CLOSE_CANDIDATES', 3),
             ('SAME_DOMAIN_VARIANTS', 1)]
        )
        self.rows = []
        company_id = 1
        for category, count in categories:
            for _ in range(count):
                self.rows.append(self._row(company_id, category))
                company_id += 1
        self._master_fixture()
        self._results_fixture()
        self._write_companies()

    def tearDown(self):
        self.temporary.cleanup()

    def _row(self, company_id, category):
        candidates = []
        for index in range(10):
            domain = f'candidate-{company_id}-{index}.example'
            candidates.append({
                'domain': domain, 'urls': [f'https://{domain}/path/{index}'],
                'candidate_type': ('DIRECTORY_OR_COMPANY_DATABASE' if index == 9
                                   else 'UNCLASSIFIED_DOMAIN'),
                'score': 100-index, 'exact_tax': index == 0,
                'exact_registration': False, 'exact_legal_name': index < 2,
                'exact_address': index == 0, 'exact_street': False,
                'exact_postal': index == 0, 'exact_municipality': index == 0,
                'attributed_phone_agreement': False,
                'attributed_email_domain_agreement': index == 0,
                'raw_email_domain_agreement': index == 1,
                'independent_evidence_count': 1,
                'query_occurrence_count': index+1,
                'minimum_search_rank': index+1,
                'blocked_as_official': index == 9,
                'identifier_conflict': False, 'address_conflict': False,
                'entity_conflict': False, 'foreign_country_domain': index == 8,
                'name_or_rank_only': index > 1,
                'evidence_ids': [f'evidence-{company_id}'],
            })
        return {
            'company_id': company_id, 'company_name': f'Company {company_id}',
            'tax_number': f'SI{company_id}',
            'registration_number': str(company_id),
            'address': f'Road {company_id}', 'municipality': '1000 Ljubljana',
            'attempt_id': f'attempt-{company_id}', 'accepted_website': '',
            'plausible_host_count': 10,
            'plausible_registrable_domain_count': 10,
            'all_candidate_host_count': 10,
            'candidate_cardinality_band': '6_TO_10',
            'diagnostic_category': category,
            'leader_domain': f'candidate-{company_id}-0.example',
            'leader_score': 100, 'runner_up_score': 99, 'leader_margin': 1,
            'same_domain_variants': '', 'foreign_country_candidate_count': 1,
            'known_third_party_candidate_count': 1,
            'meaningful_identity_candidate_count': 1,
            'candidate_type_counts_json': '{}',
            'negative_signal_counts_json': '{}',
            'candidate_details_json': json.dumps(candidates),
        }

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
        conn.executemany('''INSERT INTO companies_lite VALUES
          (?,?,?,?,?,?,1000000,10,'C25')''', [
              (row['company_id'], row['company_name'], row['tax_number'],
               row['registration_number'], row['address'], row['municipality'])
              for row in self.rows])
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
        for row in self.rows:
            company_id = row['company_id']; attempt = row['attempt_id']
            conn.execute('''INSERT INTO discovery_run_companies VALUES
              (?,?,?,?,?,?,?,?,?)''',
              ('run', company_id, 'fixture', company_id,
               json.dumps({'id':company_id}), 'ELIGIBLE', None, 'PARTIAL', attempt))
            conn.execute('''INSERT INTO discovery_attempts VALUES
              (?,?,?,?,?,?,?,?,?)''',
              (attempt, 'run', company_id, 1, 'PARTIAL', '2026-01-01',
               '2026-01-02', '{}', '{}'))
            conn.execute('''INSERT INTO discovery_evidence VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (f'evidence-{company_id}', 'run', attempt, company_id,
               'SEARCH_RESULT', 'fixture', 'UNASSESSED', '2026-01-01',
               'LEGAL_NAME_CONTACT', f'query company {company_id}', 1,
               f'https://publisher-{company_id}.example/',
               f'publisher-{company_id}.example', 'T'*300, 'S'*500,
               None, None, None, f'hash-{company_id}', '{}'))
        conn.commit()
        conn.close()

    def _write_companies(self):
        headers = list(self.rows[0])
        with self.companies.open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(self.rows)

    def test_requested_stratification_and_stable_seed(self):
        review, summary = build_review(
            self.master, self.results, 'run', self.companies,
            seed='stable', max_candidates=6)
        self.assertEqual(len(review), 100)
        self.assertEqual(summary['category_counts'], {
            'CROSS_COUNTRY_COLLISION': 3,
            'MANY_LOW_SIGNAL_CANDIDATES': 3,
            'NO_MEANINGFUL_IDENTITY_SUPPORT': 30,
            'ONE_CLEAR_EVIDENCE_LEADER': 20,
            'ONE_MODERATE_LEADER': 20,
            'SAME_DOMAIN_VARIANTS': 1,
            'THIRD_PARTY_NOISE_DOMINATES': 20,
            'TWO_CLOSE_CANDIDATES': 3,
        })
        again, _ = build_review(self.master, self.results, 'run', self.companies,
                                seed='stable', max_candidates=6)
        self.assertEqual([row['canonical_company_id'] for row in review],
                         [row['canonical_company_id'] for row in again])
        changed = stratified_sample([
            {**row, '_candidates': json.loads(row['candidate_details_json'])}
            for row in self.rows], 'different')
        self.assertNotEqual([row['canonical_company_id'] for row in review],
                            [row['company_id'] for row in changed])

    def test_existing_sufficient_sample_is_reused(self):
        parsed = [{**row, '_candidates': json.loads(row['candidate_details_json'])}
                  for row in self.rows]
        selected_ids = {row['company_id']
                        for row in stratified_sample(parsed, 'existing')}
        existing = self.root/'ambiguity_sample_100.csv'
        headers = list(self.rows[0])
        with existing.open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(row for row in self.rows
                             if row['company_id'] in selected_ids)
        review, summary = build_review(
            self.master, self.results, 'run', self.companies,
            existing_sample=existing, seed='ignored', max_candidates=6)
        self.assertEqual(summary['sample_source'], 'EXISTING_SAMPLE')
        self.assertEqual({int(row['canonical_company_id']) for row in review},
                         selected_ids)

    def test_candidate_and_evidence_text_truncation(self):
        review, _ = build_review(
            self.master, self.results, 'run', self.companies,
            seed='stable', max_candidates=5)
        row = review[0]
        self.assertEqual(len(row['CANDIDATES_COMPACT'].splitlines()), 5)
        self.assertLessEqual(len(row['EVIDENCE_PREVIEW'].split('title=', 1)[1]
                                 .split(' | snippet=', 1)[0]), 120)
        self.assertIn('…', row['EVIDENCE_PREVIEW'])
        self.assertNotIn('T'*121, row['EVIDENCE_PREVIEW'])
        self.assertNotIn('S'*221, row['EVIDENCE_PREVIEW'])

    def test_read_only_zero_network_no_source_mutation_and_outputs(self):
        sources = (self.master, self.results, self.companies)
        before = {path:hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sources}
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            review, summary = build_review(
                self.master, self.results, 'run', self.companies,
                seed='stable', max_candidates=6)
            output = self.root/'review'
            outputs = write_review(output, review, summary)
        self.assertEqual(before, {path:hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in sources})
        self.assertEqual(set(outputs), {'ambiguity_human_review_100.csv',
                                       'AMBIGUITY_HUMAN_REVIEW_GUIDE.md'})
        exported = list(csv.DictReader((output/'ambiguity_human_review_100.csv')
            .open(newline='', encoding='utf-8-sig')))
        self.assertEqual(len(exported), 100)
        self.assertTrue(all(not row['ANDREJ_RESULT'] and
                            not row['ANDREJ_NOTES'] for row in exported))
        with self.assertRaisesRegex(ValueError, 'already exists'):
            write_review(output, review, summary)

    def test_cli_and_invalid_candidate_limit(self):
        output = self.root/'cli'
        args = ['--master', str(self.master), '--results', str(self.results),
                '--run-id', 'run', '--ambiguity-companies', str(self.companies),
                '--output-dir', str(output), '--seed', 'stable',
                '--max-candidates', '5']
        self.assertEqual(main(args), 0)
        with self.assertRaises(SystemExit) as stopped:
            main(args)
        self.assertEqual(stopped.exception.code, 2)
        with self.assertRaisesRegex(ValueError, '1..8'):
            build_review(self.master, self.results, 'run', self.companies,
                         max_candidates=9)


if __name__ == '__main__':
    unittest.main()
