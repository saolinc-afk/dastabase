"""Offline Phase B P1A identity and named ownership-rule tests."""
import unittest

from discovery_v2.evidence import EvidenceWriter
from discovery_v2.identity import (address_parts, extract_claims, identifiers_equal,
                                   name_forms, normalize_identifier)
from discovery_v2.ownership import evaluate, fetch_state, page_classification, validate_assessment
from discovery_v2.tests.test_live_regressions import MemoryStore
from discovery_v2.tests.test_phase_a import response

COMPANY = dict(id=77, company_name='ALFA STROJI d.o.o.', tax_number='12345678',
               registration_number='7654321', address='Glavna ulica 12, 1000 Ljubljana',
               municipality='1000 Ljubljana')
DOMAIN = 'https://alfa-stroji.si/'


class RecordedFetcher:
    def __init__(self, responses=None, failures=None):
        self.responses = responses or {}
        self.failures = failures or {}


def legacy(**kwargs):
    value = dict(evidence=[], relationship='UNRESOLVED', verified=False, status='REVIEW')
    value.update(kwargs)
    return value


class OwnershipHarness:
    def __init__(self, company=None):
        self.company = company or COMPANY
        self.writer = EvidenceWriter(MemoryStore(), {}, self.company)
        self.responses = {}
        self.failures = {}

    def search(self, host, body, url=DOMAIN, rank=1):
        self.writer.search_result({'url': f'https://{host}/record', 'title': self.company['company_name'],
                                   'body': body + '; Website ' + url},
                                  'LEGAL_COMPANY_CONTACT', 'offline', 'fixture', rank)

    def page(self, html, url=DOMAIN):
        page = response(html, url)
        self.responses[url] = page
        self.writer.page(url, page)

    def decide(self, url=DOMAIN, legacy_result=None):
        return evaluate(self.company, url, self.writer,
                        RecordedFetcher(self.responses, self.failures), legacy_result or legacy())


class IdentityTests(unittest.TestCase):
    def test_name_address_and_identifier_normalization(self):
        self.assertEqual(name_forms('ALFA STROJI d. o. o.')['full'], 'alfa stroji doo')
        self.assertEqual(name_forms('ALFA STROJI doo')['distinctive'], 'alfa stroji')
        self.assertEqual(address_parts(COMPANY)['house_number'], '12')
        self.assertEqual(normalize_identifier(' SI 123-45-678 '),
                         {'digits': '12345678', 'si_prefix': True, 'raw': 'SI 123-45-678'})

    def test_structured_claim_contract_and_exact_comparisons(self):
        claims = extract_claims(COMPANY, 'ALFA STROJI d.o.o., Glavna ulica 12, 1000 Ljubljana; ID za DDV SI12345678')
        kinds = {item['kind'] for item in claims}
        self.assertTrue({'LEGAL_NAME', 'STREET', 'ADDRESS', 'POSTAL_CODE', 'MUNICIPALITY', 'TAX_NUMBER'} <= kinds)
        for item in claims:
            self.assertEqual(item['value']['claim_version'], 2)
            self.assertIn(item['value']['verification_status'],
                          ('EXACT_MATCH', 'COMPATIBLE', 'ALIAS_SUPPORTED', 'CONFLICT', 'UNVERIFIED'))

    def test_registration_plus_000_is_the_only_extended_equivalence(self):
        self.assertTrue(identifiers_equal('REGISTRATION_NUMBER', '6013350', '6013350000'))
        self.assertFalse(identifiers_equal('REGISTRATION_NUMBER', '6013350', '6013350999'))
        self.assertFalse(identifiers_equal('REGISTRATION_NUMBER', '6013350', '601335'))
        self.assertFalse(identifiers_equal('TAX_NUMBER', '1234567', '1234567000'))
        company = {**COMPANY, 'registration_number': '6013350'}
        claims = extract_claims(company, 'Matična številka: 6013350000')
        self.assertEqual(next(c for c in claims if c['kind'] == 'REGISTRATION_NUMBER')
                         ['value']['verification_status'], 'EXACT_MATCH')

    def test_expanded_name_address_is_not_relationship(self):
        weak = extract_claims(COMPANY, 'ALFA STROJI IN SISTEMI d.o.o.')
        self.assertTrue(any(c['kind'] == 'ALIAS' and c['value']['verification_status'] == 'UNVERIFIED' for c in weak))
        strong = extract_claims(COMPANY, 'ALFA STROJI IN SISTEMI d.o.o., Glavna ulica 12, 1000 Ljubljana')
        from discovery_v2.identity import support_aliases
        self.assertFalse(any(c['kind'] == 'ALIAS' and c['value']['verification_status'] == 'ALIAS_SUPPORTED'
                            for c in support_aliases(strong)))


