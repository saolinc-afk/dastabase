"""Offline regression against saved live evidence; never accesses the source database."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from discovery_v2.evidence import EvidenceWriter, phone_value
from discovery_v2.contacts import resolve_contacts, select_default, role
from discovery_v2.interfaces import evaluate_website, ScopedFetcher
from discovery_v2.candidates import brand_match, eligible, rank
from test_phase_a import response, FakeFetcher

FIXTURE = json.loads(Path(__file__).with_name('fixtures').joinpath('live5.json').read_text())

class MemoryStore:
    def __init__(self): self.rows = []
    def record(self, table, context, **values):
        self.rows.append((table, values))
        return str(len(self.rows))

class LiveEvidenceTests(unittest.TestCase):
    def setUp(self):
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        guard.start(); self.addCleanup(guard.stop)

    def extract(self, cid, urls, scope):
        company = FIXTURE['companies'][str(cid)]
        writer = EvidenceWriter(MemoryStore(), {}, company)
        for e in FIXTURE['search']:
            if e['company_id'] == cid:
                writer.search_result(dict(url=e['result_url'], title=e['title'], body=e['snippet_body']), e['query_type'], e['query_text'], 'saved', 1)
        responses = {}
        for url in urls:
            responses[url] = response(FIXTURE['pages'][url], url)
            writer.page(url, responses[url])
        contacts = resolve_contacts(company, writer.observations, dict(status='VERIFIED', scope=scope), responses)
        return writer, {c['normalized_value']: c for c in contacts}

    def test_saved_silco_comment_directory_has_no_active_contacts(self):
        writer, contacts = self.extract(882, ['https://www.silco.si/kontakt'], 'https://www.silco.si/')
        self.assertNotIn('info@autocolorfryslan.nl', contacts)
        self.assertNotIn('+31246793757', contacts)
        self.assertEqual(contacts['prodajasi@silco.si']['attribution_status'], 'ATTRIBUTED')
        self.assertEqual(select_default(list(contacts.values()), 'EMAIL'), contacts['prodajasi@silco.si']['contact_id'])
        self.assertTrue(any('autocolorfryslan' in r['evidence_payload_json'] for t,r in writer.store.rows if t=='discovery_evidence' and r['source_kind']=='FETCHED_PAGE'))

    def test_visible_foreign_card_also_cannot_be_default(self):
        company = dict(company_name='EXAMPLE d.o.o.', tax_number='12345678')
        url = 'https://example.si/kontakt'
        html = '<h1>EXAMPLE d.o.o.</h1><section><h2>Distributors</h2><div class="card"><h3>Foreign Co</h3><a href="mailto:info@foreign.eu">info@foreign.eu</a></div></section>'
        writer = EvidenceWriter(MemoryStore(), {}, company)
        r=response(html,url);writer.page(url,r)
        contacts=resolve_contacts(company,writer.observations,dict(status='VERIFIED',scope='https://example.si/'),{url:r})
        self.assertNotEqual(contacts[0]['attribution_status'],'ATTRIBUTED')
        self.assertIsNone(select_default(contacts,'EMAIL'))

    def test_saved_arko_regulator_excluded_company_contact_retained(self):
        _, contacts=self.extract(962,['https://arko.si/','https://arko.si/kontakt/','https://arko.si/politika-zasebnosti/'],'https://arko.si/')
        self.assertEqual(contacts['arko@arko.si']['attribution_status'],'ATTRIBUTED')
        self.assertNotEqual(contacts['gp.ip@ip-rs.si']['attribution_status'],'ATTRIBUTED')
        self.assertNotEqual(contacts['012309730']['attribution_status'],'ATTRIBUTED')
        self.assertEqual(contacts['+38625849620']['attribution_status'],'ATTRIBUTED')
        self.assertNotEqual(role('rezervni.deli@example.si','EMAIL'),'PERSON')
        self.assertNotEqual(role('servis.lj@example.si','EMAIL'),'PERSON')
        self.assertEqual(phone_value('+386 (0) 2 584 96 20'),phone_value('+386 2 584 96 20'))

    def test_saved_irles_cross_domain_corroboration(self):
        _, contacts=self.extract(1158,['https://irles.si/','https://irles.si/kontakt/'],'https://irles.si/')
        self.assertEqual(contacts['irles@amis.net']['attribution_status'],'ATTRIBUTED')

    def test_saved_docentric_candidates_prioritise_brand_not_job_portal(self):
        company=FIXTURE['companies']['229'];writer=EvidenceWriter(MemoryStore(),{},company)
        for e in FIXTURE['search']:
            if e['company_id']==229:writer.search_result(dict(url=e['result_url'],title=e['title'],body=e['snippet_body']),e['query_type'],e['query_text'],'saved',1)
        candidates=[o for o in writer.observations if o['observation_type']=='WEBSITE_CANDIDATE' and eligible(company,o['normalized_value'],o['value'].get('title',''),o['value'].get('body',''))]
        ordered=sorted(candidates,key=lambda o:rank(company,o))
        self.assertIn('https://docentric.com/',[o['normalized_value'] for o in ordered[:6]])
        self.assertFalse(brand_match(company,'https://www.mojedelo.com/'))
        self.assertFalse(eligible(company,'https://www.mojedelo.com/podjetje/example'))

    def test_saved_kresal_visible_phones_and_reordered_brand(self):
        url='https://www.steklarstvo-kresal.com/kontakt/'
        _,contacts=self.extract(6512,[url],'https://www.steklarstvo-kresal.com/')
        self.assertIn('015072124',contacts)
        self.assertIn('040888882',contacts)
        company=FIXTURE['companies']['6512']
        self.assertTrue(brand_match(company,url))
        result=evaluate_website(company,url,FakeFetcher({url:response(FIXTURE['pages'][url],url)}))
        self.assertTrue(result['verified'])

    def test_brand_alone_never_establishes_ownership(self):
        company=dict(company_name='BETA ALFA d.o.o.')
        url='https://alfa-beta.si/'
        result=evaluate_website(company,url,FakeFetcher({url:response('<h1>Alfa Beta</h1>',url)}))
        self.assertFalse(result['verified'])

    def test_nonvisible_markup_and_contact_excerpt(self):
        url='https://example.si/'
        html='<h1>Example d.o.o.</h1><div hidden>hidden@example.si</div><p style="display:none">none@example.si</p><section><h2>Contact</h2><a href="mailto:info@legacy.eu">Email us</a></section>'
        writer=EvidenceWriter(MemoryStore(),{},dict(company_name='Example d.o.o.'));writer.page(url,response(html,url))
        contacts=[o for o in writer.observations if o['observation_type']=='EMAIL_CANDIDATE']
        self.assertEqual(len(contacts),1)
        self.assertIn('info@legacy.eu',contacts[0]['value']['publication'])


class ScopedFailureTests(unittest.TestCase):
    def test_403_blocks_only_its_host_and_budget_stays_global(self):
        fetcher=ScopedFetcher(max_requests=3)
        denied=response('Forbidden','https://unrelated.invalid/');denied.status_code=403
        good=response('<h1>Example</h1>','https://company.invalid/')
        for r in (denied,good):
            r.headers['Content-Type']='text/html; charset=utf-8'
            r._content_consumed=True
        try:
            with patch.object(fetcher.session,'get',side_effect=[denied,good]) as get, patch('discovery.website_verifier.time.sleep'):
                self.assertIsNone(fetcher.fetch(denied.url))
                self.assertIsNone(fetcher.fetch('https://unrelated.invalid/another'))
                self.assertIsNotNone(fetcher.fetch(good.url))
                self.assertEqual(get.call_count,2)
                self.assertEqual(fetcher.requests,2)
        finally:fetcher.close()

    def test_supported_company_subdomain_requires_independent_identity(self):
        company=dict(company_name='EXAMPLE d.o.o.',tax_number='12345678',address='Main 10',municipality='Ljubljana')
        url='https://product.example.si/'
        rich='<title>Example</title><h1>Example d.o.o.</h1><p>SI12345678 Main 10 Ljubljana</p>'
        result=evaluate_website(company,url,FakeFetcher({url:response(rich,url)}))
        self.assertTrue(result['verified'])
        self.assertEqual(result['verified_scope'],url)
        weak=evaluate_website(company,url,FakeFetcher({url:response('<title>Example</title><p>EXAMPLE d.o.o.</p>',url)}))
        self.assertFalse(weak['verified'])

    def test_unrelated_hosting_tenant_does_not_get_brand_exemption(self):
        company=dict(company_name='EXAMPLE d.o.o.',tax_number='12345678',address='Main 10',municipality='Ljubljana')
        url='https://example.hosting.invalid/'
        html='<title>Example</title><h1>EXAMPLE d.o.o.</h1><p>SI12345678 Main 10 Ljubljana</p>'
        result=evaluate_website(company,url,FakeFetcher({url:response(html,url)}))
        self.assertFalse(result['verified'])

    def test_search_identity_does_not_confuse_extended_company_name(self):
        from discovery_v2.context import entity_specific
        company=dict(company_name='EXAMPLE d.o.o.', tax_number='12345678')
        self.assertFalse(entity_specific(company,'EXAMPLE SHIPPING d.o.o. info@shipping.invalid'))
        self.assertTrue(entity_specific(company,'EXAMPLE d.o.o. info@legacy.invalid'))

if __name__ == '__main__':
    unittest.main()
