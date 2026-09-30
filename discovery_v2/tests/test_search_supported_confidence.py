"""Offline regressions for narrowly supported search and presentation-name confidence."""
import unittest
import sqlite3

from discovery.ownership import page_type
from discovery_v2.candidates import eligible, fused_candidates
from discovery_v2.models import Config
from discovery_v2.runner import run
from discovery_v2.tests import test_phase_a as phase_a
from discovery_v2.tests.test_phase_a import response
from discovery_v2.tests.test_phase_b_p1a_ownership import OwnershipHarness


ISKRA = dict(id=132, company_name='ISKRA AMS d.o.o.', tax_number='12345678',
             registration_number='7654321', address='Savska loka 4, 4000 Kranj',
             municipality='4000 Kranj')
ISKRA_URL = 'https://iskra-ams.si/'
GPK = dict(id=8, company_name='GPK d.o.o.', tax_number='12345678',
           registration_number='7654321', address='Britof 108A, 4000 Kranj',
           municipality='4000 Kranj')
GPK_URL = 'https://gpk-gradbenistvo.si/'


def add_result(harness, url, title, body, rank):
    harness.writer.search_result({'url': url, 'title': title, 'body': body},
                                 'LEGAL_COMPANY_CONTACT', 'offline', 'fixture', rank)


def iskra_harness(failure='Access denied/rate limited (403): https://iskra-ams.si/'):
    h = OwnershipHarness(ISKRA)
    add_result(h, ISKRA_URL, 'ISKRA AMS d.o.o.',
               'Iskra AMS d.o.o. - avtomatizacija in merilni sistemi', 1)
    add_result(h, 'https://www.bizi.si/ISKRA-AMS/', 'ISKRA AMS d.o.o.',
               'ISKRA AMS d.o.o., Savska loka 4, 4000 Kranj; spletna stran '
               'https://iskra-ams.si/; info@iskra-ams.si', 2)
    add_result(h, 'https://www.giz-gois.eu/clani/iskra-ams-d-o-o/',
               'ISKRA AMS d.o.o. | Člani združenja',
               'ISKRA AMS d.o.o., Savska loka 4, 4000 Kranj; '
               'SPLETNA STRAN: www.iskra-ams.si', 3)
    h.failures[ISKRA_URL] = [failure]
    return h


class SearchSupportedConfidenceTests(unittest.TestCase):
    def test_iskra_search_convergence_authorizes_high_after_403(self):
        h = iskra_harness()
        decision = h.decide(ISKRA_URL)
        self.assertEqual(decision['status'], 'HIGH')
        self.assertFalse(decision['verified'])
        self.assertEqual(decision['confidence_rule_id'], 'CONF-05_SEARCH_SUPPORTED_DOMAIN')
        self.assertEqual(decision['verification_scope'], ISKRA_URL)
        self.assertEqual(decision['fetch_state']['status'], 'HTTP_FORBIDDEN')
        self.assertGreaterEqual(len(decision['supporting_evidence_ids']), 3)

    def test_403_does_not_authorize_weak_or_conflicting_search_evidence(self):
        cases = ('direct_only', 'directory_only', 'identifier_conflict', 'foreign_address')
        for case in cases:
            with self.subTest(case=case):
                h = OwnershipHarness(ISKRA)
                add_result(h, ISKRA_URL, 'ISKRA AMS d.o.o.', 'ISKRA AMS d.o.o.', 1)
                if case == 'directory_only':
                    add_result(h, 'https://bizi.si/iskra', 'ISKRA AMS d.o.o.',
                               'ISKRA AMS d.o.o. https://iskra-ams.si/', 2)
                elif case == 'identifier_conflict':
                    add_result(h, 'https://bizi.si/iskra', 'ISKRA AMS d.o.o.',
                               'ISKRA AMS d.o.o., Savska loka 4, 4000 Kranj; '
                               'VAT: 87654321; https://iskra-ams.si/', 2)
                elif case == 'foreign_address':
                    add_result(h, 'https://directory.example/iskra', 'ISKRA AMS d.o.o.',
                               'ISKRA AMS d.o.o., Hauptstrasse 9, 10115 Berlin; '
                               'https://iskra-ams.si/', 2)
                h.failures[ISKRA_URL] = ['HTTP 403 or non-HTML: ' + ISKRA_URL]
                self.assertEqual(h.decide(ISKRA_URL)['status'], 'REVIEW')

    def test_dns_failure_never_uses_search_supported_path(self):
        h = iskra_harness('DNS failure: iskra-ams.si')
        self.assertEqual(h.decide(ISKRA_URL)['status'], 'REVIEW')

    def test_public_email_provider_does_not_support_candidate_domain(self):
        h = OwnershipHarness(ISKRA)
        add_result(h, ISKRA_URL, 'ISKRA AMS d.o.o.', 'ISKRA AMS d.o.o.', 1)
        add_result(h, 'https://bizi.si/iskra', 'ISKRA AMS d.o.o.',
                   'ISKRA AMS d.o.o., Savska loka 4, 4000 Kranj; info@gmail.com', 2)
        h.failures[ISKRA_URL] = ['Access denied (403): ' + ISKRA_URL]
        self.assertEqual(h.decide(ISKRA_URL)['status'], 'REVIEW')


