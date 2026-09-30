"""Zero-network categorical Search Evidence Resolver v2 tests."""
import unittest
import sqlite3

from discovery_v2.evidence import EvidenceWriter
from discovery_v2.search_resolver import (NO_SUPPORTED_WEBSITE, RESOLVED, REVIEW,
    RULE_DIRECT, RULE_EMAIL, RULE_PUBLISHERS, resolve_search_evidence)
from discovery_v2.tests.test_live_regressions import MemoryStore
from discovery_v2.models import Config
from discovery_v2.tests import test_phase_a as phase_a


COMPANY = {'id': 7, 'company_name': 'ALFA d.o.o.', 'tax_number': '12345678',
           'registration_number': '8765432000', 'address': 'Cesta 1, 4000 Kranj',
           'municipality': 'Kranj'}


class SearchEvidenceResolverTests(unittest.TestCase):
    def writer(self):
        return EvidenceWriter(MemoryStore(), {}, dict(COMPANY))

    def resolve(self, writer, historical=()):
        return resolve_search_evidence(writer.company, writer.observations,
                                       writer.evidence_rows, historical)

    def add(self, writer, publisher, body, rank=1, title='ALFA d.o.o.'):
        writer.search_result({'url': publisher, 'title': title, 'body': body},
                             'ENTITY', 'fixture', 'fixture', rank)

    def test_independent_directory_publishers_resolve_brand_domain(self):
        writer = self.writer()
        self.add(writer, 'https://bizi.si/alfa', 'ALFA d.o.o. Spletna stran: https://brand.si/')
        self.add(writer, 'https://ebonitete.si/alfa', 'ALFA d.o.o. website: https://brand.si/', 2)
        result = self.resolve(writer)
        self.assertEqual((result['status'], result['candidate_domain'], result['resolution_rule']),
                         (RESOLVED, 'brand.si', RULE_PUBLISHERS))
        self.assertEqual(result['independent_publishers'], ['bizi', 'ebonitete'])

    def test_direct_first_party_identity_resolves(self):
        writer = self.writer()
        self.add(writer, 'https://alfa.si/', 'ALFA d.o.o. Cesta 1, 4000 Kranj')
        result = self.resolve(writer)
        self.assertEqual((result['status'], result['resolution_rule']),
                         (RESOLVED, RULE_DIRECT))

    def test_source_only_hosts_never_resolve_including_preveri(self):
        for url in ('https://bizi.si/alfa', 'https://preveri-podjetje.si/alfa'):
            with self.subTest(url=url):
                writer = self.writer()
                self.add(writer, url, 'ALFA d.o.o. Cesta 1, 4000 Kranj')
                result = self.resolve(writer)
                self.assertEqual(result['status'], NO_SUPPORTED_WEBSITE)
                self.assertIsNone(result['candidate_domain'])

    def test_explicit_website_plus_independent_email_domain_resolves(self):
        writer = self.writer()
        self.add(writer, 'https://bizi.si/alfa', 'ALFA d.o.o. website: https://alfa.si/')
        self.add(writer, 'https://companywall.eu/alfa', 'ALFA d.o.o. info@alfa.si', 2)
        result = self.resolve(writer)
        self.assertEqual((result['status'], result['resolution_rule']),
                         (RESOLVED, RULE_EMAIL))

    def test_public_email_ignored_and_different_company_email_does_not_block(self):
        writer = self.writer()
        self.add(writer, 'https://bizi.si/alfa',
                 'ALFA d.o.o. website: https://alfa.si/ info@gmail.com')
        self.add(writer, 'https://ebonitete.si/alfa',
                 'ALFA d.o.o. website: https://alfa.si/ info@other-brand.si', 2)
        result = self.resolve(writer)
        self.assertEqual((result['status'], result['candidate_domain']),
                         (RESOLVED, 'alfa.si'))
        domains = {c['candidate_domain'] for c in result['candidates']}
        self.assertNotIn('gmail.com', domains)

    def test_repetition_from_one_publisher_counts_once(self):
        writer = self.writer()
        for rank in range(1, 4):
            self.add(writer, f'https://bizi.si/alfa/{rank}',
                     'ALFA d.o.o. website: https://alfa.si/', rank)
        result = self.resolve(writer)
        self.assertEqual(result['status'], REVIEW)
        alfa = next(c for c in result['candidates'] if c['candidate_domain'] == 'alfa.si')
        self.assertEqual(alfa['independent_publishers'], ['bizi'])

    def test_historical_is_corroboration_only_and_wrong_history_loses(self):
        writer = self.writer()
        self.add(writer, 'https://alfa.si/', 'ALFA d.o.o. contact')
        result = self.resolve(writer, [{'website': 'https://wrong.si/',
                                        'status': 'VERIFIED'}])
        self.assertEqual((result['status'], result['candidate_domain']),
                         (RESOLVED, 'alfa.si'))
        self.assertIn('wrong.si', result['alternative_candidate_domains'])
        empty = self.resolve(self.writer(), [{'website': 'https://old.si/',
                                              'status': 'VERIFIED'}])
        self.assertEqual(empty['status'], NO_SUPPORTED_WEBSITE)

    def test_failed_attempt_rows_are_not_filtered(self):
        writer = self.writer()
        self.add(writer, 'https://bizi.si/alfa', 'ALFA d.o.o. website: https://alfa.si/')
        self.add(writer, 'https://ebonitete.si/alfa', 'ALFA d.o.o. website: https://alfa.si/', 2)
        writer.fetch_failure('https://alfa.si/', ['timeout'])
        self.assertEqual(self.resolve(writer)['status'], RESOLVED)

    def test_exact_identifier_conflict_overrides_convergence(self):
        writer = self.writer()
        self.add(writer, 'https://bizi.si/alfa',
                 'ALFA d.o.o. VAT 99999999 website: https://alfa.si/')
        self.add(writer, 'https://ebonitete.si/alfa',
                 'ALFA d.o.o. website: https://alfa.si/', 2)
        result = self.resolve(writer)
        self.assertEqual(result['status'], REVIEW)
        candidate = next(c for c in result['candidates']
                         if c['candidate_domain'] == 'alfa.si')
        self.assertIn('EXACT_IDENTIFIER_CONFLICT', candidate['conflicts'])

    def test_competing_authorized_domains_remain_review(self):
        writer = self.writer()
        self.add(writer, 'https://alfa.si/', 'ALFA d.o.o. contact')
        self.add(writer, 'https://alfa.com/', 'ALFA d.o.o. contact', 2)
        result = self.resolve(writer)
        self.assertEqual(result['status'], REVIEW)
        self.assertEqual(result['alternative_candidate_domains'], ['alfa.com', 'alfa.si'])

    def test_no_supported_external_domain_and_order_independence(self):
        rows = [
            ('https://bizi.si/alfa', 'ALFA d.o.o. website: https://alfa.si/'),
            ('https://ebonitete.si/alfa', 'ALFA d.o.o. website: https://alfa.si/'),
        ]
        outcomes = []
        for ordered in (rows, list(reversed(rows))):
            writer = self.writer()
            for rank, (url, body) in enumerate(ordered, 1):
                self.add(writer, url, body, rank)
            result = self.resolve(writer)
            outcomes.append((result['status'], result['candidate_domain'],
                             result['resolution_rule'], result['independent_publishers']))
        self.assertEqual(outcomes[0], outcomes[1])


