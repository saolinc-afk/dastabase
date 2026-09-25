"""Offline ownership, scoped-crawl and entity attribution regressions."""
import json
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup
from test_phase2_discovery import COMPANY, FakeFetcher, response, DatabaseTests
from discovery.website_verifier import verify, score_page, Fetcher
from discovery.email_discovery import crawl_site, contact_context
from discovery.ownership import evaluate_ownership, email_attribution, in_scope, page_type
from discovery.domain_policy import policy_for_url, DIRECTORY, COMPANY_DATABASE, TOURISM_PROFILE, GROUP_PARENT, BLOCK_AS_OFFICIAL, GROUP_REVIEW

IDENTITY = 'ALFA d.o.o. SI12345678 Matična 7654321 Glavna ulica 12, 1000 Ljubljana'

class OwnershipTests(unittest.TestCase):
    def test_central_policy_blocks_known_publishers_but_keeps_group_semantics(self):
        for url, category in (
            ('https://local.infobel.si/alfa', COMPANY_DATABASE),
            ('https://www.mascus.co.uk/dealer/alfa', 'MARKETPLACE'),
            ('https://trivago.com/alfa', 'BOOKING_AGGREGATOR'),
            ('https://evendo.com/locations/alfa', TOURISM_PROFILE),
        ):
            with self.subTest(url=url):
                policy = policy_for_url(url)
                self.assertEqual(policy.classification, category)
                self.assertEqual(policy.policy, BLOCK_AS_OFFICIAL)
                self.assertEqual(verify(COMPANY, url, FakeFetcher({}))['status'], 'REVIEW')
        group = policy_for_url('https://veto.si/locations/alfa')
        self.assertEqual((group.classification, group.policy), (GROUP_PARENT, GROUP_REVIEW))

    def test_tourism_profile_cannot_become_group_parent(self):
        url = 'https://evendo.com/locations/slovenia/bar/alfa'
        html = '<title>ALFA Lounge Travel Guide | Evendo</title> Group companies ' + IDENTITY
        result = verify(COMPANY, url, FakeFetcher({url: response(html, url)}))
        self.assertEqual(result['status'], 'REVIEW')
        self.assertNotEqual(result.get('relationship'), 'GROUP_PARENT')
        self.assertEqual(page_type(url, 'ALFA Lounge Travel Guide | Evendo', 'Group companies'), 'THIRD_PARTY')

    def check_page(self, url, title, extra='', company=COMPANY, candidate=None):
        f=FakeFetcher({candidate or url:response(f'<title>{title}</title>{IDENTITY}{extra}',url)})
        return verify(company,candidate or url,f)

    def test_directory_exact_legal_identifiers_are_entity_only(self):
        r=self.check_page('https://directory.example/alfa','ALFA business directory')
        self.assertEqual(r['status'],'REVIEW')
        self.assertIn('tax_exact',r['evidence'][0]['entity_signals'])
        self.assertFalse(r['ownership']['ownership_evidence'])

    def test_taxpayer_and_company_databases(self):
        for url,title in [('https://records.example/ddv/12345678','ALFA'),('https://records.example/a','Company database ALFA')]:
            with self.subTest(url=url):self.assertEqual(self.check_page(url,title)['status'],'REVIEW')

    def test_hosted_company_page_needs_operator_evidence(self):
        self.assertEqual(self.check_page('https://alfa.hosting.example/','ALFA')['status'],'REVIEW')
        r=self.check_page('https://hosting.example/alfa/','ALFA','Website operated by ALFA d.o.o.')
        self.assertEqual(r['status'],'VERIFIED')
        self.assertEqual(r['verified_scope'],'https://hosting.example/alfa/')

    def test_business_profile(self):
        self.assertEqual(self.check_page('https://profile.example/company/alfa','ALFA company profile')['status'],'REVIEW')

    def test_dealer_portal(self):
        self.assertEqual(self.check_page('https://portal.example/prodajno-mesto/alfa','ALFA')['status'],'REVIEW')

    def test_exhibitor_portal(self):
        self.assertEqual(self.check_page('https://fair.example/exhibitor/alfa','ALFA')['status'],'REVIEW')

    def test_marketplace_schema_is_not_ownership(self):
        schema=json.dumps({'@type':'Organization','name':'ALFA d.o.o.','taxID':'12345678'})
        self.assertEqual(self.check_page('https://portal.example/searchdealer/alfa','ALFA',f'<script type="application/ld+json">{schema}</script>')['status'],'REVIEW')

    def test_parent_group_site(self):
        r=self.check_page('https://parent.example/locations/alfa','Group locations ALFA')
        self.assertEqual((r['status'],r['relationship']),('GROUP_REVIEW','GROUP_PARENT'))

    def test_group_affiliation_does_not_block_entity_specific_operator(self):
        r=self.check_page('https://alfa.si/','ALFA','Our subsidiaries. Website operated by ALFA d.o.o.')
        self.assertEqual(r['status'],'VERIFIED')

    def test_dedicated_legitimate_subdomain(self):
        c={**COMPANY,'company_name':'SeneCura Radenci d.o.o.'}
        html='<title>SeneCura Dom starejših občanov Radenci</title>SeneCura Radenci d.o.o. SI12345678'
        f=FakeFetcher({'https://radenci.senecura.si/':response(html,'https://radenci.senecura.si/')})
        r=verify(c,'https://radenci.senecura.si/',f)
        self.assertEqual(r['status'],'VERIFIED')
        self.assertEqual(r['verified_scope'],'https://radenci.senecura.si/')

    def test_legitimate_path_scoped_dealer_microsite(self):
        r=self.check_page('https://portal.example/alfa/','ALFA',candidate='https://alfa.si/')
        self.assertEqual(r['status'],'VERIFIED')
        self.assertEqual(r['verified_scope'],'https://portal.example/alfa/')
        self.assertFalse(in_scope('https://portal.example/other/',r['verified_scope']))

    def test_duplicate_listing_pages_never_corroborate_ownership(self):
        p=score_page(COMPANY,response('<title>ALFA company profile</title>'+IDENTITY,'https://listing.example/company/alfa'))
        self.assertEqual(evaluate_ownership(COMPANY,[p]*8)['status'],'REVIEW')

    def test_ambiguous_legal_name_change(self):
        self.assertEqual(self.check_page('https://alfa.si/','ALFA s.p.')['status'],'REVIEW')

    def test_staging_host_not_confirmed_by_legal_details(self):
        self.assertEqual(self.check_page('https://alfa.testni.si/','ALFA','Website operated by ALFA d.o.o.')['status'],'REVIEW')

    def test_products_do_not_disqualify_manufacturer(self):
        self.assertEqual(self.check_page('https://alfa.si/','ALFA','Buy machinery, our products and equipment for sale')['status'],'VERIFIED')

    def test_other_structural_profile_patterns(self):
        for title in ('ALFA job portal','News article ALFA','Chamber member ALFA','Web agency client ALFA','Social network profile ALFA'):
            with self.subTest(title=title):self.assertEqual(self.check_page('https://publisher.example/alfa',title)['status'],'REVIEW')

    def test_similar_name_not_owner(self):
        self.assertEqual(self.check_page('https://alfabeta.si/','ALFA BETA')['status'],'REVIEW')

    def test_organization_schema_alone_not_operator(self):
        schema=json.dumps({'@type':'Organization','name':'ALFA d.o.o.'})
        self.assertEqual(self.check_page('https://host.example/alfa/','ALFA',f'<script type="application/ld+json">{schema}</script>')['status'],'REVIEW')

    def test_website_publisher_proof_preserves_scope(self):
        schema=json.dumps({'@type':'WebSite','url':'https://host.example/alfa/','publisher':{'@type':'Organization','name':'ALFA d.o.o.'}})
        r=self.check_page('https://host.example/alfa/','ALFA',f'<script type="application/ld+json">{schema}</script>')
        self.assertEqual(r['status'],'VERIFIED');self.assertEqual(r['verified_scope'],'https://host.example/alfa/')

