"""Deterministic production-confidence rules below strict P1A verification."""
import unittest

from discovery_v2.contacts import resolve_contacts, select_default
from discovery_v2.tests.test_phase_b_p1a_ownership import OwnershipHarness, DOMAIN


LEGAL = 'ALFA STROJI d.o.o.'
ADDRESS = 'Glavna ulica 12, 1000 Ljubljana'


class ConfidenceTests(unittest.TestCase):
    def test_verified_remains_strict_and_precedes_confidence(self):
        h = OwnershipHarness(); h.page(
            '<section>Website operated by ALFA STROJI d.o.o.; VAT: 12345678</section>')
        decision = h.decide()
        self.assertEqual(decision['status'], 'VERIFIED')
        self.assertTrue(decision['verified'])
        self.assertIsNone(decision['confidence_rule_id'])

    def test_strong_converging_evidence_is_high(self):
        h = OwnershipHarness()
        h.search('directory-one.si', LEGAL)
        h.page('<section>ALFA STROJI d.o.o. Contact '
               '<a href="mailto:info@alfa-stroji.si">info@alfa-stroji.si</a></section>')
        decision = h.decide()
        self.assertEqual(decision['status'], 'HIGH')
        self.assertEqual(decision['confidence_rule_id'], 'CONF-02_LEGAL_CONTACT_CONVERGENCE')
        self.assertFalse(decision['verified'])
        self.assertTrue(decision['usable'])

    def test_weaker_coherent_legal_contact_page_is_medium(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<section>ALFA STROJI d.o.o.</section>', url)
        decision = h.decide(url)
        self.assertEqual(decision['status'], 'MEDIUM')
        self.assertEqual(decision['confidence_rule_id'], 'CONF-04_LEGAL_PAGE_COHERENCE')

    def test_weak_or_search_only_evidence_stays_review(self):
        weak = OwnershipHarness(); weak.page('<section>ALFA STROJI d.o.o.</section>')
        self.assertEqual(weak.decide()['status'], 'REVIEW')
        search_only = OwnershipHarness(); search_only.search('directory-one.si', LEGAL)
        self.assertEqual(search_only.decide()['status'], 'REVIEW')

    def test_directory_conflicts_and_group_context_are_hard_vetoes(self):
        directory = OwnershipHarness(); url = 'https://bizi.si/kontakt'
        directory.page('<section>' + LEGAL + '; ' + ADDRESS + '</section>', url)
        self.assertEqual(directory.decide(url)['status'], 'REVIEW')
        catalog = OwnershipHarness(); catalog_url = 'https://chamber.example/katalog_clanov/alfa'
        catalog.search('directory-one.si', LEGAL, url=catalog_url)
        catalog.page('<section>' + LEGAL + ' Contact '
                     '<a href="mailto:info@chamber.example">info@chamber.example</a></section>',
                     catalog_url)
        self.assertEqual(catalog.decide(catalog_url)['status'], 'REVIEW')

        conflict = OwnershipHarness(); url = DOMAIN + 'kontakt'
        conflict.page('<section>' + LEGAL + '; VAT: 87654321</section>', url)
        self.assertEqual(conflict.decide(url)['status'], 'REVIEW')
        self.assertIn('EXACT_IDENTIFIER_CONFLICT', conflict.decide(url)['blockers'])

        group = OwnershipHarness()
        group.page('<section>' + LEGAL + '; ' + ADDRESS + '; member of group</section>', url)
        self.assertEqual(group.decide(url)['status'], 'REVIEW')

    def test_directory_profile_with_perfect_identity_cannot_be_official(self):
        h = OwnershipHarness(); url = 'https://directory.example/podjetja/alfa-stroji/'
        h.search('directory.example', LEGAL + '; ' + ADDRESS,
                 url=url)
        h.page('<title>ALFA STROJI company profile</title><section>' + LEGAL + '; '
               + ADDRESS + '; VAT: 12345678; '
               '<a href="mailto:info@alfa-stroji.si">info@alfa-stroji.si</a>'
               '</section>', url)
        decision = h.decide(url)
        self.assertEqual(decision['status'], 'REVIEW')
        self.assertIn('THIRD_PARTY_PAGE', decision['blockers'])

    def test_company_database_perfect_identity_cannot_be_official(self):
        h = OwnershipHarness(); url = 'https://companywall.si/podjetje/alfa'
        h.page('<section>Website operated by ALFA STROJI d.o.o.; VAT: 12345678; '
               + ADDRESS + '</section>', url)
        decision = h.decide(url)
        self.assertEqual(decision['status'], 'REVIEW')
        self.assertIn('DISALLOWED_PUBLISHER_DOMAIN', decision['blockers'])

    def test_third_party_corroboration_still_supports_first_party(self):
        h = OwnershipHarness(); url = DOMAIN
        h.search('bizi.si', LEGAL + '; ' + ADDRESS, url=url)
        h.page('<section>Website operated by ALFA STROJI d.o.o.</section>', url)
        decision = h.decide(url)
        self.assertEqual(decision['status'], 'VERIFIED')
        self.assertEqual(decision['rule_id'], 'OWN-03_TRUSTED_LINK_PLUS_SITE_IDENTITY')

    def test_first_party_confidence_is_unchanged_by_selection_invariant(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<section>' + LEGAL + '; ' + ADDRESS + '</section>', url)
        self.assertEqual(h.decide(url)['status'], 'VERIFIED')

    def test_dct_style_podjetja_directory_remains_review(self):
        h = OwnershipHarness(); url = 'https://database.example/podjetja/alfa-stroji/'
        h.page('<title>ALFA STROJI d.o.o. - Company Info</title><section>'
               + LEGAL + '; ' + ADDRESS + '; '
               '<a href="mailto:info@alfa-stroji.si">info@alfa-stroji.si</a>'
               '</section>', url)
        self.assertEqual(h.decide(url)['status'], 'REVIEW')

    def test_foreign_or_related_contact_context_is_a_hard_veto(self):
        h = OwnershipHarness(); url = DOMAIN + 'kontakt'
        h.page('<section>' + LEGAL + '</section><section><h2>Foreign office</h2>'
               '<a href="mailto:office@alfa-stroji.si">office@alfa-stroji.si</a></section>', url)
        decision = h.decide(url)
        self.assertEqual(decision['status'], 'REVIEW')
        self.assertIn('FOREIGN_OR_RELATED_ENTITY_CONTEXT', decision['blockers'])

    def test_repeated_same_publisher_does_not_create_high_confidence(self):
        h = OwnershipHarness(); url = 'https://unrelated.example/kontakt'
        h.search('directory-one.si', LEGAL, url=url, rank=1)
        h.search('directory-one.si', LEGAL, url=url, rank=2)
        h.page('<section>' + LEGAL + '</section>', url)
        self.assertEqual(h.decide(url)['status'], 'MEDIUM')

    def test_high_site_does_not_auto_attribute_unsafe_contact(self):
        h = OwnershipHarness()
        h.page('<section>' + LEGAL + '; ' + ADDRESS + '</section>'
               '<footer>Website by <a href="mailto:dev@agency.example">dev@agency.example</a></footer>')
        decision = h.decide()
        self.assertEqual(decision['status'], 'HIGH')
        owner = {'status': 'HIGH', 'scope': decision['verification_scope']}
        contacts = resolve_contacts(h.company, h.writer.observations, owner, h.responses)
        agency = next(c for c in contacts if c['normalized_value'] == 'dev@agency.example')
        self.assertEqual(agency['attribution_status'], 'REJECTED')
        self.assertIsNone(select_default(contacts, 'EMAIL'))


if __name__ == '__main__':
    unittest.main()
