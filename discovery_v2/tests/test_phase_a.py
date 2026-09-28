import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import requests

from discovery_v2.contacts import role, select_default
from discovery_v2.eligibility import company_eligibility, normalize_legal_name
from discovery_v2.evidence import EvidenceWriter, phone_value
from discovery_v2.models import Config
from discovery_v2.runner import main, run
from discovery_v2.search import (DDGSearch, SUCCESS_WITH_RESULTS, SUCCESS_ZERO_RESULTS,
                                 SerperAPIError, SerperSearch, default_provider,
                                 domain_query, queries)
from discovery_v2.store import Store, digest, encode, read_manifest, snapshot

COMPANY = dict(id=1, company_name='ALFA d.o.o.', tax_number='12345678',
               registration_number='7654321', address='Glavna ulica 12, 1000 Ljubljana', municipality='Ljubljana')
IDENTITY = 'ALFA d.o.o. Davčna številka SI12345678 Glavna ulica 12, 1000 Ljubljana'
HTML = f'''<title>ALFA</title><h1>ALFA d.o.o.</h1><p>{IDENTITY}</p>
<section><h2>ALFA d.o.o.</h2><p>Kontakti</p>
<a href="mailto:info@legacy.si">info@legacy.si</a>
<a href="mailto:prodaja@alfa.si">prodaja@alfa.si</a>
<a href="mailto:janez.novak@alfa.si">janez.novak@alfa.si</a>
<a href="tel:+386 1 234 5678">+386 1 234 5678</a></section>'''


def response(html, url='https://alfa.si/'):
    r = requests.Response()
    r.status_code, r.url, r.encoding = 200, url, 'utf-8'
    r._content = html.encode()
    return r


class FakeSearch:
    name = 'fake-search'

    def __init__(self, results=None, error=None):
        self.results = results if results is not None else [{'url': 'https://bizi.si/alfa', 'title': 'ALFA d.o.o.',
            'body': 'Website https://alfa.si/ info@legacy.si Tel: +386 1 234 5678; SKD 2025 25.110 Manufacturing components'}]
        self.calls = []
        self.error = error

    def search(self, query, max_results):
        self.calls.append(query)
        if self.error:
            raise self.error
        return self.results[:max_results]


class FakeFetcher:
    def __init__(self, pages=None):
        self.pages = pages if pages is not None else {'https://alfa.si/': response(HTML)}
        self.requests, self.errors, self.calls = 0, [], []
        self.closed = False

    def fetch(self, url, allowed_site=None):
        from discovery.ownership import in_scope
        if allowed_site and not in_scope(url, allowed_site):
            raise AssertionError('Outside scope')
        if 'bizi.si' in url:
            raise AssertionError('No Bizi fetches permitted')
        self.calls.append(url)
        self.requests += 1
        return self.pages.get(url)

    def close(self):
        self.closed = True


def json_response(payload, status=200):
    r = requests.Response()
    r.status_code = status
    r.url = SerperSearch.endpoint
    r.encoding = 'utf-8'
    r.headers['Content-Type'] = 'application/json'
    r._content = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return r