class SearchResolverRunnerTests(unittest.TestCase):
    setUp = phase_a.PhaseATests.setUp
    create = phase_a.PhaseATests.create
    execute = phase_a.PhaseATests.execute

    def test_valid_resolution_survives_optional_fetch_failure_and_persists(self):
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE companies_lite SET company_name='ALFA d.o.o.', "
                     "address='Cesta 1, 4000 Kranj', municipality='Kranj' WHERE id=1")
        conn.commit()
        conn.close()
        results = [
            {'url': 'https://bizi.si/alfa', 'title': 'ALFA d.o.o.',
             'body': 'ALFA d.o.o. website: https://alfa.si/'},
            {'url': 'https://ebonitete.si/alfa', 'title': 'ALFA d.o.o.',
             'body': 'ALFA d.o.o. website: https://alfa.si/'},
        ]
        run_id = self.create(config=Config(max_search_queries_per_company=1))
        _, outcome, _, _ = self.execute(run_id, search=phase_a.FakeSearch(results),
                                        pages={})
        self.assertEqual(outcome['failed'], 0)
        stored = self.store.current_results(run_id)[0]
        self.assertEqual((stored['website_status'], stored['official_website']),
                         ('HIGH', 'https://alfa.si/'))


if __name__ == '__main__':
    unittest.main()
