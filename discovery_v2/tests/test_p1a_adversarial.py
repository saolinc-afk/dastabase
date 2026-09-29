"""Synthetic ownership counterexamples; no live data, network or production DBs."""
import unittest
from unittest.mock import patch
from discovery_v2.tests.test_phase_b_p1a_ownership import OwnershipHarness, COMPANY, DOMAIN
from discovery_v2.tests.test_phase_a import FakeFetcher, response
from discovery_v2.interfaces import RecordingFetcher, evaluate_website
from discovery_v2.identity import extract_claims, support_aliases
from discovery_v2.contacts import resolve_contacts, select_default

OPERATOR = 'Website operated by ALFA STROJI d.o.o.'
IDENTITY = 'ALFA STROJI d.o.o.; Glavna ulica 12, 1000 Ljubljana'

class AdversarialTests(unittest.TestCase):
    def setUp(self):
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        guard.start(); self.addCleanup(guard.stop)

    def test_news_at_root_cannot_authorize_publisher(self):
        h = OwnershipHarness(); url = 'https://journal.example/'
        f = RecordingFetcher(FakeFetcher({url: response('<title>ALFA STROJI press release</title><article>ALFA STROJI d.o.o.; VAT: 12345678</article>', url)}), h.writer)
        self.assertFalse(evaluate_website(h.company, url, f)['verified'])
        self.assertTrue(any(o['observation_type'] == 'TAX_NUMBER' for o in h.writer.observations))

    def test_shared_addresses_are_not_site_identity(self):
        for name in ('ALFA STROJI d.o.o.', 'AMBIENT d.o.o.', 'DCT d.o.o.', 'HYB d.o.o.', 'ABC d.o.o.'):
            for body in ('Glavna ulica 12', 'Glavna ulica 12, 1000 Ljubljana', 'Virtual office; Glavna ulica 12, 1000 Ljubljana', 'BETA d.o.o.; Glavna ulica 12, 1000 Ljubljana'):
                with self.subTest(name=name, body=body):
                    h = OwnershipHarness({**COMPANY, 'company_name': name})
                    h.search('bizi.si', 'Glavna ulica 12, 1000 Ljubljana'); h.page('<p>'+body+'</p>')
                    self.assertFalse(h.decide()['verified'])

    def test_split_page_name_address(self):
        h=OwnershipHarness(); h.page('<p>'+OPERATOR+'</p><h1>ALFA STROJI d.o.o.</h1>')
        h.page('<p>Glavna ulica 12</p><a href="mailto:x@example.org">Contact</a>', DOMAIN+'contact')
        self.assertFalse(h.decide()['verified'])

    def test_split_blocks_name_address(self):
        h=OwnershipHarness(); h.page('<section>'+OPERATOR+'</section><section>Glavna ulica 12, 1000 Ljubljana</section>')
        self.assertFalse(h.decide()['verified'])

    def test_coherent_identity_positive(self):
        h=OwnershipHarness(); h.page('<section>'+OPERATOR+'; Glavna ulica 12, 1000 Ljubljana</section>')
        self.assertEqual(h.decide()['rule_id'], 'OWN-02_EXACT_ENTITY_CONTACT_PAGE')

    def test_duplicate_publisher_without_fetch(self):
        h=OwnershipHarness(); h.search('bizi.si', IDENTITY); h.search('www.bizi.si', IDENTITY)
        self.assertFalse(h.decide()['verified'])

    def test_distinct_sources_without_fetch(self):
        h=OwnershipHarness(); h.search('bizi.si', IDENTITY); h.search('companywall.si', IDENTITY)
        self.assertFalse(h.decide()['verified'])
        self.assertEqual(len(h.writer.evidence_rows),2)

    def test_fetch_failure_never_helps(self):
        for error in ('certificate has expired','hostname mismatch certificate','HTTP 403','EOF','timeout','redirect'):
            h=OwnershipHarness(); h.search('bizi.si',IDENTITY); h.search('companywall.si',IDENTITY)
            h.failures[DOMAIN]=[error]; self.assertFalse(h.decide()['verified'])
            h.page('<p>Welcome</p>'); self.assertFalse(h.decide()['verified'])

    def test_sibling_subsidiary(self):
        h=OwnershipHarness(); h.page('<section>'+OPERATOR+'; Glavna ulica 12, 1000 Ljubljana; member of group</section>', 'https://group.example/right/')
        self.assertFalse(h.decide('https://group.example/wrong/')['verified'])
        self.assertEqual(h.decide('https://group.example/right/')['verification_scope'], 'https://group.example/right/')

    def test_group_root_redirect(self):
        h=OwnershipHarness(); url='https://group.example/right/'
        f=RecordingFetcher(FakeFetcher({url:response('<section>'+IDENTITY+'; member of group</section>','https://group.example/')}),h.writer)
        self.assertFalse(evaluate_website(h.company,url,f)['verified'])

    def test_strong_legal_page_redirect_cannot_broaden_scope(self):
        h=OwnershipHarness(); url='https://tenant.example/contact/alfa'
        html='<section>ALFA STROJI d.o.o.; VAT: 12345678</section>'
        f=RecordingFetcher(FakeFetcher({url:response(html,'https://tenant.example/')}),h.writer)
        self.assertFalse(evaluate_website(h.company,url,f)['verified'])

    def test_group_root_footer(self):
        h=OwnershipHarness(); h.page('<footer>'+IDENTITY+'; member of group</footer>', 'https://group.example/')
        self.assertFalse(h.decide('https://group.example/')['verified'])

    def test_real_writer_shape_scopes_target_container_away_from_parent_footer(self):
        company = {**COMPANY, 'company_name': 'EKOREL d.o.o.',
                   'address': 'Cesta 7, 1000 Ljubljana'}
        h = OwnershipHarness(company)
        url = 'https://ekorel.parent.si/kontaktne-osebe-ekorel/'
        html = ('<main><h1>EKOREL d.o.o.</h1><div>Cesta 7</div>'
                '<div>1000 Ljubljana</div></main>'
                '<footer>PARENT HOLDING d.o.o.</footer>')
        fetcher = RecordingFetcher(FakeFetcher({url: response(html, url)}), h.writer)
        result = evaluate_website(company, url, fetcher)
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertEqual(result['p1a']['rule_id'], 'OWN-07_SCOPED_ENTITY_PAGE')
        self.assertEqual(result['relationship'], 'ENTITY_PAGE_ON_GROUP_DOMAIN')
        self.assertNotIn('CONFLICTING_LEGAL_ENTITY', result['p1a']['blockers'])
        witness_blocks = {o['value'].get('qualifiers', {}).get('block_id')
                          for o in h.writer.observations
                          if o['observation_id'] in result['p1a']['identity_evidence']}
        self.assertEqual(len(witness_blocks), 1)

    def test_real_writer_shape_preserves_duplicate_redirect_association(self):
        company = {**COMPANY, 'company_name': 'GEN OVE d.o.o.'}
        h = OwnershipHarness(company)
        candidate = 'https://gek.si/'
        root = 'https://www.gen-energija.si/'
        entity = 'https://www.gen-energija.si/gen/skupina-gen/gen-ove/'
        root_html = f'<a href="{entity}">Podjetje GEN OVE</a>'
        entity_html = ('<main><h1>GEN OVE d.o.o.</h1><div>VAT: 12345678</div>'
                       '<div>Glavna ulica 12, 1000 Ljubljana</div></main>')
        # A previous candidate already recorded the same canonical root. The
        # later gek.si assessment must retain its own redirect association.
        h.writer.page(root, response(root_html, root))
        fetcher = RecordingFetcher(FakeFetcher({
            candidate: response(root_html, root),
            entity: response(entity_html, entity),
        }), h.writer)
        result = evaluate_website(company, candidate, fetcher)
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertEqual(result['p1a']['rule_id'], 'OWN-07_SCOPED_ENTITY_PAGE')
        self.assertEqual(result['verified_scope'], entity)
        redirect_rows = [row for row in h.writer.evidence_rows.values()
                         if row.get('requested_url') == candidate
                         and row.get('final_url') == root]
        self.assertEqual(len(redirect_rows), 1)

    def test_real_writer_shape_same_block_conflicting_operator_still_blocks(self):
        h = OwnershipHarness(); url = 'https://group.example/skupina/alfa-stroji/'
        html = ('<main><p>ALFA STROJI d.o.o.; VAT: 12345678; '
                'Website operated by BETA HOLDING d.o.o.</p></main>')
        fetcher = RecordingFetcher(FakeFetcher({url: response(html, url)}), h.writer)
        result = evaluate_website(h.company, url, fetcher)
        self.assertEqual(result['status'], 'REVIEW')
        self.assertIn('CONFLICTING_LEGAL_ENTITY', result['p1a']['blockers'])

    def test_real_writer_shape_canonical_redirect_uses_standalone_confidence(self):
        company = {**COMPANY, 'company_name': 'MARUŠIČ d.o.o.'}
        h = OwnershipHarness(company)
        candidate = 'https://marusic.si/'
        final = 'https://marusic-beechwood.com/'
        html = ('<main><h1>MARUŠIČ d.o.o.</h1><div>Glavna ulica 12</div>'
                '<div>1000 Ljubljana</div></main>')
        fetcher = RecordingFetcher(FakeFetcher({candidate: response(html, final)}), h.writer)
        result = evaluate_website(company, candidate, fetcher)
        self.assertEqual(result['status'], 'HIGH')
        self.assertEqual(result['relationship'], 'STANDALONE')
        self.assertEqual(result['p1a']['confidence_rule_id'],
                         'CONF-01_LEGAL_ADDRESS_CONVERGENCE')
        self.assertEqual(result['verified_scope'], final)

    def test_parent_identifier_conflict(self):
        h=OwnershipHarness(); url='https://group.example/right/'
        h.page('<section>'+IDENTITY+'; VAT: 12345678; member of group</section><footer>Registration: 1111111</footer>',url)
        self.assertFalse(h.decide(url)['verified'])

    def test_tenant_contact_scope(self):
        h=OwnershipHarness(); url='https://portal.example/contact/alfa'
        f=RecordingFetcher(FakeFetcher({url:response('<section>'+OPERATOR+'; VAT: 12345678</section>',url)}),h.writer)
        d=evaluate_website(h.company,url,f)
        self.assertTrue(d['verified']); self.assertEqual(d['verified_scope'],url+'/')

    def test_address_does_not_manufacture_alias(self):
        claims=support_aliases(extract_claims(COMPANY,'ALFA STROJI IN SISTEMI d.o.o.; Glavna ulica 12, 1000 Ljubljana'))
        self.assertFalse(any(c['value']['verification_status']=='ALIAS_SUPPORTED' for c in claims))
        h=OwnershipHarness(); h.search('bizi.si',IDENTITY)
        h.page('<section>ALFA STROJI IN SISTEMI d.o.o.; Glavna ulica 12, 1000 Ljubljana</section>')
        self.assertFalse(h.decide()['verified'])

    def test_explicit_brand_mapping(self):
        h=OwnershipHarness(); h.page('<section>'+OPERATOR+'; trading as ALFA MACHINES; VAT: 12345678</section>')
        self.assertEqual(h.decide()['rule_id'],'OWN-06_SUPPORTED_BRAND')

    def test_address_components(self):
        for address in ('Glavna ulica 123, 1000 Ljubljana','Glavna ulica 12A, 1000 Ljubljana','Glavna ulica 12, 2000 Maribor'):
            h=OwnershipHarness(); h.page('<section>'+OPERATOR+'; '+address+'</section>')
            self.assertFalse(h.decide()['verified'],address)
        h=OwnershipHarness(); h.page('<section>'+OPERATOR+'; Glavna ulica 12</section><footer>1000 Ljubljana</footer>')
        self.assertFalse(h.decide()['verified'])

    def test_identifier_complete_tokens(self):
        for token in ('12345678XYZ','X12345678','123456789'):
            h=OwnershipHarness(); h.page('<section>'+OPERATOR+'; VAT: '+token+'</section>')
            self.assertFalse(h.decide()['verified'],token)
        for token in ('1234 5678','SI12345678'):
            h=OwnershipHarness(); h.page('<section>'+OPERATOR+'; VAT: '+token+'</section>')
            self.assertTrue(h.decide()['verified'],token)

    def test_writerless_conflict_fails_closed(self):
        f=FakeFetcher({DOMAIN:response('<title>ALFA STROJI</title><p>VAT: 12345678; Registration: 1111111</p>',DOMAIN)})
        self.assertFalse(evaluate_website(COMPANY,DOMAIN,f)['verified'])

    def test_unrelated_directory_url_and_stale_link(self):
        for body in ('Advertisement https://alfa-stroji.si/', 'Website https://alfa-stroji.si/'):
            h=OwnershipHarness(); h.writer.search_result({'url':'https://bizi.si/record','title':COMPANY['company_name'],'body':IDENTITY+'; '+body},'LEGAL_COMPANY_CONTACT','offline','fixture',1)
            h.page('<section>BETA d.o.o.; Glavna ulica 12, 1000 Ljubljana</section>')
            self.assertFalse(h.decide()['verified'])

    def test_successful_fetch_brand_title_not_operator(self):
        h=OwnershipHarness(); h.page('<title>ALFA STROJI</title><h1>ALFA STROJI d.o.o.</h1><p>VAT: 12345678</p>')
        self.assertFalse(h.decide()['verified'])

    def test_unsupported_identifier_preserved_for_phone_exclusion(self):
        h=OwnershipHarness(); h.page('<p>Registration: 12345678901</p><section><h2>ALFA STROJI d.o.o. Contact</h2><p>Telefon: 12345678901</p></section>')
        contacts=resolve_contacts(h.company,h.writer.observations,{'status':'VERIFIED','scope':DOMAIN},h.responses)
        self.assertIsNone(select_default(contacts,'PHONE'))
        self.assertFalse(any(o['observation_type']=='PHONE_CANDIDATE' for o in h.writer.observations))

    def test_group_membership_mention_is_not_operator(self):
        h=OwnershipHarness(); url='https://unknown.example/right/'
        h.page('<section>'+IDENTITY+'; member of group</section>',url)
        self.assertFalse(h.decide(url)['verified'])

    def test_negated_group_statement(self):
        h=OwnershipHarness(); url='https://group.example/right/'
        h.page('<section>'+IDENTITY+'; not a member of group</section>',url)
        self.assertFalse(h.decide(url)['verified'])

    def test_operator_and_identity_cannot_cross_blocks(self):
        h=OwnershipHarness(); h.page('<p>'+OPERATOR+'</p><p>VAT: 12345678</p>')
        self.assertFalse(h.decide()['verified'])

    def test_editorial_retains_bounded_operator_quote_without_authorizing(self):
        h=OwnershipHarness(); h.page('<title>News article</title><article><section>'+OPERATOR+'; VAT: 12345678</section></article>')
        self.assertFalse(h.decide()['verified'])
        self.assertTrue(any(o['observation_type']=='SITE_OPERATOR' for o in h.writer.observations))

    def test_source_aliases_and_unknown_independence(self):
        h=OwnershipHarness()
        for host in ('bizi.si','www.bizi.si','foo.bizi.si','companywall.si','companywall.eu','www.ebonitete.si','ebonitete.si'):
            h.search(host,IDENTITY)
        d=h.decide()
        self.assertEqual(set(d['source_publishers']),{'bizi','companywall','ebonitete'})
        self.assertFalse(d['verified'])

    def test_unrelated_url_is_not_trusted_link(self):
        h=OwnershipHarness(); h.writer.search_result({'url':'https://bizi.si/record','title':COMPANY['company_name'],
            'body':IDENTITY+'; Advertisement '+DOMAIN},'LEGAL_COMPANY_CONTACT','offline','fixture',1)
        h.page('<p>'+OPERATOR+'</p>')
        self.assertFalse(h.decide()['verified'])

    def test_rule_witnesses_are_local_not_all_positive_observations(self):
        h=OwnershipHarness(); h.search('bizi.si',IDENTITY)
        h.page('<section>'+OPERATOR+'; VAT: 12345678</section>')
        d=h.decide(); by_id={o['observation_id']:o for o in h.writer.observations}
        self.assertTrue(d['verified'])
        self.assertTrue(d['operator_evidence'])
        self.assertEqual(d['site_operator_anchors'],d['operator_evidence'])
        self.assertTrue(all(by_id[i]['observation_type']=='SITE_OPERATOR' for i in d['operator_evidence']))
        self.assertEqual(len(d['supporting_evidence_ids']),1)
        self.assertTrue(all(by_id[i]['value']['qualifiers'].get('block_id') for i in d['supporting_observation_ids']))

    def test_identifier_conflicts_both_directions(self):
        for ids in ('VAT: 12345678; Registration: 1111111','Registration: 7654321; VAT: 87654321'):
            h=OwnershipHarness(); h.page('<section>'+OPERATOR+'; '+ids+'</section>')
            d=h.decide(); self.assertFalse(d['verified']); self.assertIn('EXACT_IDENTIFIER_CONFLICT',d['blockers'])

    def test_subdomain_strong_legal_page_retains_path_scope(self):
        url='https://tenant.hosting.example/contact'
        h=OwnershipHarness(); h.page('<section>'+IDENTITY+'; VAT: 12345678</section>',url)
        decision=h.decide(url)
        self.assertTrue(decision['verified'])
        self.assertEqual(decision['authorization_basis'],'STRONG_LEGAL_PAGE')
        self.assertEqual(decision['verification_scope'],url+'/')
        h=OwnershipHarness(); h.page('<section>'+OPERATOR+'; VAT: 12345678</section>',url)
        self.assertEqual(h.decide(url)['verification_scope'],url+'/')

    def test_identifier_and_ownership_rules_ignore_uuid_values(self):
        h=OwnershipHarness(); h.page('<section>'+OPERATOR+'; VAT: 12345678</section>')
        before=h.decide()
        for index,o in enumerate(h.writer.observations): o['observation_id']='uuid-'+str(10000-index)
        after=h.decide()
        self.assertEqual((before['rule_id'],before['verification_scope']), (after['rule_id'],after['verification_scope']))

    def test_section_does_not_merge_separate_publications(self):
        h=OwnershipHarness(); h.page('<section><p>'+OPERATOR+'</p><p>Warehouse customer: Glavna ulica 12, 1000 Ljubljana</p></section>')
        self.assertFalse(h.decide()['verified'])

    def test_encoded_tenant_path_cannot_become_query_scope(self):
        from discovery.ownership import in_scope
        h=OwnershipHarness(); url='https://portal.example/contact/alfa%3Fother'
        h.page('<section>'+OPERATOR+'; VAT: 12345678</section>',url)
        d=h.decide(url); self.assertTrue(d['verified'])
        self.assertFalse(in_scope('https://portal.example/contact/alfa/sibling',d['verification_scope']))

        h=OwnershipHarness(); h.page('<section>'+IDENTITY+'; VAT: 12345678</section>',url)
        d=h.decide(url); self.assertTrue(d['verified'])
        self.assertEqual(d['authorization_basis'],'STRONG_LEGAL_PAGE')
        self.assertFalse(in_scope('https://portal.example/contact/alfa/sibling',d['verification_scope']))

if __name__=='__main__': unittest.main()