class ExpandedLegalNameTests(unittest.TestCase):
    def supported_gpk(self):
        h = OwnershipHarness(GPK)
        add_result(h, GPK_URL, 'GPK, gradbeništvo d.o.o.',
                   'GPK d.o.o. - gradbeništvo', 1)
        add_result(h, 'https://bizi.si/gpk', 'GPK d.o.o.',
                   'GPK d.o.o., Britof 108A, 4000 Kranj; '
                   'https://gpk-gradbenistvo.si/; info@gpk-gradbenistvo.si', 2)
        h.page('<html><title>GPK, gradbeništvo d.o.o.</title>'
               '<main><h1>GPK, gradbeništvo d.o.o.</h1></main></html>',
               GPK_URL)
        return h

    def test_supported_expanded_name_is_alias_and_authorizes_high(self):
        h = self.supported_gpk()
        aliases = [o for o in h.writer.observations if o['observation_type'] == 'ALIAS']
        self.assertTrue(any(o['value']['qualifiers'].get('alias_kind') ==
                            'EXPANDED_LEGAL_NAME' for o in aliases))
        decision = h.decide(GPK_URL)
        self.assertEqual(decision['status'], 'HIGH')
        self.assertFalse(decision['verified'])
        self.assertEqual(decision['confidence_rule_id'],
                         'CONF-06_SUPPORTED_EXPANDED_LEGAL_NAME')

    def test_expanded_alias_alone_or_with_wrong_identifier_stays_review(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                h = OwnershipHarness(GPK)
                html = '<title>GPK, gradbeništvo d.o.o.</title><h1>GPK, gradbeništvo d.o.o.'
                if conflict:
                    html += '; VAT: 87654321'
                h.page(html + '</h1>', GPK_URL)
                self.assertEqual(h.decide(GPK_URL)['status'], 'REVIEW')

    def test_supported_expanded_name_cannot_override_identifier_or_entity_conflict(self):
        for conflicting in ('VAT: 87654321', 'Website operated by BETA SISTEMI d.o.o.'):
            with self.subTest(conflicting=conflicting):
                h = OwnershipHarness(GPK)
                add_result(h, GPK_URL, 'GPK, gradbeništvo d.o.o.',
                           'GPK d.o.o. - gradbeništvo', 1)
                add_result(h, 'https://bizi.si/gpk', 'GPK d.o.o.',
                           'GPK d.o.o., Britof 108A, 4000 Kranj; '
                           'https://gpk-gradbenistvo.si/', 2)
                h.page('<title>GPK, gradbeništvo d.o.o.</title><h1>'
                       'GPK, gradbeništvo d.o.o.; ' + conflicting + '</h1>', GPK_URL)
                self.assertEqual(h.decide(GPK_URL)['status'], 'REVIEW')

    def test_expanded_alias_on_member_profile_is_ineligible(self):
        url = 'https://association.example/clani/gpk/'
        self.assertFalse(eligible(GPK, url, 'GPK, gradbeništvo d.o.o.',
                                  'Člani združenja'))


class AssociationProfileTests(unittest.TestCase):
    def test_member_profiles_are_not_official_candidates(self):
        self.assertEqual(page_type('https://association.example/clani/alfa/'), 'PROFILE')
        self.assertEqual(page_type('https://association.example/clan/alfa/'), 'PROFILE')
        self.assertEqual(page_type('https://association.example/members/alfa/',
                                   'Member directory', 'Industry association'), 'PROFILE')
        self.assertFalse(eligible(ISKRA,
            'https://www.giz-gois.eu/clani/iskra-ams-d-o-o/',
            'ISKRA AMS d.o.o.', 'Člani združenja'))

    def test_normal_first_party_members_path_is_not_overclassified(self):
        self.assertEqual(page_type('https://alfa.si/members/team/', 'Our team',
                                   'Meet the members of our engineering team'), 'COMPANY_PAGE')

    def test_member_snippet_can_support_external_candidate(self):
        h = iskra_harness()
        bundles = fused_candidates(ISKRA, h.writer.observations, h.writer.evidence_rows)
        target = next(bundle for bundle in bundles if bundle['domain'] == 'iskra-ams.si')
        self.assertIn('SNIPPET_URL', target['evidence']['evidence_types'])
        association = next(bundle for bundle in bundles if bundle['domain'] == 'giz-gois.eu')
        self.assertFalse(eligible(ISKRA, association['observation']['normalized_value'],
                                  association['observation']['value']['title'],
                                  association['observation']['value']['body']))


class RealRunnerPipelineTests(unittest.TestCase):
    setUp = phase_a.PhaseATests.setUp
    create = phase_a.PhaseATests.create

    def set_company(self, company):
        conn = sqlite3.connect(self.source)
        conn.execute('UPDATE companies_lite SET company_name=?, tax_number=?, '
                     'registration_number=?, address=?, municipality=? WHERE id=1',
                     (company['company_name'], company['tax_number'],
                      company['registration_number'], company['address'],
                      company['municipality']))
        conn.commit()
        conn.close()

    def test_iskra_403_is_selected_high_through_runner(self):
        self.set_company(ISKRA)
        results = [
            {'url': ISKRA_URL, 'title': 'ISKRA AMS d.o.o.',
             'body': 'ISKRA AMS d.o.o. avtomatizacija'},
            {'url': 'https://bizi.si/iskra', 'title': 'ISKRA AMS d.o.o.',
             'body': 'ISKRA AMS d.o.o., Savska loka 4, 4000 Kranj; '
                     'https://iskra-ams.si/; info@iskra-ams.si'},
            {'url': 'https://giz-gois.eu/clani/iskra/', 'title': 'ISKRA AMS d.o.o.',
             'body': 'Člani združenja: ISKRA AMS d.o.o., Savska loka 4, '
                     '4000 Kranj; SPLETNA STRAN www.iskra-ams.si'},
        ]

        class ForbiddenFetcher:
            def __init__(self):
                self.requests, self.errors, self.calls = 0, [], []
            def fetch(self, url, allowed_site=None):
                self.requests += 1
                self.calls.append(url)
                self.errors.append('Access denied/rate limited (403): ' + url)
                return None
            def close(self):
                pass

        fetchers = []
        def factory(_config):
            fetcher = ForbiddenFetcher()
            fetchers.append(fetcher)
            return fetcher

        run_id = self.create(config=Config(max_search_queries_per_company=1))
        outcome = run(self.store, run_id, provider=phase_a.FakeSearch(results),
                      fetcher_factory=factory)
        attempts = [dict(row) for row in self.store.conn.execute(
            'SELECT * FROM discovery_attempts')]
        self.assertEqual(outcome['failed'], 0, (outcome, attempts))
        result = self.store.current_results(run_id)[0]
        self.assertEqual(result['website_status'], 'HIGH')
        self.assertEqual(result['official_website'], ISKRA_URL)
        self.assertIn(ISKRA_URL, fetchers[0].calls)
        self.assertFalse(any('giz-gois.eu' in url for url in fetchers[0].calls))

    def test_gpk_expanded_name_is_selected_high_through_runner(self):
        self.set_company(GPK)
        results = [
            {'url': GPK_URL, 'title': 'GPK, gradbeništvo d.o.o.',
             'body': 'GPK d.o.o. - gradbeništvo'},
            {'url': 'https://bizi.si/gpk', 'title': 'GPK d.o.o.',
             'body': 'GPK d.o.o., Britof 108A, 4000 Kranj; '
                     'https://gpk-gradbenistvo.si/; info@gpk-gradbenistvo.si'},
        ]
        html = ('<html><title>GPK, gradbeništvo d.o.o.</title>'
                '<main><h1>GPK, gradbeništvo d.o.o.</h1></main></html>')
        run_id = self.create(config=Config(max_search_queries_per_company=1))
        _, outcome, _, _ = phase_a.PhaseATests.execute(
            self, run_id, search=phase_a.FakeSearch(results),
            pages={GPK_URL: response(html, GPK_URL)})
        result = self.store.current_results(run_id)[0]
        self.assertEqual(outcome['failed'], 0)
        self.assertEqual(result['website_status'], 'HIGH')
        self.assertEqual(result['official_website'], GPK_URL)


if __name__ == '__main__':
    unittest.main()
