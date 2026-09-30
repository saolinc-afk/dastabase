"""Offline, real-pipeline-shaped search evidence fusion regressions."""
import sqlite3
import unittest

from discovery_v2.candidates import eligible, fused_candidates
from discovery_v2.evidence import EvidenceWriter
from discovery_v2.models import Config
from discovery_v2.tests.test_live_regressions import MemoryStore
from discovery_v2.tests import test_phase_a as phase_a


class SearchFusionTests(unittest.TestCase):
    setUp = phase_a.PhaseATests.setUp
    create = phase_a.PhaseATests.create
    execute = phase_a.PhaseATests.execute
    rows = phase_a.PhaseATests.rows

    def company(self, name, address):
        conn = sqlite3.connect(self.source)
        conn.execute('UPDATE companies_lite SET company_name=?, address=?, municipality=? '
                     'WHERE id=1', (name, address, '4000 Kranj'))
        conn.commit()
        conn.close()

    def test_iskra_ams_bundle_leads_and_reaches_verification(self):
        self.company('ISKRA AMS d.o.o.', 'Savska loka 4, 4000 Kranj')
        target = 'https://iskra-ams.si/'
        results = [
            {'url': target, 'title': 'Testna oprema',
             'body': 'Iskra AMS d.o.o. je vaš zanesljiv partner. Kontakt · Podjetje.'},
            {'url': 'https://companywall.si/iskra', 'title': 'ISKRA AMS d.o.o.',
             'body': 'Kontakt podjetja je 042375510.'},
            {'url': 'https://bizi.si/iskra', 'title': 'ISKRA AMS d.o.o.',
             'body': 'Savska loka 4, 4000 Kranj https://iskra-ams.si info@iskra-ams.si'},
            {'url': 'https://giz-gois.eu/clan/iskra', 'title': 'ISKRA AMS d.o.o.',
             'body': 'SPLETNA STRAN www.iskra-ams.si. KONTAKT Savska loka 4, 4000 Kranj'},
            {'url': target + 'podjetje/', 'title': 'Podjetje - IskraAMS',
             'body': 'Iskra AMS d.o.o. info@iskra-ams.si'},
        ]
        html = '<section>Website operated by ISKRA AMS d.o.o.; VAT: 12345678</section>'
        run_id = self.create(config=Config(max_search_queries_per_company=1))
        _, outcome, _, fetchers = self.execute(
            run_id, search=phase_a.FakeSearch(results),
            pages={target: phase_a.response(html, target)})
        self.assertEqual(outcome['failed'], 0)
        self.assertEqual(fetchers[0].calls[0], target)
        self.assertEqual(self.store.current_results(run_id)[0]['official_website'], target)
        self.assertFalse(any('bizi.si' in url or 'companywall.si' in url
                             for url in fetchers[0].calls))

    def test_gpk_email_and_direct_result_fuse_to_target(self):
        self.company('GPK d.o.o.', 'Britof 108A, 4000 Kranj')
        target = 'https://www.gpk-gradbenistvo.si/'
        results = [
            {'url': 'https://companywall.si/gpk', 'title': 'GPK d.o.o.',
             'body': 'GPK d.o.o., Britof 108A, 4000 Kranj'},
            {'url': 'https://bizi.si/gpk', 'title': 'GPK d.o.o.',
             'body': 'Britof 108A, 4000 Kranj info@gpk-gradbenistvo.si'},
            {'url': target, 'title': 'GPK, gradbeništvo d.o.o.',
             'body': 'Podjetje GPK d.o.o se ponaša z bogatimi izkušnjami.'},
            {'url': 'https://www.bizi.si/gpk-kontakt', 'title': 'GPK d.o.o.',
             'body': 'info@gpk-gradbenistvo.si'},
        ]
        html = '<section>Website operated by GPK d.o.o.; VAT: 12345678</section>'
        run_id = self.create(config=Config(max_search_queries_per_company=1))
        _, outcome, _, fetchers = self.execute(
            run_id, search=phase_a.FakeSearch(results),
            pages={target: phase_a.response(html, target)})
        self.assertEqual(outcome['failed'], 0)
        self.assertEqual(fetchers[0].calls[0], target)
        self.assertEqual(self.store.current_results(run_id)[0]['official_website'], target)

    def test_public_email_and_directories_never_become_candidates(self):
        writer = EvidenceWriter(MemoryStore(), {}, {
            'id': 1, 'company_name': 'ALFA d.o.o.', 'tax_number': '12345678'})
        writer.search_result({'url': 'https://bizi.si/alfa', 'title': 'ALFA d.o.o.',
            'body': 'ALFA d.o.o. company@gmail.com company@yahoo.co.uk'},
            'LEGAL_COMPANY_CONTACT', 'query', 'fixture', 1)
        domains = {bundle['domain'] for bundle in fused_candidates(
            writer.company, writer.observations, writer.evidence_rows)}
        self.assertNotIn('gmail.com', domains)
        self.assertNotIn('yahoo.co.uk', domains)
        self.assertEqual(domains, {'bizi.si'})
        directory = next(bundle for bundle in fused_candidates(
            writer.company, writer.observations, writer.evidence_rows)
            if bundle['domain'] == 'bizi.si')['observation']
        self.assertFalse(eligible(writer.company, directory['normalized_value'],
                                  directory['value']['title'], directory['value']['body']))
        self.assertFalse(any(bundle['observations'][0]['normalized_value'].startswith(
            'https://gmail.') for bundle in fused_candidates(
                writer.company, writer.observations, writer.evidence_rows)))

    def test_conflict_and_repetition_do_not_beat_strong_first_party(self):
        company = {'id': 1, 'company_name': 'ALFA d.o.o.', 'tax_number': '12345678'}
        writer = EvidenceWriter(MemoryStore(), {}, company)
        for rank in range(1, 11):
            writer.search_result({'url': 'https://directory.example/alfa-' + str(rank),
                'title': 'ALFA d.o.o.',
                'body': 'ALFA d.o.o. VAT: 87654321 Website https://weak-example.si/'},
                'LEGAL_COMPANY_CONTACT', 'query', 'fixture', rank)
        writer.search_result({'url': 'https://alfa.si/', 'title': 'ALFA d.o.o.',
            'body': 'ALFA d.o.o. contact'}, 'LEGAL_COMPANY_CONTACT', 'query', 'fixture', 11)
        bundles = fused_candidates(company, writer.observations, writer.evidence_rows)
        self.assertEqual(bundles[0]['domain'], 'alfa.si')
        weak = next(bundle for bundle in bundles if bundle['domain'] == 'weak-example.si')
        self.assertTrue(weak['evidence']['identifier_conflict'])
        self.assertEqual(weak['evidence']['independent_publishers'],
                         [f'directory.example'])

    def test_default_persists_ten_results_without_expanding_fetch_budget(self):
        self.assertEqual(Config().results_per_query, 10)
        self.assertEqual(Config().max_candidates, 8)
        results = [{'url': f'https://weak-{rank}.example/', 'title': 'Unrelated',
                    'body': 'weak mention', 'position': rank} for rank in range(1, 10)]
        results.append({'url': 'https://alfa.si/', 'title': 'ALFA d.o.o.',
                        'body': 'ALFA d.o.o. contact', 'position': 10})
        html = '<section>Website operated by ALFA d.o.o.; VAT: 12345678</section>'
        run_id = self.create(config=Config(max_search_queries_per_company=1))
        self.execute(run_id, search=phase_a.FakeSearch(results),
                     pages={'https://alfa.si/': phase_a.response(html)})
        search_rows = [row for row in self.rows('discovery_evidence')
                       if row['source_kind'] == 'SEARCH_RESULT']
        self.assertEqual(len(search_rows), 10)


if __name__ == '__main__':
    unittest.main()
