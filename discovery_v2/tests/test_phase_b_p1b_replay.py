"""Offline P1B replay tests using only synthetic temporary SQLite artifacts."""
import hashlib
import io
import json
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from discovery_v2.replay import RecordedOnlyFetcher, replay
from discovery_v2.runner import run
from discovery_v2.runner import main
from discovery_v2.store import Store, read_manifest
from discovery_v2.store import encode, now
from discovery_v2.tests.test_phase_a import FakeFetcher, FakeSearch, response


HTML = '''<title>ALFA</title>
<section>Website operated by ALFA d.o.o.; VAT: 12345678</section>
<section><h2>ALFA d.o.o. Contact</h2>
<p>General company contact <a href="mailto:info@alfa.si">info@alfa.si</a></p>
<p>Sales <a href="mailto:sales@alfa.si">sales@alfa.si</a></p>
<p>Centralni telefon: +386 1 234 56 78</p></section>'''


def sha(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity_db = self.root / 'identities.db'
        conn = sqlite3.connect(self.identity_db)
        conn.execute('''CREATE TABLE companies_lite(
            id INTEGER PRIMARY KEY, company_name TEXT, tax_number TEXT,
            registration_number TEXT, address TEXT, municipality TEXT)''')
        for company_id in range(1, 7):
            conn.execute('INSERT INTO companies_lite VALUES(?,?,?,?,?,?)',
                (company_id, 'ALFA d.o.o.', '12345678', '7654321',
                 'Glavna ulica 12, 1000 Ljubljana', '1000 Ljubljana'))
        conn.commit(); conn.close()
        self.source = self.root / 'source-results.db'
        store = Store(self.source, source=self.identity_db, create=True)
        descriptor, companies = read_manifest(self.identity_db, [2, 1], 'synthetic')
        self.source_run = store.create_run(descriptor, companies)
        pages = {'https://alfa.si/': response(HTML, 'https://alfa.si/')}
        outcome = run(store, self.source_run, provider=FakeSearch(),
                      fetcher_factory=lambda config: FakeFetcher(pages))
        self.assertEqual(outcome['failed'], 0)
        store.close()
        self.before = self.source.read_bytes()
        self.network = patch('requests.sessions.Session.request',
                             side_effect=AssertionError('Network forbidden'))
        self.connect = patch.object(socket.socket, 'connect',
                                    side_effect=AssertionError('Network forbidden'))
        self.network.start(); self.connect.start()
        self.addCleanup(self.network.stop); self.addCleanup(self.connect.stop)

    def replay_to(self, name='replay.db'):
        destination = self.root / name
        result = replay(self.source, self.source_run, destination)
        return destination, result

    def raw_legacy_source(self):
        """Create source evidence with no current P1A observations."""
        path = self.root / 'raw-legacy.db'
        store = Store(path, source=self.identity_db, create=True)
        descriptor, companies = read_manifest(self.identity_db, [2, 1], 'legacy-raw')
        run_id = store.create_run(descriptor, companies)
        fixtures = {
            2: ('https://alfa.si/contact',
                '<title>Contact</title><section>Website operated by ALFA d.o.o.; VAT: 12345678</section>'),
            1: ('https://publisher.example/article',
                '<title>ALFA press release</title><article><section>Website operated by ALFA d.o.o.; VAT: 12345678</section></article>'),
        }
        for company in companies:
            attempt = store.start_attempt(run_id, company['id'])
            context = {'run_id': run_id, 'company_id': company['id'], 'attempt_id': attempt}
            url, html = fixtures[company['id']]
            store.record('discovery_evidence', context, source_kind='GENERATED_CANDIDATE',
                source_class='HYPOTHESIS', observed_at=now(), requested_url='https://alfa.si/',
                evidence_payload_json=encode({'strategy': 'legacy_guess'}))
            store.record('discovery_evidence', context, source_kind='SEARCH_RESULT',
                source_class='UNASSESSED', observed_at=now(), provider='legacy',
                query_type='LEGAL_COMPANY_CONTACT', query_text='legacy', result_rank=1,
                result_url=url, result_host=url.split('/')[2], title='ALFA d.o.o.',
                snippet_body='ALFA d.o.o. Website ' + url,
                evidence_payload_json=encode({'url': url, 'title': 'ALFA d.o.o.',
                    'body': 'ALFA d.o.o. Website ' + url}))
            store.record('discovery_evidence', context, source_kind='FETCHED_PAGE',
                source_class='UNASSESSED', observed_at=now(), requested_url=url,
                final_url=url, http_status=200, title='Contact', snippet_body='legacy text',
                content_hash='legacy', evidence_payload_json=encode({'html': html}))
            with store.conn:
                store.conn.execute("UPDATE discovery_attempts SET status='COMPLETED',finished_at=? WHERE attempt_id=?",
                                   (now(), attempt))
                store.conn.execute("""UPDATE discovery_run_companies
                    SET status='COMPLETED',selected_attempt_id=? WHERE run_id=? AND company_id=?""",
                    (attempt, run_id, company['id']))
        with store.conn:
            store.conn.execute("UPDATE discovery_runs SET status='COMPLETED',finished_at=? WHERE run_id=?",
                               (now(), run_id))
        self.assertEqual(store.conn.execute(
            'SELECT COUNT(*) FROM discovery_observations').fetchone()[0], 0)
        store.close()
        return path, run_id

    def raw_html_source(self, fixtures, order):
        """Create an old-style artifact containing raw pages but no P1A observations."""
        path = self.root / ('raw-parity-' + '-'.join(map(str, order)) + '.db')
        store = Store(path, source=self.identity_db, create=True)
        descriptor, companies = read_manifest(self.identity_db, order, 'raw-parity')
        run_id = store.create_run(descriptor, companies)
        for company in companies:
            attempt = store.start_attempt(run_id, company['id'])
            context = {'run_id': run_id, 'company_id': company['id'], 'attempt_id': attempt}
            for rank, (url, html) in enumerate(fixtures[company['id']], 1):
                store.record('discovery_evidence', context, source_kind='SEARCH_RESULT',
                    source_class='UNASSESSED', observed_at=now(), provider='legacy',
                    query_type='LEGAL_COMPANY_CONTACT', query_text='legacy', result_rank=rank,
                    result_url=url, result_host=url.split('/')[2], title='ALFA d.o.o.',
                    snippet_body='ALFA d.o.o. Website ' + url,
                    evidence_payload_json=encode({'url': url, 'title': 'ALFA d.o.o.',
                        'body': 'ALFA d.o.o. Website ' + url}))
                store.record('discovery_evidence', context, source_kind='FETCHED_PAGE',
                    source_class='UNASSESSED', observed_at=now(), requested_url=url,
                    final_url=url, http_status=200, title='', snippet_body='',
                    content_hash='legacy', evidence_payload_json=encode({'html': html}))
            with store.conn:
                store.conn.execute("UPDATE discovery_attempts SET status='COMPLETED',finished_at=? WHERE attempt_id=?",
                                   (now(), attempt))
                store.conn.execute("""UPDATE discovery_run_companies
                    SET status='COMPLETED',selected_attempt_id=? WHERE run_id=? AND company_id=?""",
                    (attempt, run_id, company['id']))
        with store.conn:
            store.conn.execute("UPDATE discovery_runs SET status='COMPLETED',finished_at=? WHERE run_id=?",
                               (now(), run_id))
        self.assertEqual(store.conn.execute(
            'SELECT COUNT(*) FROM discovery_observations').fetchone()[0], 0)
        store.close()
        return path, run_id

    def logical(self, destination, run_id):
        store = Store(destination)
        try:
            values = []
            for company in store.conn.execute('''SELECT * FROM discovery_run_companies
                    WHERE run_id=? ORDER BY manifest_position''', (run_id,)):
                result = store.conn.execute('''SELECT * FROM discovery_company_results
                    WHERE attempt_id=?''', (company['selected_attempt_id'],)).fetchone()
                assessment = json.loads(result['website_assessment_json'])
                chosen = next((c.get('assessment', {}) for c in assessment['candidates']
                               if c.get('observation_id') == result['website_observation_id']), {})
                contacts = {row['contact_id']: row for row in store.conn.execute(
                    'SELECT * FROM discovery_contacts WHERE attempt_id=?',
                    (company['selected_attempt_id'],))}
                values.append((company['company_id'], company['manifest_position'],
                    result['website_status'], result['official_website'], result['verified_scope'],
                    chosen.get('p1a', {}).get('rule_id'), chosen.get('relationship'),
                    contacts[result['default_email_contact_id']]['normalized_value']
                        if result['default_email_contact_id'] else None,
                    contacts[result['default_phone_contact_id']]['normalized_value']
                        if result['default_phone_contact_id'] else None,
                    result['contact_outcome'], sorted((c['normalized_value'],
                        c['attribution_status']) for c in contacts.values())))
            return values
        finally:
            store.close()

    def test_source_is_immutable_and_lineage_is_complete(self):
        destination, result = self.replay_to()
        self.assertEqual(self.source.read_bytes(), self.before)
        lineage = json.loads(Path(result['lineage']).read_text())
        self.assertEqual(lineage['source_sha256'], sha(self.source))
        self.assertEqual(lineage['source_run_id'], self.source_run)
        self.assertEqual(lineage['replay_mode'], 'OFFLINE_REPLAY')
        self.assertTrue(lineage['network_disabled'])
        self.assertEqual(lineage['engine_version'], 'discovery-v2-phase-a-5')
        self.assertTrue(destination.exists())

    def test_source_and_destination_must_differ_and_destination_is_new(self):
        with self.assertRaises(ValueError):
            replay(self.source, self.source_run, self.source)
        occupied = self.root / 'occupied.db'; occupied.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            replay(self.source, self.source_run, occupied)
        self.assertEqual(occupied.read_bytes(), b'keep')
        sidecar_destination = self.root / 'sidecar-conflict.db'
        sidecar = Path(str(sidecar_destination) + '.replay.json')
        sidecar.write_text('keep')
        with self.assertRaises(ValueError):
            replay(self.source, self.source_run, sidecar_destination)
        self.assertEqual(sidecar.read_text(), 'keep')
        self.assertFalse(sidecar_destination.exists())

    def test_manifest_order_current_rules_and_contact_ranking(self):
        destination, result = self.replay_to()
        logical = self.logical(destination, result['run_id'])
        self.assertEqual([(row[0], row[1]) for row in logical], [(2, 0), (1, 1)])
        self.assertTrue(all(row[2] == 'VERIFIED' for row in logical))
        self.assertTrue(all(row[5] == 'OWN-01_EXACT_IDENTIFIER_ON_SITE' for row in logical))
        self.assertTrue(all(row[7] == 'info@alfa.si' for row in logical))
        self.assertTrue(all(row[8] == '+38612345678' for row in logical))

    def test_replay_uses_current_p1a_entrypoint(self):
        from discovery_v2 import replay as replay_module
        calls = []
        real = replay_module.evaluate_website
        def observed(*args, **kwargs):
            calls.append(args[1])
            return real(*args, **kwargs)
        with patch.object(replay_module, 'evaluate_website', side_effect=observed):
            _, result = self.replay_to()
        self.assertGreaterEqual(len(calls), result['companies'])

    def test_replay_primary_selection_prefers_direct_site_over_verified_group_scope(self):
        group = 'https://group.example/alfa/'
        direct = 'https://alfa.si/'
        group_html = '''<title>ALFA group</title><section>Website operated by ALFA d.o.o.
            Glavna ulica 12, 1000 Ljubljana is a member of group</section>'''
        source, source_run = self.raw_html_source(
            {3: [(group, group_html), (direct, HTML)]}, [3])
        before = source.read_bytes()

        destination = self.root / 'primary-selection-replay.db'
        result = replay(source, source_run, destination)
        store = Store(destination)
        try:
            selected = store.current_results(result['run_id'])[0]
            candidates = json.loads(selected['website_assessment_json'])['candidates']
            group_candidate = next(candidate for candidate in candidates
                                   if candidate['url'] == group)
            self.assertEqual(group_candidate['status'], 'VERIFIED')
            self.assertEqual(group_candidate['assessment']['relationship'],
                             'SUBSIDIARY_ON_GROUP_DOMAIN')
            self.assertEqual(selected['official_website'], direct)
        finally:
            store.close()
        self.assertEqual(source.read_bytes(), before)

    def test_raw_legacy_html_reconstructs_current_bounded_p1a_evidence(self):
        source, source_run = self.raw_legacy_source()
        source_before = source.read_bytes()
        destination = self.root / 'raw-replay.db'
        result = replay(source, source_run, destination)
        self.assertEqual(source.read_bytes(), source_before)
        store = Store(destination)
        try:
            companies = list(store.conn.execute('''SELECT * FROM discovery_run_companies
                WHERE run_id=? ORDER BY manifest_position''', (result['run_id'],)))
            verified = store.conn.execute('''SELECT * FROM discovery_company_results
                WHERE attempt_id=?''', (companies[0]['selected_attempt_id'],)).fetchone()
            editorial = store.conn.execute('''SELECT * FROM discovery_company_results
                WHERE attempt_id=?''', (companies[1]['selected_attempt_id'],)).fetchone()
            self.assertEqual(verified['website_status'], 'VERIFIED')
            self.assertEqual(editorial['website_status'], 'REVIEW')
            observations = list(store.conn.execute('''SELECT o.observation_type,o.extraction_method,
                e.evidence_payload_json FROM discovery_observations o JOIN discovery_evidence e
                ON e.evidence_id=o.evidence_id WHERE o.attempt_id=?''',
                (companies[0]['selected_attempt_id'],)))
            bounded = [row for row in observations if row['extraction_method'] == 'bounded_identity']
            self.assertTrue(any(row['observation_type'] == 'LEGAL_NAME' for row in bounded))
            self.assertTrue(any(row['observation_type'] == 'TAX_NUMBER' for row in bounded))
            self.assertTrue(any(row['observation_type'] == 'SITE_OPERATOR' for row in bounded))
            self.assertTrue(all(json.loads(row['evidence_payload_json'])
                ['offline_replay_source']['evidence_id'] for row in bounded))
            assessment = json.loads(verified['website_assessment_json'])
            chosen = next(c['assessment'] for c in assessment['candidates']
                          if c['observation_id'] == verified['website_observation_id'])
            self.assertTrue(chosen['p1a']['page_classifications'])
            self.assertTrue(chosen['p1a']['identity_evidence'])
            self.assertTrue(chosen['p1a']['operator_evidence'])
            editorial_assessment = json.loads(editorial['website_assessment_json'])
            self.assertTrue(any('NEWS_PAGE' in c.get('assessment', {}).get('p1a', {})
                .get('page_classifications', {}).values()
                for c in editorial_assessment['candidates']))
        finally:
            store.close()

    def test_raw_operator_and_relationship_reconstruction_matches_live_safety_boundaries(self):
        fixtures = {
            1: [('https://alfa.si/contact',
                 '<title>Contact</title><section>Website operated by ALFA d.o.o.; VAT: 12345678</section>')],
            2: [('https://alfa.si/subsidiary/',
                 '<title>Subsidiary</title><section>Website operated by ALFA d.o.o. is a member of group; VAT: 12345678</section>')],
            3: [('https://alfa.si/press/operator',
                 '<title>News article</title><article><section>Website operated by ALFA d.o.o.; VAT: 12345678</section></article>')],
            4: [('https://alfa.si/listing/alfa',
                 '<title>Company directory</title><section>Website operated by ALFA d.o.o.; VAT: 12345678</section>')],
            5: [('https://alfa.si/legal',
                 '<title>Legal</title><section>ALFA d.o.o.; Glavna ulica 12, 1000 Ljubljana</section>'),
                ('https://alfa.si/', '<title>ALFA</title><p>Welcome</p>')],
            6: [('https://alfa.si/split/operator',
                 '<section>Website operated by ALFA d.o.o.</section>'),
                ('https://alfa.si/split/identity',
                 '<section>VAT: 12345678</section>')],
        }
        source, source_run = self.raw_html_source(fixtures, [1, 2, 3, 4, 5, 6])
        before = source.read_bytes()
        destination = self.root / 'raw-operator-parity.db'
        result = replay(source, source_run, destination)
        self.assertEqual(source.read_bytes(), before)
        store = Store(destination)
        try:
            rows = list(store.conn.execute('''SELECT rc.company_id,rc.selected_attempt_id,
                r.website_status,r.website_assessment_json FROM discovery_run_companies rc
                JOIN discovery_company_results r ON r.attempt_id=rc.selected_attempt_id
                WHERE rc.run_id=? ORDER BY rc.manifest_position''', (result['run_id'],)))
            self.assertEqual([row['company_id'] for row in rows], [1, 2, 3, 4, 5, 6])
            self.assertEqual([row['website_status'] for row in rows],
                             ['VERIFIED', 'VERIFIED', 'REVIEW', 'REVIEW', 'VERIFIED', 'REVIEW'])

            by_company = {row['company_id']: row for row in rows}
            observations = {}
            for company_id, row in by_company.items():
                observations[company_id] = list(store.conn.execute('''SELECT observation_type,
                    normalized_value,value_json FROM discovery_observations
                    WHERE attempt_id=? AND extraction_method='bounded_identity' ''',
                    (row['selected_attempt_id'],)))
            self.assertTrue(any(o['observation_type'] == 'SITE_OPERATOR' for o in observations[1]))
            self.assertTrue(any(o['observation_type'] == 'ENTITY_RELATIONSHIP'
                                and o['normalized_value'] == 'SUBSIDIARY_ON_GROUP_DOMAIN'
                                for o in observations[2]))
            self.assertFalse(any(o['observation_type'] == 'SITE_OPERATOR' for o in observations[5]))

            def candidate_assessments(company_id):
                payload = json.loads(by_company[company_id]['website_assessment_json'])
                return [c.get('assessment', {}).get('p1a', {}) for c in payload['candidates']]

            safe = candidate_assessments(1)
            group = candidate_assessments(2)
            strong = candidate_assessments(5)
            self.assertTrue(any(p.get('operator_evidence') for p in safe))
            self.assertTrue(any(p.get('operator_evidence') and p.get('relationship_evidence')
                                for p in group))
            self.assertTrue(any(p.get('authorization_basis') == 'STRONG_LEGAL_PAGE'
                                and not p.get('operator_evidence') for p in strong))
            for company_id in (3, 4, 6):
                self.assertFalse(any(p.get('operator_evidence') or p.get('relationship_evidence')
                                     for p in candidate_assessments(company_id)))
            self.assertTrue(any('NEWS_PAGE' in p.get('page_classifications', {}).values()
                                for p in candidate_assessments(3)))
            self.assertTrue(any('THIRD_PARTY_PAGE' in p.get('page_classifications', {}).values()
                                for p in candidate_assessments(4)))
        finally:
            store.close()

    def test_confidence_replay_is_zero_network_immutable_and_deterministic(self):
        fixtures = {1: [('https://alfa.si/',
            '<title>ALFA</title><section>ALFA d.o.o.; Glavna ulica 12, 1000 Ljubljana</section>')]}
        source, source_run = self.raw_html_source(fixtures, [1])
        before = source.read_bytes()
        first = self.root / 'confidence-one.db'
        second = self.root / 'confidence-two.db'
        one = replay(source, source_run, first)
        two = replay(source, source_run, second)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.logical(first, one['run_id']), self.logical(second, two['run_id']))
        store = Store(first)
        try:
            result = store.conn.execute('SELECT * FROM discovery_company_results').fetchone()
            self.assertEqual(result['website_status'], 'HIGH')
            assessment = json.loads(result['website_assessment_json'])
            chosen = next(c['assessment'] for c in assessment['candidates']
                          if c['observation_id'] == result['website_observation_id'])
            self.assertEqual(chosen['p1a']['confidence_rule_id'],
                             'CONF-01_LEGAL_ADDRESS_CONVERGENCE')
            self.assertTrue(chosen['p1a']['confidence_reasons'])
        finally:
            store.close()

    def test_review_root_does_not_suppress_stronger_recorded_same_host_path(self):
        fixtures = {1: [
            ('https://alfa.si/', '<title>ALFA</title><p>Welcome</p>'),
            ('https://alfa.si/o-nas/', '<title>O nas</title><section>ALFA d.o.o.; '
             'Glavna ulica 12, 1000 Ljubljana</section>'),
        ]}
        source, source_run = self.raw_html_source(fixtures, [1])
        before = source.read_bytes()
        destination = self.root / 'same-host-path.db'
        result = replay(source, source_run, destination)
        self.assertEqual(source.read_bytes(), before)
        store = Store(destination)
        try:
            row = store.conn.execute('SELECT * FROM discovery_company_results').fetchone()
            self.assertIn(row['website_status'], ('VERIFIED', 'HIGH', 'MEDIUM'))
            self.assertIn('/o-nas/', row['official_website'])
            assessment = json.loads(row['website_assessment_json'])
            urls = [item['url'] for item in assessment['candidates']]
            self.assertEqual(len(urls), len(set(urls)))
        finally:
            store.close()

    def test_ruleless_verified_result_is_rejected(self):
        forged = {'verified': True, 'status': 'VERIFIED',
            'verified_scope': 'https://alfa.si/', 'relationship': 'STANDALONE',
            'ownership': {'status': 'VERIFIED', 'scope': 'https://alfa.si/',
                          'relationship': 'STANDALONE'},
            'p1a': {'verified': True, 'status': 'VERIFIED', 'rule_id': None}}
        destination = self.root / 'forged.db'
        with patch('discovery_v2.replay.evaluate_website', return_value=forged), \
                self.assertRaises(ValueError):
            replay(self.source, self.source_run, destination)
        self.assertFalse(destination.exists())

    def test_source_evidence_lineage_is_retained(self):
        destination, result = self.replay_to()
        store = Store(destination)
        try:
            evidence = list(store.conn.execute('SELECT evidence_payload_json FROM discovery_evidence'))
            self.assertTrue(evidence)
            links = [json.loads(row[0])['offline_replay_source'] for row in evidence]
            self.assertTrue(all(link['artifact_path'] == str(self.source.resolve()) for link in links))
            source = sqlite3.connect(self.source)
            try:
                source_ids = {row[0] for row in source.execute(
                    'SELECT evidence_id FROM discovery_evidence')}
            finally:
                source.close()
            self.assertTrue(all(link['evidence_id'] in source_ids for link in links))
        finally:
            store.close()

    def test_unrecorded_candidate_url_fails_closed_without_fetch(self):
        writer = type('Writer', (), {})()
        fetcher = RecordedOnlyFetcher(writer, {})
        self.assertIsNone(fetcher.fetch('https://unrecorded.example/'))
        self.assertEqual(fetcher.missing_urls, ['https://unrecorded.example/'])
        self.assertEqual(fetcher.responses, {})

    def test_replay_is_logically_deterministic(self):
        first, one = self.replay_to('one.db')
        second, two = self.replay_to('two.db')
        self.assertEqual(self.logical(first, one['run_id']),
                         self.logical(second, two['run_id']))

    def test_cli_replay_command(self):
        destination = self.root / 'cli.db'
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['replay', '--source-results', str(self.source),
                         '--source-run-id', self.source_run,
                         '--results', str(destination)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'COMPLETED')
        self.assertTrue(destination.exists())


if __name__ == '__main__':
    unittest.main()