class FakeSerperSession:
    def __init__(self, payload, status=200):
        self.response = json_response(payload, status)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class PhaseATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'frozen.db'
        conn = sqlite3.connect(self.source)
        conn.execute('CREATE TABLE companies_lite (id INTEGER PRIMARY KEY, company_name TEXT, tax_number TEXT, registration_number TEXT, address TEXT, municipality TEXT)')
        conn.execute('INSERT INTO companies_lite VALUES (?,?,?,?,?,?)', tuple(COMPANY.values()))
        # Frozen legacy data deliberately conflicts with fresh network evidence.
        conn.execute('CREATE TABLE website_discovery (company_id INTEGER, website TEXT, status TEXT)')
        conn.execute("INSERT INTO website_discovery VALUES (1,'https://stale.si/','VERIFIED')")
        conn.execute('CREATE TABLE email_discovery (company_id INTEGER, email TEXT)')
        conn.execute("INSERT INTO email_discovery VALUES (1,'old@stale.si')")
        conn.commit()
        conn.close()
        self.before = self.source.read_bytes()
        self.store = Store(self.root / 'results.db', source=self.source, create=True)
        self.addCleanup(self.store.close)
        # Every new test is incapable of accidentally using real HTTP/search.
        self.http_guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Live network forbidden'))
        self.http_guard.start()
        self.addCleanup(self.http_guard.stop)
        self.search_guard = patch('discovery.search_engine.search_ddg', side_effect=AssertionError('Live search forbidden'))
        self.search_guard.start()
        self.addCleanup(self.search_guard.stop)

    def create(self, ids=(1,), **kwargs):
        descriptor, companies = read_manifest(self.source, ids, 'frozen-sparrow09')
        return self.store.create_run(descriptor, companies, **kwargs)

    def execute(self, run_id=None, search=None, pages=None, **kwargs):
        run_id = run_id or self.create()
        provider = search or FakeSearch()
        fetchers = []

        def factory(config):
            f = FakeFetcher(pages)
            fetchers.append(f)
            return f
        result = run(self.store, run_id, provider=provider, fetcher_factory=factory, **kwargs)
        return run_id, result, provider, fetchers

    def rows(self, table):
        return [dict(r) for r in self.store.conn.execute(f'SELECT * FROM {table}')]

    def test_four_queries_keep_legal_name_and_optional_location(self):
        self.assertEqual([q for _, q in queries(COMPANY)], ['Podjetje ALFA d.o.o. kontakt', 'ALFA d.o.o. kontakt', 'Podjetje ALFA d.o.o. bizi.si'])
        self.assertEqual(queries(COMPANY, True)[0][1], 'Podjetje ALFA d.o.o. Ljubljana kontakt')
        self.assertEqual(domain_query('https://www.alfa.si/a'), ('DOMAIN_CONTACT', 'alfa.si kontakt'))

    def test_serper_maps_organic_results_and_preserves_safe_payload(self):
        secret = 'never-store-this-key'
        query = 'Podjetje GSELMAN & GSELMAN d.o.o. kontakt čšž'
        payload = {'organic': [
            {'position': 2, 'title': 'GSELMAN & GSELMAN', 'link': 'https://gselman-gselman.si/kontakt/',
             'snippet': 'Kontakt info@gselman-gselman.si'},
            {'position': 4, 'title': 'Bizi', 'link': 'https://www.bizi.si/GSELMAN-GSELMAN/',
             'snippet': 'Bistriška cesta 85, Poljčane'},
        ], 'credits': 99}
        session = FakeSerperSession(payload)
        outcome = SerperSearch(secret, session=session).search(query, 6)

        self.assertEqual(outcome.status, SUCCESS_WITH_RESULTS)
        self.assertEqual([r['position'] for r in outcome.results], [2, 4])
        self.assertEqual(outcome.results[0]['title'], 'GSELMAN & GSELMAN')
        self.assertEqual(outcome.results[0]['url'], 'https://gselman-gselman.si/kontakt/')
        self.assertEqual(outcome.results[0]['body'], 'Kontakt info@gselman-gselman.si')
        self.assertEqual(outcome.results[0]['provider'], 'serper')
        self.assertNotIn('credits', encode(outcome.results))
        self.assertNotIn(secret, encode(outcome.results))
        url, call = session.calls[0]
        self.assertEqual(url, SerperSearch.endpoint)
        self.assertEqual(call['json'], {'q': query, 'gl': 'si', 'hl': 'sl', 'num': 10})
        self.assertEqual(call['headers']['X-API-KEY'], secret)

    def test_serper_zero_results_and_http_failure_are_distinct(self):
        zero = SerperSearch('test-key', session=FakeSerperSession({'organic': [], 'credits': 1})).search('nič', 6)
        self.assertEqual(zero.status, SUCCESS_ZERO_RESULTS)
        self.assertEqual(zero.results, ())
        with self.assertRaises(requests.HTTPError):
            SerperSearch('test-key', session=FakeSerperSession({'message': 'unavailable'}, 503)).search('test', 6)
        with self.assertRaisesRegex(SerperAPIError, 'Serper API returned an error'):
            SerperSearch('test-key', session=FakeSerperSession({'message': 'quota exhausted'})).search('test', 6)

    def test_provider_selection_prefers_configured_serper_and_retains_ddgs(self):
        self.assertIsInstance(default_provider({}), DDGSearch)
        self.assertIsInstance(default_provider({'SERPER_API_KEY': 'configured'}), SerperSearch)
        with patch('discovery.search_engine.search_ddg', return_value=[]):
            self.assertEqual(DDGSearch().search('zero', 6).status, SUCCESS_ZERO_RESULTS)

    def test_serper_stages_after_first_query_and_persists_provenance(self):
        secret = 'integration-secret'
        session = FakeSerperSession({'organic': [{
            'position': 1, 'title': 'ALFA d.o.o.', 'link': 'https://alfa.si/',
            'snippet': IDENTITY + ' info@alfa.si Tel: +386 1 234 5678'}], 'credits': 1})
        provider = SerperSearch(secret, session=session)
        self.execute(search=provider)

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]['json']['q'], 'Podjetje ALFA d.o.o. kontakt')
        diagnostics = json.loads(self.rows('discovery_attempts')[0]['diagnostics_json'])
        self.assertEqual([q['status'] for q in diagnostics['queries']],
                         ['COMPLETED', 'SKIPPED', 'SKIPPED', 'SKIPPED'])
        self.assertEqual(diagnostics['queries'][0]['search_outcome'], SUCCESS_WITH_RESULTS)
        evidence = next(e for e in self.rows('discovery_evidence') if e['source_kind'] == 'SEARCH_RESULT')
        self.assertEqual((evidence['provider'], evidence['result_rank'], evidence['result_url'], evidence['title']),
                         ('serper', 1, 'https://alfa.si/', 'ALFA d.o.o.'))
        persisted = encode(self.rows('discovery_evidence')) + encode(diagnostics)
        self.assertNotIn(secret, persisted)
        self.assertNotIn('credits', persisted)

    def test_serper_http_failure_is_recorded_failed_without_secret(self):
        secret = 'failure-secret'
        provider = SerperSearch(secret, session=FakeSerperSession({'message': 'unavailable'}, 503))
        self.execute(search=provider, pages={})
        diagnostics = json.loads(self.rows('discovery_attempts')[0]['diagnostics_json'])
        self.assertEqual([q['search_outcome'] for q in diagnostics['queries'][:3]], ['FAILED'] * 3)
        self.assertTrue(all(q['status'] == 'FAILED' for q in diagnostics['queries'][:3]))
        self.assertNotIn(secret, encode(diagnostics) + encode(self.rows('discovery_evidence')))

    def test_serper_query_cap_stops_escalation_but_processes_first_evidence(self):
        session = FakeSerperSession({'organic': [{
            'position': 1, 'title': 'ALFA d.o.o.', 'link': 'https://alfa.si/',
            'snippet': IDENTITY + ' info@alfa.si'}]})
        run_id = self.create(config=Config(max_search_queries_per_company=1))
        _, _, _, fetchers = self.execute(run_id, search=SerperSearch('mock-key', session=session), pages={})

        self.assertEqual(len(session.calls), 1)
        diagnostics = json.loads(self.rows('discovery_attempts')[0]['diagnostics_json'])
        self.assertEqual([q['status'] for q in diagnostics['queries']],
                         ['COMPLETED', 'SKIPPED', 'SKIPPED', 'SKIPPED'])
        self.assertEqual([q.get('reason') for q in diagnostics['queries'][1:]],
                         ['Search query budget exhausted'] * 3)
        self.assertGreater(fetchers[0].requests, 0)
        self.assertTrue(any(e['source_kind'] == 'SEARCH_RESULT' for e in self.rows('discovery_evidence')))
        self.assertTrue(any(o['observation_type'] == 'EMAIL_CANDIDATE' for o in self.rows('discovery_observations')))

    def test_serper_without_query_cap_preserves_staged_escalation(self):
        session = FakeSerperSession({'organic': [{
            'position': 1, 'title': 'Directory', 'link': 'https://example.org/alfa',
            'snippet': 'Uncorroborated listing'}]})
        self.execute(search=SerperSearch('mock-key', session=session), pages={})
        self.assertEqual(len(session.calls), 3)
        diagnostics = json.loads(self.rows('discovery_attempts')[0]['diagnostics_json'])
        self.assertEqual([q['status'] for q in diagnostics['queries'][:3]], ['COMPLETED'] * 3)
        self.assertEqual(diagnostics['queries'][3]['status'], 'SKIPPED')

    def test_bankruptcy_name_variants_are_ineligible_only_for_exact_marker(self):
        names = [
            'PICERIJA BUF d.o.o. - v stečaju',
            'PICERIJA BUF D.O.O. - V STEČAJU',
            'Picerija Buf d.o.o. - V Stečaju',
            'PICERIJA BUF d.o.o. ...  v   stečaju !!!',
            'PICERIJA BUF d.o.o. (v---stečaju)',
            'PICERIJA BUF d.o.o. v___stečaju',
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(company_eligibility({'company_name': name}),
                                 ('INELIGIBLE', 'BANKRUPTCY'))
        self.assertEqual(normalize_legal_name('  A,  B  '), 'a b')
        self.assertEqual(company_eligibility({'company_name': 'ALFA d.o.o.'}),
                         ('ELIGIBLE', None))
        self.assertEqual(company_eligibility({'company_name': 'V STEČAJNEM POSTOPKU d.o.o.'}),
                         ('ELIGIBLE', None))

    def test_bankruptcy_company_records_outcome_without_discovery_work(self):
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE companies_lite SET company_name='PICERIJA BUF d.o.o. - v stečaju' WHERE id=1")
        conn.commit()
        conn.close()
        run_id = self.create()
        provider = FakeSearch()
        factory_calls = []

        result = run(self.store, run_id, provider=provider,
                     fetcher_factory=lambda config: factory_calls.append(config))

        company = self.rows('discovery_run_companies')[0]
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(company['status'], 'INELIGIBLE')
        self.assertEqual(company['eligibility_status'], 'INELIGIBLE')
        self.assertEqual(company['eligibility_reason'], 'BANKRUPTCY')
        self.assertEqual(provider.calls, [])
        self.assertEqual(factory_calls, [])
        self.assertEqual(self.rows('discovery_attempts'), [])
        self.assertEqual(self.rows('discovery_evidence'), [])
        self.assertEqual(self.rows('discovery_observations'), [])
        self.assertEqual(self.rows('discovery_contacts'), [])
        self.assertEqual(self.rows('discovery_company_results'), [])
        source = sqlite3.connect(self.source)
        try:
            self.assertEqual(source.execute('SELECT company_name FROM companies_lite WHERE id=1').fetchone()[0],
                             'PICERIJA BUF d.o.o. - v stečaju')
        finally:
            source.close()

    def test_complete_provenance_defaults_and_first_class_search(self):
        run_id, result, provider, fetchers = self.execute()
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(len(provider.calls), 4)
        self.assertTrue(fetchers[0].closed)
        self.assertFalse(any('bizi.si' in u for u in fetchers[0].calls))
        current = self.store.current_results(run_id)[0]
        self.assertEqual(current['official_website'], 'https://alfa.si/')
        contacts = {c['contact_id']: c for c in self.rows('discovery_contacts')}
        self.assertEqual(contacts[current['default_email_contact_id']]['normalized_value'], 'info@legacy.si')
        self.assertEqual(contacts[current['default_phone_contact_id']]['normalized_value'], '+38612345678')
        email = contacts[current['default_email_contact_id']]
        refs = json.loads(email['supporting_observation_ids_json'])
        self.assertGreaterEqual(len(refs), 5)
        observations = {o['observation_id']: o for o in self.rows('discovery_observations')}
        evidence = {e['evidence_id']: e for e in self.rows('discovery_evidence')}
        self.assertEqual(evidence[observations[email['primary_observation_id']]['evidence_id']]['source_kind'], 'FETCHED_PAGE')
        self.assertEqual({evidence[observations[r]['evidence_id']]['source_kind'] for r in refs}, {'SEARCH_RESULT', 'FETCHED_PAGE'})
        for e in evidence.values():
            self.assertEqual((e['run_id'], e['company_id']), (run_id, 1))
        search = next(e for e in evidence.values() if e['source_kind'] == 'SEARCH_RESULT')
        self.assertEqual((search['result_host'], search['provider'], search['result_rank']), ('bizi.si', 'fake-search', 1))
        self.assertEqual(search['source_class'], 'THIRD_PARTY')
        person = next(c for c in contacts.values() if c['normalized_value'].startswith('janez'))
        self.assertEqual(json.loads(person['roles_json']), ['PERSON'])
        self.assertIsNone(person['person_name'])
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertEqual(self.store.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_third_party_only_contacts_stay_candidates_no_bulk_fetch(self):
        _, result, _, _ = self.execute(pages={})
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertTrue(all(c['attribution_status'] == 'CANDIDATE' for c in self.rows('discovery_contacts')))
        record = self.rows('discovery_company_results')[0]
        self.assertIsNone(record['official_website'])
        self.assertIsNone(record['default_email_contact_id'])
        kinds = {o['observation_type'] for o in self.rows('discovery_observations')}
        self.assertTrue({'REGISTERED_ACTIVITY', 'BUSINESS_DESCRIPTION', 'COMPANY_IDENTITY'} <= kinds)
        activity = next(o for o in self.rows('discovery_observations') if o['observation_type'] == 'REGISTERED_ACTIVITY')
        self.assertEqual(json.loads(activity['value_json'])['classification_version'], '2025')
        self.assertFalse(json.loads(activity['value_json'])['authoritative'])

    def test_search_not_skipped_when_guess_verifies(self):
        _, result, provider, _ = self.execute(search=FakeSearch([]))
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(len(provider.calls), 4)

    def test_empty_search_has_diagnostics_and_domain_skip(self):
        self.execute(search=FakeSearch([]), pages={})
        diagnostics = json.loads(self.rows('discovery_attempts')[0]['diagnostics_json'])
        self.assertEqual([q.get('result_count') for q in diagnostics['queries'][:3]], [0, 0, 0])
        self.assertEqual(diagnostics['queries'][3]['status'], 'SKIPPED')

    def test_fresh_runs_never_reuse_verified_legacy_or_other_runs(self):
        first, _, _, _ = self.execute()
        second, _, provider, fetchers = self.execute()
        self.assertNotEqual(first, second)
        self.assertEqual(len(provider.calls), 4)
        self.assertGreater(fetchers[0].requests, 0)
        self.assertEqual(len(self.rows('discovery_company_results')), 2)
        self.assertFalse(any('stale.si' in encode(o) for o in self.rows('discovery_observations')))
        self.assertEqual(len(self.store.current_results(second)), 1)

    def test_resume_skips_completed_in_own_run(self):
        run_id, _, _, _ = self.execute()
        _, result, provider, fetchers = self.execute(run_id)
        self.assertEqual(result['processed'], 0)
        self.assertEqual(provider.calls, [])
        self.assertEqual(fetchers, [])
        self.assertEqual(len(self.rows('discovery_attempts')), 1)

    def test_failed_search_retains_evidence_and_resume_new_attempt(self):
        run_id, result, _, _ = self.execute(search=FakeSearch(error=TimeoutError('offline')))
        self.assertEqual(result['partial'], 1)
        self.assertEqual(self.store.current_results(run_id)[0]['contact_outcome'], 'INCOMPLETE')
        self.assertGreater(len(self.rows('discovery_evidence')), 0)
        self.assertEqual(len(json.loads(self.rows('discovery_attempts')[0]['diagnostics_json'])['queries']), 4)
        _, resumed, _, _ = self.execute(run_id)
        self.assertEqual(resumed['status'], 'COMPLETED')
        self.assertEqual([a['status'] for a in self.rows('discovery_attempts')], ['PARTIAL', 'COMPLETED'])

    def test_interrupted_attempt_preserved_and_retried(self):
        run_id = self.create()
        first = self.store.start_attempt(run_id, 1)
        context = dict(run_id=run_id, company_id=1, attempt_id=first)
        EvidenceWriter(self.store, context, COMPANY).generated('https://old-attempt.si/')
        self.execute(run_id)
        self.assertEqual([a['status'] for a in self.rows('discovery_attempts')], ['INTERRUPTED', 'COMPLETED'])
        self.assertNotEqual(self.store.current_results(run_id)[0]['attempt_id'], first)

    def test_changed_snapshot_refuses_resume_before_network(self):
        run_id = self.create()
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE companies_lite SET company_name='changed'")
        conn.commit()
        conn.close()
        provider = FakeSearch()
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            self.execute(run_id, search=provider)
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.rows('discovery_attempts'), [])

    def test_tampered_manifest_and_config_rejected(self):
        run_id = self.create()
        with self.store.conn:
            self.store.conn.execute("UPDATE discovery_runs SET config_json='{}' WHERE run_id=?", (run_id,))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.execute(run_id)

    def test_output_guards_no_source_or_unknown_db_modification(self):
        with self.assertRaises(ValueError):
            Store(self.source, source=self.source, create=True)
        with self.assertRaises(ValueError):
            Store(self.source, create=True)
        symbolic = self.root / 'symbolic.db'
        symbolic.symlink_to(self.source)
        hard = self.root / 'hard.db'
        os.link(self.source, hard)
        for alias in (symbolic, hard):
            with self.assertRaises(ValueError):
                Store(alias, create=True)
        from discovery_v2.store import ROOT
        with self.assertRaises(ValueError):
            Store(ROOT / 'database' / 'new-forbidden.db', create=True)
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_active_wal_source_rejected(self):
        Path(str(self.source) + '-wal').write_bytes(b'not frozen')
        with self.assertRaisesRegex(ValueError, 'checkpointed'):
            snapshot(self.source, 'baseline')

    def test_only_phase_a_modules_accepted_before_cli_writes(self):
        target = self.root / 'not-created.db'
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['create', '--source', str(self.source), '--results', str(target), '--namespace', 'x', '--ids', '1', '--job-type', 'PROFILE_ACTIVITY'])
        self.assertFalse(target.exists())
        with self.assertRaises(ValueError):
            self.create(job_type='SUCCESSION')
        self.assertEqual(self.rows('discovery_runs'), [])

    def test_cli_persists_search_query_cap_in_run_config(self):
        target = self.root / 'capped-results.db'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['create', '--source', str(self.source), '--results', str(target),
                '--namespace', 'x', '--ids', '1', '--max-search-queries-per-company', '1']), 0)
        created = Store(target)
        try:
            config = json.loads(created.conn.execute('SELECT config_json FROM discovery_runs').fetchone()[0])
            self.assertEqual(config['max_search_queries_per_company'], 1)
        finally:
            created.close()

    def test_invalid_manifest_and_config(self):
        for ids in ([], [1, 1], [999], [-1]):
            with self.assertRaises(ValueError):
                read_manifest(self.source, ids, 'x')
        with self.assertRaises(ValueError):
            Config(max_http_requests=0)
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                Config(max_search_queries_per_company=value)

    def test_phone_raw_extension_and_no_invented_country(self):
        self.assertEqual(phone_value('01 234 5678 ext. 12'), '012345678;ext=12')
        self.assertEqual(phone_value('tel:+386-1-234-5678;ext=12'), '+38612345678;ext=12')
        self.assertIsNone(phone_value('123'))

    def test_roles_all_supported_and_person_not_automatic_default(self):
        cases = {'info': 'GENERAL', 'prodaja': 'SALES', 'uprava': 'MANAGEMENT', 'nabava': 'PURCHASING',
                 'racunovodstvo': 'ACCOUNTING', 'hr': 'HR', 'support': 'SUPPORT', 'marketing': 'MARKETING',
                 'janez.novak': 'PERSON', 'something': 'UNKNOWN'}
        for local, expected in cases.items():
            self.assertEqual(role(local + '@alfa.si', 'EMAIL'), expected)
        self.assertIsNone(select_default([{'contact_type': 'EMAIL', 'attribution_status': 'ATTRIBUTED', 'roles_json': encode(['PERSON'])}], 'EMAIL'))

    def test_foreign_and_vendor_contacts_are_not_attributed(self):
        html = HTML + '''<section><h2>BETA d.o.o.</h2><a href="mailto:other@alfa.si">other@alfa.si</a>
        <a href="tel:+38619999999">+38619999999</a></section>
        <footer>Website by AGENCY <a href="mailto:agency@alfa.si">agency@alfa.si</a></footer>'''
        self.execute(pages={'https://alfa.si/': response(html)})
        contacts = {c['normalized_value']: c for c in self.rows('discovery_contacts')}
        for value in ('other@alfa.si', '+38619999999', 'agency@alfa.si'):
            self.assertEqual(contacts[value]['attribution_status'], 'REJECTED')

    def test_conflicting_visible_link_contact_rejected(self):
        html = HTML + '<section><h2>ALFA d.o.o.</h2><a href="mailto:wrong@alfa.si">visible@alfa.si</a></section>'
        self.execute(pages={'https://alfa.si/': response(html)})
        contacts = {c['normalized_value']: c for c in self.rows('discovery_contacts')}
        self.assertEqual(contacts['wrong@alfa.si']['attribution_status'], 'REJECTED')
        self.assertNotIn('visible@alfa.si', contacts)

    def test_atomic_result_failure_rolls_back_contacts_and_selection(self):
        original = self.store.insert

        def failing_insert(table, values):
            if table == 'discovery_company_results':
                raise RuntimeError('Simulated interruption during commit')
            original(table, values)
        with patch.object(self.store, 'insert', side_effect=failing_insert):
            run_id, result, _, _ = self.execute()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(self.rows('discovery_contacts'), [])
        self.assertIsNone(self.rows('discovery_run_companies')[0]['selected_attempt_id'])
        self.execute(run_id)
        self.assertEqual(len(self.store.current_results(run_id)), 1)

    def test_cross_attempt_supplemental_provenance_rejected_atomically(self):
        self.execute()
        old = self.rows('discovery_observations')[0]['observation_id']
        original = self.store.complete

        def invalid(context, contacts, result):
            contacts[0]['supporting_observation_ids_json'] = encode([old])
            return original(context, contacts, result)
        with patch.object(self.store, 'complete', side_effect=invalid):
            run_id, result, _, _ = self.execute()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(self.store.current_results(run_id), [])
        self.assertTrue(all(c['run_id'] != run_id for c in self.rows('discovery_contacts')))

    def test_two_simultaneous_invocations_do_not_steal_attempt(self):
        run_id = self.create()
        other = Store(self.store.path)
        try:
            with self.store.exclusive_runner(), self.assertRaisesRegex(ValueError, 'Another'):
                run(other, run_id, provider=FakeSearch(), fetcher_factory=lambda c: FakeFetcher())
        finally:
            other.close()
        self.assertEqual(self.rows('discovery_attempts'), [])

    def test_500_company_manifest_uses_bounded_chunks_and_resume(self):
        conn = sqlite3.connect(self.source)
        conn.executemany('INSERT INTO companies_lite VALUES (?,?,?,?,?,?)',
                         [(i, f'Company {i} d.o.o.', None, None, None, None) for i in range(2, 501)])
        conn.commit()
        conn.close()
        run_id = self.create(ids=range(1, 501))
        # Test real storage/progress orchestration, with bounded fake discovery.
        def empty_discovery(store, context, company, *args):
            store.complete(context, [], dict(website_status='NOT_FOUND', official_website=None,
                verified_scope=None, website_observation_id=None, website_evidence_ids_json='[]',
                website_assessment_json='{}', default_email_contact_id=None, default_phone_contact_id=None,
                contact_outcome='NO_ATTRIBUTED_CONTACT'))
        with patch('discovery_v2.runner.discover', side_effect=empty_discovery):
            first = run(self.store, run_id, max_items=203, batch_size=37)
            second = run(self.store, run_id, batch_size=37)
        self.assertEqual((first['processed'], first['remaining']), (203, 297))
        self.assertEqual((second['processed'], second['remaining']), (297, 0))
        self.assertEqual(len(self.store.current_results(run_id)), 500)

    def test_cli_create_only_no_network(self):
        target = self.root / 'cli.db'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['create', '--source', str(self.source), '--results', str(target), '--namespace', 'frozen', '--ids', '1'])
        self.assertEqual(code, 0)
        store = Store(target)
        try:
            run_id = json.loads(output.getvalue())['run_id']
            self.assertEqual(store.validate_run(run_id)[0]['status'], 'PENDING')
        finally:
            store.close()

    def test_domain_query_can_discover_new_official_website(self):
        class DomainSearch(FakeSearch):
            def search(self, query, max_results):
                self.calls.append(query)
                if query == 'alfa.si kontakt':
                    return [{'url': 'https://alfa-new.si/', 'title': 'ALFA', 'body': IDENTITY}]
                return []
        pages = {'https://alfa.si/': response('<title>ALFA</title><h1>ALFA d.o.o.</h1>'),
                 'https://alfa-new.si/': response(HTML + '<p>Website operated by ALFA d.o.o.</p>', 'https://alfa-new.si/')}
        run_id, result, search, _ = self.execute(search=DomainSearch(), pages=pages)
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(self.store.current_results(run_id)[0]['official_website'], 'https://alfa-new.si/')
        self.assertEqual(search.calls.count('alfa.si kontakt'), 1)
        observation_id = self.store.current_results(run_id)[0]['website_observation_id']
        observation = next(o for o in self.rows('discovery_observations') if o['observation_id'] == observation_id)
        evidence = next(e for e in self.rows('discovery_evidence') if e['evidence_id'] == observation['evidence_id'])
        self.assertEqual(evidence['query_type'], 'DOMAIN_CONTACT')

    def test_unknown_directory_exact_identity_does_not_establish_ownership(self):
        search = FakeSearch([{'url': 'https://directory.invalid/alfa', 'title': 'Company directory', 'body': IDENTITY}])
        html = '<title>ALFA company directory</title>' + HTML
        run_id, _, _, _ = self.execute(search=search, pages={'https://directory.invalid/alfa': response(html, 'https://directory.invalid/alfa')})
        self.assertIsNone(self.store.current_results(run_id)[0]['official_website'])
        self.assertTrue(all(c['attribution_status'] != 'ATTRIBUTED' for c in self.rows('discovery_contacts')))

    def test_tenant_scope_excludes_other_tenant_contact_link(self):
        scope = 'https://hosting.invalid/alfa/'
        html = HTML + '<p>Website operated by ALFA d.o.o.</p><a href="/beta/kontakt">Contact BETA</a>'
        search = FakeSearch([{'url': scope, 'title': 'ALFA', 'body': IDENTITY}])
        run_id, result, _, fetchers = self.execute(search=search, pages={scope: response(html, scope)})
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(self.store.current_results(run_id)[0]['verified_scope'], scope)
        self.assertFalse(any('/beta/' in u for u in fetchers[0].calls))

    def test_no_entity_contact_page_does_not_attribute_same_domain(self):
        home = f'<title>ALFA</title><h1>ALFA d.o.o.</h1><p>{IDENTITY}</p><a href="/kontakt">Kontakt</a>'
        page = '<p><a href="mailto:unknown@alfa.si">unknown@alfa.si</a></p><p>Tel: 01 999 9999</p>'
        self.execute(search=FakeSearch([]), pages={'https://alfa.si/': response(home), 'https://alfa.si/kontakt': response(page, 'https://alfa.si/kontakt')})
        contacts = self.rows('discovery_contacts')
        self.assertEqual(len(contacts), 2)
        self.assertTrue(all(c['attribution_status'] != 'ATTRIBUTED' for c in contacts))

    def test_contact_transport_failure_is_resumable_not_empty_success(self):
        run_id = self.create()
        home = HTML + '<a href="/kontakt">Kontakt</a>'

        class BrokenContact(FakeFetcher):
            def fetch(self, url, allowed_site=None):
                if url.endswith('/kontakt'):
                    self.errors.append(url + ': Timeout: offline')
                    return None
                return super().fetch(url, allowed_site)
        result = run(self.store, run_id, provider=FakeSearch(), fetcher_factory=lambda c: BrokenContact({'https://alfa.si/': response(home)}))
        self.assertEqual(result['partial'], 1)
        self.assertEqual(self.store.current_results(run_id)[0]['contact_outcome'], 'INCOMPLETE')
        self.assertTrue(json.loads(self.rows('discovery_attempts')[0]['diagnostics_json'])['fetch_errors'])
        self.assertEqual(self.execute(run_id)[1]['status'], 'COMPLETED')

    def test_default_and_value_provenance_validation_rolls_back(self):
        original = self.store.complete

        def wrong_default(context, contacts, result):
            result['default_phone_contact_id'] = next(c['contact_id'] for c in contacts if c['contact_type'] == 'EMAIL')
            original(context, contacts, result)
        with patch.object(self.store, 'complete', side_effect=wrong_default):
            _, result, _, _ = self.execute()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(self.rows('discovery_contacts'), [])

        def wrong_value(context, contacts, result):
            contacts[0]['normalized_value'] = 'invented@invalid.si'
            original(context, contacts, result)
        with patch.object(self.store, 'complete', side_effect=wrong_value):
            _, result, _, _ = self.execute()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(self.rows('discovery_contacts'), [])

    def test_foreign_keys_block_cross_attempt_primary_evidence(self):
        self.execute()
        observation = dict(self.rows('discovery_observations')[0])
        run_id = self.create()
        attempt_id = self.store.start_attempt(run_id, 1)
        observation.update(observation_id='bad-reference', run_id=run_id, attempt_id=attempt_id)
        with self.assertRaises(sqlite3.IntegrityError), self.store.conn:
            self.store.insert('discovery_observations', observation)

    def test_explicit_adapter_uses_new_bounded_fetcher(self):
        from discovery_v2.interfaces import new_fetcher
        first = new_fetcher(Config(max_http_requests=3))
        second = new_fetcher(Config(max_http_requests=3))
        try:
            first.cache['https://stale.si/'] = None
            self.assertEqual(second.cache, {})
            self.assertEqual(second.max_requests, 3)
        finally:
            first.close()
            second.close()

    def test_cached_contact_transport_error_is_not_lost(self):
        from discovery_v2.interfaces import evaluate_website
        run_id = self.create()
        home = HTML + '<a href="/kontakt">Kontakt</a>'

        class CachedFailure(FakeFetcher):
            seen_failure = False

            def fetch(self, url, allowed_site=None):
                if url.endswith('/kontakt'):
                    if not self.seen_failure:
                        self.errors.append(url + ': Timeout: unavailable')
                        self.seen_failure = True
                    return None
                return super().fetch(url, allowed_site)

        def evaluator(company, url, fetcher):
            fetcher.fetch('https://alfa.si/kontakt')
            return evaluate_website(company, url, fetcher)
        result = run(self.store, run_id, provider=FakeSearch(), evaluator=evaluator,
                     fetcher_factory=lambda c: CachedFailure({'https://alfa.si/': response(home)}))
        self.assertEqual(result['partial'], 1)
        self.assertEqual(self.store.current_results(run_id)[0]['contact_outcome'], 'INCOMPLETE')

    def test_interrupted_search_records_unexecuted_query_schedule(self):
        run_id = self.create()
        with self.assertRaises(KeyboardInterrupt):
            self.execute(run_id, search=FakeSearch(error=KeyboardInterrupt()))
        attempt = self.rows('discovery_attempts')[0]
        self.assertEqual(attempt['status'], 'INTERRUPTED')
        diagnostics = json.loads(attempt['diagnostics_json'])
        self.assertEqual([q['status'] for q in diagnostics['queries']], ['RUNNING', 'PENDING', 'PENDING', 'PENDING'])
        self.assertEqual(self.execute(run_id)[1]['status'], 'COMPLETED')

    def test_defaults_stable_when_observation_order_changes(self):
        self.execute()
        contacts = self.rows('discovery_contacts')
        self.assertEqual(select_default(contacts, 'EMAIL'), select_default(list(reversed(contacts)), 'EMAIL'))

    def test_unrelated_403_preserves_partial_assessments_and_resume_history(self):
        def review_with_error(company, url, fetcher):
            fetcher.fetch('https://alfa.si/')
            fetcher.inner.errors.append('Access denied/rate limited (403): https://unrelated.invalid/')
            return dict(status='REVIEW', verified=False)
        run_id, result, _, _ = self.execute(evaluator=review_with_error)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['partial'], 1)
        record = self.store.current_results(run_id)[0]
        self.assertEqual(record['website_status'], 'REVIEW')
        self.assertEqual(record['contact_outcome'], 'INCOMPLETE')
        self.assertGreater(len(self.rows('discovery_contacts')), 0)
        old_attempt = record['attempt_id']
        self.execute(run_id)
        self.assertNotEqual(self.store.current_results(run_id)[0]['attempt_id'], old_attempt)
        self.assertEqual(len(self.rows('discovery_company_results')), 2)
        self.assertEqual(self.rows('discovery_attempts')[0]['status'], 'PARTIAL')

    def test_saved_docentric_gets_actual_evaluation_budget(self):
        from discovery_v2.tests.test_live_regressions import FIXTURE
        company = FIXTURE['companies']['229']
        conn = sqlite3.connect(self.source)
        conn.execute('UPDATE companies_lite SET company_name=?,tax_number=?,registration_number=?,address=?,municipality=? WHERE id=1',
                     tuple(company[k] for k in ('company_name','tax_number','registration_number','address','municipality')))
        conn.commit(); conn.close()
        class SavedSearch(FakeSearch):
            def search(self, query, max_results):
                self.calls.append(query)
                return [dict(url=e['result_url'],title=e['title'],body=e['snippet_body']) for e in FIXTURE['search']
                        if e['company_id']==229 and e['query_text']==query][:max_results]
        evaluated = []
        def evaluate(company, url, fetcher):
            evaluated.append(url)
            return dict(status='REVIEW',verified=False,evidence=[dict(signals=['company_name_exact'])])
        _, _, provider, _ = self.execute(search=SavedSearch(),pages={},evaluator=evaluate)
        self.assertIn('https://docentric.com/', evaluated)
        self.assertLessEqual(len(evaluated), 8)
        self.assertNotIn('mojedelo.com kontakt', provider.calls)
        self.assertTrue(any(q in provider.calls for q in ('docentric.com kontakt','ax.docentric.com kontakt')))


if __name__ == '__main__':
    unittest.main()