class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.owner={'status':'VERIFIED','scope':'https://alfa.si/'}
        self.group={'status':'GROUP_REVIEW','source_scopes':['https://group.example/']}

    def test_same_domain_is_insufficient(self):
        self.assertFalse(email_attribution(COMPANY,'info@alfa.si','https://alfa.si/contact','info@alfa.si',self.owner)['attributable'])

    def test_central_policy_blocks_email_crawl_and_attribution_sources(self):
        for url in ('https://infobel.com/alfa', 'https://trivago.com/alfa', 'https://evendo.com/alfa'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    crawl_site({**COMPANY, 'status':'VERIFIED', 'website':url,
                                'ownership':{'status':'VERIFIED','scope':url}}, FakeFetcher({}))
                result=email_attribution(COMPANY, 'info@example.com', url, IDENTITY,
                                         {'status':'VERIFIED','scope':url})
                self.assertFalse(result['attributable'])

    def test_explicit_context_allows_different_mail_domain(self):
        self.assertTrue(email_attribution(COMPANY,'info@group.example','https://alfa.si/contact','ALFA d.o.o. info@group.example',self.owner)['attributable'])

    def test_group_contact_requires_entity_and_evidenced_source(self):
        for pub,url,ok in [('info@group.example','https://group.example/',False),('ALFA d.o.o. info@group.example','https://group.example/contacts',True),('ALFA d.o.o. info@group.example','https://other.example/',False)]:
            with self.subTest(pub=pub,url=url):self.assertEqual(email_attribution(COMPANY,'info@group.example',url,pub,self.group)['attributable'],ok)

    def test_cross_subsidiary_email_contamination(self):
        self.assertFalse(email_attribution(COMPANY,'beta@group.example','https://group.example/contact','BETA d.o.o. beta@group.example',self.group)['attributable'])

    def test_foreign_country_group_email_contamination(self):
        self.assertFalse(email_attribution(COMPANY,'de@group.example','https://group.example/contact','ALFA Deutschland GmbH de@group.example',self.group)['attributable'])

    def test_publisher_email_contamination(self):
        self.assertFalse(email_attribution(COMPANY,'info@records.example','https://records.example/ddv/12345678',IDENTITY,{'status':'REVIEW','relationship':'THIRD_PARTY'})['attributable'])

    def test_agency_context_rejected(self):
        self.assertFalse(email_attribution(COMPANY,'vendor@alfa.si','https://alfa.si/','Website by ALFA d.o.o. vendor@alfa.si',self.owner)['attributable'])

    def test_different_displayed_address_rejected(self):
        self.assertFalse(email_attribution(COMPANY,'wrong@alfa.si','https://alfa.si/',IDENTITY,self.owner,visible_email='right@alfa.si')['attributable'])

    def test_path_scope_blocks_root_sibling_and_encoded_traversal(self):
        scope='https://portal.example/alfa/'
        for u in ('https://portal.example/','https://portal.example/alfabeta/','https://portal.example/alfa/%252e%252e/beta/','https://other.portal.example/alfa/'):
            self.assertFalse(in_scope(u,scope),u)
        self.assertTrue(in_scope(scope+'contact',scope))

    def test_email_crawl_never_visits_portal_root(self):
        url='https://portal.example/alfa/'
        html=f'<title>ALFA</title><section><h2>ALFA d.o.o.</h2>info@alfa.si</section><a href="/contact">Contact root</a><a href="/beta/contact">Other contact</a>'
        f=FakeFetcher({url:response(html,url)})
        r=crawl_site({**COMPANY,'status':'VERIFIED','website':url,'ownership':{'status':'VERIFIED','scope':url}},f)
        self.assertEqual(r['pages_attempted'],[url]);self.assertEqual([e['email'] for e in r['emails']],['info@alfa.si'])

    def test_contact_cards_do_not_mix_entities(self):
        soup=BeautifulSoup('<main><section><h2>ALFA d.o.o.</h2><a href="mailto:a@alfa.si">Email</a></section><section><h2>BETA d.o.o.</h2><a href="mailto:b@alfa.si">Email</a></section></main>','html.parser')
        pub,block=contact_context(soup.find_all('a')[1],COMPANY,soup)
        self.assertTrue(block['other_entity'])
        self.assertFalse(email_attribution(COMPANY,'b@alfa.si','https://alfa.si/',pub,self.owner,block=block)['attributable'])

    def test_fetcher_rejects_redirect_out_of_path(self):
        f=Fetcher();r=response('','https://portal.example/alfa/');r.status_code=302;r.headers['Location']='/contact'
        with patch.object(f.session,'get',return_value=r) as get,patch('discovery.website_verifier.time.sleep'):
            self.assertIsNone(f.fetch(r.url,allowed_site=r.url));self.assertEqual(get.call_count,1)
        f.close()
