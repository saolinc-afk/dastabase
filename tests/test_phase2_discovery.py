"""Offline Phase 2 tests: fake HTTP/search and temporary SQLite only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import requests
import database_lite as db
from discovery.domain_generator import normalize_domain, normalize_url, same_site
from discovery.website_verifier import verify, Fetcher, blocked_url, group_domain
from discovery.website_discovery import best_candidate, discover
from discovery.email_discovery import crawl_site, mailto_emails, extract_emails
from discovery.runner import load_companies, persist_email_result, run

COMPANY = {'id': 1, 'company_name': 'ALFA d.o.o.', 'tax_number': '12345678',
           'registration_number': '7654321', 'address': 'Glavna ulica 12, 1000 Ljubljana', 'municipality': '1000 Ljubljana'}


def response(html, url='https://alfa.si/'):
    r = requests.Response(); r.status_code = 200; r.url = url
    r._content = html.encode(); r._content_consumed = True; r.encoding = 'utf-8'
    return r


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages; self.errors = []; self.requests = 0; self.blocked = False
    def fetch(self, url, allowed_site=None):
        if allowed_site and not same_site(url, allowed_site):
            raise AssertionError('Cross-domain fetch attempted')
        self.requests += 1
        return self.pages.get(url)
    def close(self): pass


class VerificationTests(unittest.TestCase):
    def test_domain_normalization(self):
        self.assertEqual(normalize_domain('HTTPS://WWW.ALFA.SI/path?q=x'), 'alfa.si')
        self.assertEqual(normalize_domain('mailto:Info@Alfa.si?subject=test'), 'alfa.si')
        self.assertEqual(normalize_url('alfa.si/kontakt#x'), 'https://alfa.si/kontakt')
        self.assertEqual(normalize_url('javascript:alert(1)'), '')
        self.assertFalse(same_site('https://alfa.si.evil.com', 'https://alfa.si'))
        self.assertTrue(same_site('https://www.alfa.si/contact', 'https://alfa.si'))

    def test_name_title_alone_never_verified(self):
        f = FakeFetcher({'https://alfa.si/': response('<title>ALFA</title><h1>ALFA d.o.o.</h1>')})
        self.assertEqual(verify(COMPANY, 'https://alfa.si/', f)['status'], 'REVIEW')

    def test_tax_evidence(self):
        f = FakeFetcher({'https://alfa.si/': response('<title>ALFA</title>ALFA d.o.o. Davčna številka: SI12345678 Glavna ulica 12, 1000 Ljubljana')})
        result = verify(COMPANY, 'https://alfa.si/', f)
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertIn('tax_exact', result['evidence'][0]['signals'])

    def test_registration_evidence(self):
        f = FakeFetcher({'https://alfa.si/': response('<title>ALFA</title>ALFA d.o.o. Matična: 7654321 Glavna ulica 12, 1000 Ljubljana')})
        self.assertTrue(verify(COMPANY, 'https://alfa.si/', f)['verified'])

    def test_identifier_needs_corroboration_and_boundaries(self):
        for html in ('Davčna: 12345678', 'ALFA Davčna: 9123456789'):
            f = FakeFetcher({'https://alfa.si/': response(html)})
            self.assertFalse(verify(COMPANY, 'https://alfa.si/', f)['verified'])

    def test_name_address_across_pages(self):
        f = FakeFetcher({'https://alfa.si/': response('<title>ALFA</title>ALFA <a href="/kontakt">Kontakt</a>'),
                         'https://alfa.si/kontakt': response('ALFA Glavna ulica 12, 1000 Ljubljana', 'https://alfa.si/kontakt')})
        result = verify(COMPANY, 'https://alfa.si/', f)
        self.assertTrue(result['verified']); self.assertEqual(result['confidence'], 85)
        self.assertEqual(result['contact_page'], 'https://alfa.si/kontakt')

    def test_directory_social_gvin_and_parked_rejection(self):
        for url in ('https://www.companywall.si/alfa', 'https://linkedin.com/alfa', 'https://www.gvin.com/detail',
                    'https://www.sloexport.si/company-card?ms=7654321',
                    'https://www.ebonitete.si/alfa/', 'https://imenik-podjetij.com/firma/alfa',
                    'https://www.infobel.com/alfa', 'https://www.topograph.co/companies/alfa',
                    'https://et.cybo.com/SI-biz/alfa', 'https://www.moje-podjetje.net/alfa/'):
            f = FakeFetcher({})
            self.assertFalse(verify(COMPANY, url, f)['verified']); self.assertEqual(f.requests, 0)
        f = FakeFetcher({'https://alfa.si/': response('ALFA SI12345678 Domain for sale')})
        self.assertFalse(verify(COMPANY, 'https://alfa.si/', f)['verified'])

    def test_directory_identifiers_and_redirect_are_never_verified(self):
        listing = 'ALFA d.o.o. SI12345678 Matična 7654321 Glavna ulica 12, 1000 Ljubljana'
        f = FakeFetcher({'https://directory.example/': response(listing, 'https://www.sloexport.si/card')})
        result = verify(COMPANY, 'https://directory.example/', f)
        self.assertFalse(result['verified'])
        self.assertEqual(result['status'], 'REVIEW')
        self.assertEqual(f.requests, 1)
        self.assertTrue(blocked_url(result['final_url']))

    def test_job_directory_and_news_pages_with_identifiers_are_not_verified(self):
        identity = 'ALFA d.o.o. SI12345678 Matična 7654321 Glavna ulica 12, 1000 Ljubljana'
        for url, title in (
            ('https://jobs.example/alfa', 'Prosta delovna mesta - ALFA'),
            ('https://directory.example/alfa', 'ALFA company profile'),
            ('https://news.example/novice/alfa', 'Novice: ALFA'),
        ):
            f = FakeFetcher({url: response(f'<title>{title}</title>{identity}', url)})
            result = verify(COMPANY, url, f)
            self.assertFalse(result['verified'], url)
            self.assertEqual(result['status'], 'REVIEW')

    def test_legitimate_site_remains_verified(self):
        f = FakeFetcher({'https://alfa.si/': response('<title>ALFA</title>ALFA d.o.o. SI12345678 Glavna ulica 12, 1000 Ljubljana')})
        self.assertTrue(verify(COMPANY, 'https://alfa.si/', f)['verified'])

    def test_group_parent_site_is_review_not_blocked(self):
        self.assertEqual(group_domain('https://veto.si/'), 'veto.si')
        self.assertFalse(blocked_url('https://veto.si/'))
        html = 'ALFA d.o.o. SI12345678 Matična 7654321 Glavna ulica 12, 1000 Ljubljana'
        f = FakeFetcher({'https://veto.si/': response(html, 'https://veto.si/')})
        result = verify(COMPANY, 'https://veto.si/', f)
        self.assertEqual(result['status'], 'GROUP_REVIEW')
        self.assertFalse(result['verified'])

    def test_search_handoff_uses_string_and_preserves_metadata(self):
        candidate = {'url': 'https://alfa.si/', 'title': 'ALFA', 'body': 'Search snippet'}
        verified = {'verified': True, 'status': 'VERIFIED', 'confidence': 95, 'final_url': candidate['url']}
        with patch('discovery.website_discovery.verify', return_value=verified) as check:
            result = best_candidate(COMPANY, [candidate])
            self.assertEqual(check.call_args.args, (COMPANY, candidate['url']))
            self.assertEqual(result['candidate']['body'], 'Search snippet')

    def test_search_only_after_guesses(self):
        verified = {'verified': True, 'status': 'VERIFIED', 'confidence': 95, 'final_url': 'https://alfa.si/'}
        with patch('discovery.website_discovery.best_candidate', return_value=verified), patch('discovery.website_discovery.discover_urls') as search:
            discover(COMPANY, FakeFetcher({}))
            search.assert_not_called()

    def test_group_review_status_is_not_overwritten_by_error_handling(self):
        group = {'verified': False, 'status': 'GROUP_REVIEW', 'confidence': 40,
                 'final_url': 'https://veto.si/', 'evidence': [], 'contact_page': ''}
        with patch('discovery.website_discovery.best_candidate', return_value=group), \
             patch('discovery.website_discovery.discover_urls', return_value=[]):
            result = discover(COMPANY, Fetcher())
        self.assertEqual(result['status'], 'GROUP_REVIEW')

    def test_email_redirect_does_not_fetch_external_target(self):
        f = Fetcher()
        redirect = response('', 'https://alfa.si/'); redirect.status_code = 302; redirect.headers['Location'] = 'https://thirdparty.si/'
        with patch.object(f.session, 'get', return_value=redirect) as get, patch('discovery.website_verifier.time.sleep'):
            self.assertIsNone(f.fetch('https://alfa.si/', allowed_site='https://alfa.si/'))
            self.assertEqual(get.call_count, 1)
        f.close()


class EmailTests(unittest.TestCase):
    def test_mailto_multiple_recipients_and_exclusions(self):
        self.assertEqual(mailto_emails('MAILTO:Info%40alfa.si;ana@alfa.si?subject=mail@evil.si'), {'info@alfa.si', 'ana@alfa.si'})
        self.assertEqual(mailto_emails('mailto:noreply@alfa.si,test@example.com'), set())

    def test_visible_text(self):
        self.assertEqual(extract_emails('Pišite Ana.Novak@alfa.si ali info@alfa.si.'), {'ana.novak@alfa.si', 'info@alfa.si'})

    def test_verified_gate(self):
        with self.assertRaises(ValueError): crawl_site({'website': 'https://alfa.si', 'status': 'REVIEW'})
        with self.assertRaises(ValueError): crawl_site({'website': 'https://veto.si', 'status': 'GROUP_REVIEW'})
        with self.assertRaises(ValueError): crawl_site({'website': 'https://ebonitete.si', 'status': 'VERIFIED'})

    def test_contact_priority_source_email_domain_and_score(self):
        contact = response('<title>ALFA Kontakt</title><section><h2>ALFA d.o.o.</h2><a href="mailto:info@alfa.si,ana@alfa.si">Pišite nam</a></section><footer>Website by vendor@agency.si</footer>', 'https://www.alfa.si/kontakt')
        f = FakeFetcher({'https://alfa.si/kontakt': contact, 'https://alfa.si/': response('<a href="https://evil.si/contact">Contact</a>')})
        result = crawl_site({**COMPANY, 'ownership': {'status':'VERIFIED','scope':'https://alfa.si/'}, 'website': 'https://alfa.si/', 'status': 'VERIFIED', 'contact_page': 'https://alfa.si/kontakt'}, f)
        self.assertEqual({e['email'] for e in result['emails']}, {'info@alfa.si', 'ana@alfa.si'})
        self.assertTrue(all(e['confidence'] <= 100 for e in result['emails']))
        self.assertTrue(all(e['page_url'] == contact.url for e in result['emails']))
        self.assertTrue(result['rejected'])


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, 'DB_PATH', Path(self.tmp.name)/'test.db'); self.patch.start()
        db.initialize_database(); db.initialize_discovery_database()
        db.save_company(COMPANY)
    def tearDown(self):
        self.patch.stop(); self.tmp.cleanup()
    def test_idempotent_website_and_retry(self):
        for status in ('NOT_FOUND', 'ERROR', 'VERIFIED'):
            db.save_website({'company_id': 1, 'final_url': 'https://alfa.si/', 'status': status, 'confidence': 95, 'evidence': [{'signal':'tax'}]})
            conn=db.get_connection()
            self.assertEqual(conn.execute('SELECT count(*) FROM website_discovery').fetchone()[0], 1)
            conn.close()
            if status in ('NOT_FOUND', 'ERROR'):
                self.assertEqual(load_companies(), [])
                self.assertEqual(len(load_companies(retry=True)), 1)
        self.assertEqual(len(load_companies()), 1)  # resume pending email stage
        db.save_website({'company_id': 1, 'final_url': 'https://veto.si/', 'status': 'GROUP_REVIEW'})
        self.assertEqual(len(load_companies(retry=True)), 1)
    def test_idempotent_email_and_resume(self):
        db.save_website({'company_id': 1, 'status': 'VERIFIED', 'ownership': {'status':'VERIFIED','scope':'https://alfa.si/'}})
        e={'email':'info@alfa.si','confidence':110,'page_url':'https://alfa.si/kontakt','page_title':'Kontakt','found_in':'mailto','website':'https://alfa.si/','evidence':{'publication':'ALFA d.o.o. info@alfa.si'}}
        for _ in range(2): persist_email_result(1, {'emails':[e],'status':'DONE'})
        conn=db.get_connection()
        rows=conn.execute('SELECT * FROM email_discovery').fetchall()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['confidence'],100)
        self.assertEqual(rows[0]['website'],'https://alfa.si/')
        self.assertEqual(json.loads(rows[0]['evidence_json'])['publication'],'ALFA d.o.o. info@alfa.si')
        conn.close(); self.assertEqual(load_companies(),[])
    def test_blocked_email_cannot_be_persisted_and_targeted_repair_preserves_valid_data(self):
        db.save_website({'company_id': 1, 'status': 'VERIFIED', 'final_url': 'https://alfa.si/', 'ownership': {'status':'VERIFIED','scope':'https://alfa.si/'}})
        conn = db.get_connection()
        with self.assertRaises(ValueError):
            db.save_email(conn, 1, 'info@ebonitete.si', 90, 'https://ebonitete.si/kontakt', 'Kontakt', 'mailto', website='https://ebonitete.si/')
        db.save_email(conn, 1, 'info@alfa.si', 90, 'https://alfa.si/kontakt', 'Kontakt', 'mailto', website='https://alfa.si/', evidence={'publication':'ALFA d.o.o. info@alfa.si'})
        conn.commit()
        conn.close()
        self.assertEqual(db.invalidate_blocked_discovery_records([1]), {'websites': 0, 'emails': 0})
        conn = db.get_connection()
        self.assertEqual(conn.execute('SELECT email FROM email_discovery').fetchone()['email'], 'info@alfa.si')
        conn.close()

    def test_group_email_repair_removes_generic_parent_address(self):
        db.save_website({'company_id': 1, 'status': 'VERIFIED', 'final_url': 'https://veto.si/'})
        conn = db.get_connection()
        # Historical bad data predating ownership validation.
        conn.execute("INSERT INTO email_discovery (company_id,email,website,page_url) VALUES (1,'info@veto.si','https://veto.si/','https://veto.si/kontakt')")
        conn.commit(); conn.close()
        result = db.mark_group_review_records([1])
        self.assertEqual(result['emails'], 1)
        conn = db.get_connection()
        self.assertEqual(conn.execute('SELECT count(*) FROM email_discovery').fetchone()[0], 0)
        self.assertEqual(tuple(conn.execute('SELECT status,relationship FROM website_discovery').fetchone()), ('GROUP_REVIEW', 'GROUP_PARENT'))
        conn.close()
    def test_limits(self):
        with self.assertRaises(ValueError): run(limit=201)
        with self.assertRaises(ValueError): load_companies(ids=list(range(201)))
    def test_completed_report_resumes_without_new_work(self):
        report = Path(self.tmp.name) / 'complete.json'
        report.write_text(json.dumps({'selected_ids': [1], 'companies': [{'company_id': 1}], 'complete': False}))
        result = run(resume_report=report)
        self.assertTrue(result['complete'])
    def test_migration_does_not_change_companies(self):
        conn=db.get_connection();before=tuple(conn.execute('SELECT * FROM companies_lite').fetchone());conn.close()
        db.initialize_discovery_database()
        conn=db.get_connection();self.assertEqual(tuple(conn.execute('SELECT * FROM companies_lite').fetchone()),before);conn.close()


if __name__ == '__main__': unittest.main()