class NamedRuleTests(unittest.TestCase):
    def test_own01_exact_identifier_on_site(self):
        h = OwnershipHarness(); h.page('<section>Website operated by ALFA STROJI d.o.o.; ID za DDV SI12345678</section>')
        decision = h.decide()
        self.assertTrue(decision['verified']); self.assertEqual(decision['rule_id'], 'OWN-01_EXACT_IDENTIFIER_ON_SITE')
        self.assertEqual(decision['authorization_basis'], 'EXPLICIT_OPERATOR')

    def test_own01_strong_legal_page_tax_and_registration(self):
        cases = (
            (DOMAIN + 'kontakt', 'ID za DDV SI12345678'),
            (DOMAIN + 'legal', 'Registration number: 7654321'),
        )
        for url, identifier in cases:
            with self.subTest(url=url):
                h = OwnershipHarness()
                h.page('<section>ALFA STROJI d.o.o.; ' + identifier + '</section>', url)
                decision = h.decide(url)
                self.assertTrue(decision['verified'])
                self.assertEqual(decision['rule_id'], 'OWN-01_EXACT_IDENTIFIER_ON_SITE')
                self.assertEqual(decision['authorization_basis'], 'STRONG_LEGAL_PAGE')
                self.assertEqual(decision['operator_evidence'], [])

    def test_own02_exact_name_and_address_contact_page(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<section>Website operated by ALFA STROJI d.o.o.; Glavna ulica 12, 1000 Ljubljana</section>', url)
        decision = h.decide(url)
        self.assertTrue(decision['verified']); self.assertEqual(decision['rule_id'], 'OWN-02_EXACT_ENTITY_CONTACT_PAGE')
        self.assertEqual(decision['authorization_basis'], 'EXPLICIT_OPERATOR')

    def test_own02_strong_legal_contact_page(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<section>ALFA STROJI d.o.o.; Glavna ulica 12, 1000 Ljubljana</section>', url)
        decision = h.decide(url)
        self.assertTrue(decision['verified'])
        self.assertEqual(decision['rule_id'], 'OWN-02_EXACT_ENTITY_CONTACT_PAGE')
        self.assertEqual(decision['authorization_basis'], 'STRONG_LEGAL_PAGE')
        self.assertEqual(decision['operator_evidence'], [])

    def test_strong_legal_page_requires_safe_role_and_complete_same_block_identity(self):
        cases = (
            (DOMAIN, '<section>ALFA STROJI d.o.o.; VAT: 12345678</section>'),
            (DOMAIN + 'kontakt', '<section>ALFA STROJI d.o.o.</section>'),
            (DOMAIN + 'kontakt', '<section>VAT: 12345678</section>'),
            (DOMAIN + 'kontakt', '<section>Glavna ulica 12, 1000 Ljubljana</section>'),
            (DOMAIN + 'kontakt', '<section>ALFA STROJI d.o.o.; Glavna ulica 12</section>'),
            (DOMAIN + 'kontakt', '<p>ALFA STROJI d.o.o.</p><p>VAT: 12345678</p>'),
            (DOMAIN + 'kontakt', '<p>ALFA STROJI d.o.o.</p><p>Glavna ulica 12, 1000 Ljubljana</p>'),
        )
        for url, html in cases:
            with self.subTest(url=url, html=html):
                h = OwnershipHarness(); h.page(html, url)
                self.assertFalse(h.decide(url)['verified'])

    def test_strong_legal_page_does_not_authorize_news_directory_or_group(self):
        cases = (
            (DOMAIN + 'kontakt', '<title>News</title><article><section>ALFA STROJI d.o.o.; VAT: 12345678</section></article>'),
            (DOMAIN + 'kontakt', '<title>Company directory</title><section>ALFA STROJI d.o.o.; VAT: 12345678</section>'),
            (DOMAIN + 'kontakt', '<section>ALFA STROJI d.o.o.; VAT: 12345678; member of group</section>'),
        )
        for url, html in cases:
            with self.subTest(html=html):
                h = OwnershipHarness(); h.page(html, url)
                self.assertFalse(h.decide(url)['verified'])

    def test_strong_legal_page_keeps_domain_and_conflict_blockers(self):
        identity = '<section>ALFA STROJI d.o.o.; VAT: 12345678</section>'
        blocked = OwnershipHarness(); blocked_url = 'https://bizi.si/kontakt'
        blocked.page(identity, blocked_url)
        self.assertFalse(blocked.decide(blocked_url)['verified'])
        self.assertIn('DISALLOWED_PUBLISHER_DOMAIN', blocked.decide(blocked_url)['blockers'])

        parked = OwnershipHarness(); parked_url = DOMAIN + 'kontakt'
        parked.page(identity, parked_url)
        parked_decision = parked.decide(parked_url,
            legacy(evidence=[{'signals': ['parked_domain']}]))
        self.assertFalse(parked_decision['verified'])
        self.assertIn('PARKED_DOMAIN', parked_decision['blockers'])

        conflicting_id = OwnershipHarness()
        conflicting_id.page('<section>ALFA STROJI d.o.o.; VAT: 87654321</section>', parked_url)
        self.assertIn('EXACT_IDENTIFIER_CONFLICT',
                      conflicting_id.decide(parked_url)['blockers'])

        conflicting_entity = OwnershipHarness()
        conflicting_entity.page(identity + '<footer>BETA HOLDING d.o.o.</footer>', parked_url)
        self.assertIn('CONFLICTING_LEGAL_ENTITY',
                      conflicting_entity.decide(parked_url)['blockers'])

    def test_forged_strong_legal_page_basis_fails_revalidation(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<section>ALFA STROJI d.o.o.; VAT: 12345678</section>', url)
        decision = h.decide(url)
        assessment = {'verified': True, 'status': 'VERIFIED',
            'verified_scope': decision['verification_scope'],
            'relationship': decision['relationship'], 'p1a': decision,
            'ownership': {'status': 'VERIFIED', 'scope': decision['verification_scope'],
                          'relationship': decision['relationship']}}
        fetcher = RecordedFetcher(h.responses, h.failures)
        self.assertTrue(validate_assessment(COMPANY, url, assessment, h.writer, fetcher))
        assessment['p1a'] = {**decision, 'authorization_basis': 'EXPLICIT_OPERATOR'}
        self.assertFalse(validate_assessment(COMPANY, url, assessment, h.writer, fetcher))

    def test_own03_trusted_link_plus_site_identity(self):
        h = OwnershipHarness(); h.search('bizi.si', 'ALFA STROJI d.o.o., Glavna ulica 12, 1000 Ljubljana')
        h.page('<p>Website operated by ALFA STROJI d.o.o.</p>')
        self.assertEqual(h.decide()['rule_id'], 'OWN-03_TRUSTED_LINK_PLUS_SITE_IDENTITY')

    def test_own04_unknown_source_independence_remains_review(self):
        h = OwnershipHarness()
        h.search('directory-one.si', 'ALFA STROJI d.o.o., Glavna ulica 12, 1000 Ljubljana', rank=1)
        h.search('directory-two.si', 'ALFA STROJI d.o.o.', rank=2)
        h.page('<p>Website operated by ALFA STROJI d.o.o.</p>')
        self.assertFalse(h.decide()['verified'])

    def test_name_domain_and_email_alone_remain_review(self):
        for body in ('ALFA STROJI IN SISTEMI d.o.o.', 'Email info@alfa-stroji.si', ''):
            with self.subTest(body=body):
                h = OwnershipHarness(); h.search('directory-one.si', body); h.page('<h1>Welcome</h1>')
                self.assertFalse(h.decide()['verified'])

    def test_identifier_conflicts_are_hard_blockers(self):
        for label, value in (('ID za DDV', '87654321'), ('Matična številka', '1111111')):
            with self.subTest(label=label):
                h = OwnershipHarness(); h.page(f'<h1>ALFA STROJI d.o.o.</h1><p>{label}: {value}</p>')
                decision = h.decide()
                self.assertFalse(decision['verified']); self.assertIn('EXACT_IDENTIFIER_CONFLICT', decision['blockers'])

    def test_conflicting_legal_entity_blocks_unbounded_page(self):
        h = OwnershipHarness()
        h.page('<h1>ALFA STROJI d.o.o.</h1><p>ID za DDV SI12345678</p><footer>BETA HOLDING d.o.o.</footer>')
        decision = h.decide()
        self.assertFalse(decision['verified']); self.assertIn('CONFLICTING_LEGAL_ENTITY', decision['blockers'])

    def test_incidental_entities_do_not_poison_target_identity(self):
        cases = (
            'Website by WEBTIM d.o.o.',
            'Privacy and cookie software provider PRIVACY TECH d.o.o.',
            'Customer reference: BETA KUPEC d.o.o.',
            'Formerly known as STARA ALFA d.o.o.',
        )
        for incidental in cases:
            with self.subTest(incidental=incidental):
                h = OwnershipHarness(); url = DOMAIN + 'kontakt'
                h.page('<section>ALFA STROJI d.o.o.; VAT: 12345678</section>'
                       f'<footer>{incidental}</footer>', url)
                self.assertNotIn('CONFLICTING_LEGAL_ENTITY', h.decide(url)['blockers'])

    def test_vendor_entity_and_identifier_do_not_poison_target(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<h1>ALFA STROJI d.o.o.</h1><p>VAT: 12345678</p>'
               '<footer>Website developed by WEBTIM d.o.o.; '
               'Registration number: 9876543000</footer>', url)
        decision = h.decide(url)
        self.assertNotIn('CONFLICTING_LEGAL_ENTITY', decision['blockers'])
        self.assertNotIn('EXACT_IDENTIFIER_CONFLICT', decision['blockers'])

    def test_privacy_software_provider_identifier_does_not_poison_target(self):
        h = OwnershipHarness(); url = DOMAIN + 'legal'
        h.page('<section>ALFA STROJI d.o.o.; VAT: 12345678</section>'
               '<p>We use software provided by PRIVACY TECH d.o.o., '
               'registration number 9876543000.</p>', url)
        self.assertNotIn('EXACT_IDENTIFIER_CONFLICT', h.decide(url)['blockers'])

    def test_target_legal_name_with_prose_prefix_is_not_another_entity(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<section>Projekt podjetja ALFA STROJI d.o.o.; VAT: 12345678</section>', url)
        self.assertNotIn('CONFLICTING_LEGAL_ENTITY', h.decide(url)['blockers'])

    def test_different_operator_and_foreign_identity_remain_blocked(self):
        for text in ('Website operated by BETA HOLDING d.o.o.',
                     'Foreign operator BETA HOLDING d.o.o.'):
            with self.subTest(text=text):
                h = OwnershipHarness(); url = DOMAIN + 'kontakt'
                h.page('<section>ALFA STROJI d.o.o.; VAT: 12345678</section>'
                       f'<footer>{text}</footer>', url)
                self.assertIn('CONFLICTING_LEGAL_ENTITY', h.decide(url)['blockers'])

    def test_different_operator_identifier_is_still_a_hard_conflict(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<h1>ALFA STROJI d.o.o.</h1>'
               '<footer>Website operated by BETA HOLDING d.o.o.; '
               'registration number 9876543000</footer>', url)
        decision = h.decide(url)
        self.assertIn('CONFLICTING_LEGAL_ENTITY', decision['blockers'])
        self.assertIn('EXACT_IDENTIFIER_CONFLICT', decision['blockers'])

    def test_target_identifier_is_still_a_hard_conflict(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<section>ALFA STROJI d.o.o.; VAT: 87654321</section>', url)
        self.assertIn('EXACT_IDENTIFIER_CONFLICT', h.decide(url)['blockers'])

    def test_historical_entity_is_observed_without_becoming_site_identity(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<section>ALFA STROJI d.o.o.; VAT: 12345678</section>'
               '<p>Formerly known as STARA ALFA d.o.o.</p>', url)
        decision = h.decide(url)
        self.assertNotIn('CONFLICTING_LEGAL_ENTITY', decision['blockers'])
        self.assertTrue(any(o['observation_type'] == 'ALIAS'
                            for o in h.writer.observations))

    def test_parent_or_sibling_context_does_not_become_standalone(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<section>ALFA STROJI d.o.o.; VAT: 12345678; member of group</section>'
               '<footer>BETA HOLDING d.o.o.</footer>', url)
        self.assertEqual(h.decide(url)['status'], 'REVIEW')

    def test_generic_name_still_requires_exact_anchor(self):
        company = {**COMPANY, 'company_name': 'AMBIENT d.o.o.'}
        h = OwnershipHarness(company); h.page('<h1>Ambient</h1>')
        self.assertFalse(h.decide()['verified'])

    def test_supported_and_unsupported_expanded_names(self):
        supported = OwnershipHarness()
        supported.search('bizi.si', 'ALFA STROJI d.o.o., Glavna ulica 12, 1000 Ljubljana')
        supported.page('<section>Website operated by ALFA STROJI d.o.o.; trading as ALFA MACHINES; VAT: 12345678</section>')
        decision = supported.decide()
        self.assertTrue(decision['verified']); self.assertEqual(decision['relationship'], 'BRAND_OF_ENTITY')
        unsupported = OwnershipHarness(); unsupported.page('<h1>ALFA STROJI IN SISTEMI d.o.o.</h1>')
        self.assertFalse(unsupported.decide()['verified'])

    def test_group_root_review_and_bounded_subsidiary(self):
        root = OwnershipHarness(); root.page('<h1>ALFA Group</h1><p>Member of group</p>')
        self.assertEqual(root.decide()['relationship'], 'GROUP_PARENT'); self.assertFalse(root.decide()['verified'])
        sub = OwnershipHarness(); url = 'https://group.example/alfa-stroji/'
        sub.page('<section>Website operated by ALFA STROJI d.o.o.; Glavna ulica 12, 1000 Ljubljana; member of group</section>', url)
        decision = sub.decide(url)
        self.assertEqual(decision['rule_id'], 'OWN-05_SUBSIDIARY_SCOPE')
        self.assertEqual(decision['verification_scope'], url)

    def test_related_foreign_entity_and_implied_brand_remain_review(self):
        related = OwnershipHarness(); related.page('<h1>BETA GmbH</h1><p>Foreign subsidiary</p>')
        self.assertFalse(related.decide()['verified'])
        implied = OwnershipHarness(); implied.search('bizi.si', 'ALFA STROJI d.o.o.')
        implied.page('<h1>Alfa Brand</h1>')
        self.assertFalse(implied.decide()['verified'])

    def test_parked_and_directory_domains_never_verify(self):
        parked = OwnershipHarness(); parked.page('<h1>ALFA STROJI d.o.o.</h1><p>ID za DDV SI12345678</p>')
        decision = parked.decide(legacy_result=legacy(evidence=[{'signals': ['parked_domain']}]))
        self.assertFalse(decision['verified']); self.assertIn('PARKED_DOMAIN', decision['blockers'])
        directory = OwnershipHarness(); directory.page('<p>ID za DDV SI12345678</p>', 'https://bizi.si/alfa')
        self.assertFalse(directory.decide('https://bizi.si/alfa')['verified'])


class FetchAndPageTests(unittest.TestCase):
    def test_fetch_failures_are_uncertain_not_negative_identity(self):
        cases = {'certificate has expired': 'TLS_EXPIRED', 'hostname mismatch certificate': 'TLS_HOSTNAME_MISMATCH',
                 'HTTP 403 access denied': 'HTTP_FORBIDDEN', 'unexpected EOF': 'CONNECTION_EOF',
                 'read timeout': 'TIMEOUT', 'Redirect outside accepted site': 'REDIRECT_UNRESOLVED'}
        for error, expected in cases.items():
            with self.subTest(error=error):
                state = fetch_state(RecordedFetcher(failures={DOMAIN: [error]}), DOMAIN)
                self.assertEqual(state['status'], expected)

    def test_tls_hostname_mismatch_does_not_transfer_authority(self):
        fetcher = RecordedFetcher(failures={DOMAIN: ['certificate hostname mismatch: other.example']})
        self.assertEqual(fetch_state(fetcher, DOMAIN)['status'], 'TLS_HOSTNAME_MISMATCH')
        self.assertEqual(fetch_state(fetcher, 'https://other.example/')['status'], 'NOT_FETCHED')

    def test_no_fetch_never_verifies_even_with_two_sources_and_exact_anchor(self):
        h = OwnershipHarness(); h.search('source-one.si', 'ALFA STROJI d.o.o., Glavna ulica 12, 1000 Ljubljana')
        h.search('source-two.si', 'ALFA STROJI d.o.o.')
        h.failures[DOMAIN] = ['HTTP 403 access denied']
        decision = h.decide()
        self.assertFalse(decision['verified']); self.assertFalse(decision['verified_without_fetch']); self.assertEqual(decision['fetch_state']['status'], 'HTTP_FORBIDDEN')
        weak = OwnershipHarness(); weak.search('source-one.si', 'ALFA STROJI d.o.o. info@alfa-stroji.si')
        weak.search('source-two.si', 'ALFA STROJI d.o.o. Tel: 012345678')
        weak.failures[DOMAIN] = ['timeout']
        self.assertFalse(weak.decide()['verified'])

    def test_news_label_does_not_erase_identity_and_uncertain_preserves_claims(self):
        h = OwnershipHarness(); url = DOMAIN + 'novice/kontakt'
        h.page('<article><h1>ALFA STROJI d.o.o.</h1><p>ID za DDV SI12345678 Glavna ulica 12</p></article>', url)
        evidence_id = next(iter(h.writer.pages.values()))
        claims = [o for o in h.writer.observations if o['evidence_id'] == evidence_id]
        self.assertEqual(page_classification(h.writer.evidence_rows[evidence_id], claims), 'NEWS_PAGE')
        uncertain = OwnershipHarness(); uncertain.page('<p>ALFA STROJI d.o.o.</p>')
        eid = next(iter(uncertain.writer.pages.values()))
        claims = [o for o in uncertain.writer.observations if o['evidence_id'] == eid]
        self.assertEqual(page_classification(uncertain.writer.evidence_rows[eid], claims), 'UNKNOWN_PAGE')
        self.assertTrue(any(o['observation_type'] == 'LEGAL_NAME' for o in claims))

    def test_article_layout_alone_is_not_news(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<title>Kontakt</title><article><section>ALFA STROJI d.o.o.; '
               'VAT: 12345678</section></article>', url)
        evidence_id = next(iter(h.writer.pages.values()))
        self.assertEqual(page_classification(h.writer.evidence_rows[evidence_id]), 'CONTACT_PAGE')


if __name__ == '__main__':
    unittest.main()
