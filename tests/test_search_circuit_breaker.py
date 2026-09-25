"""Offline search failures, batch checkpointing, and retry coverage."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database_lite as db
from discovery.runner import run, load_companies
from discovery.search_engine import (
    SearchCircuitBreaker, SearchInfrastructureStopped, discover_urls,
    infrastructure_failure,
)
from discovery.website_discovery import discover
from test_phase2_discovery import COMPANY, FakeFetcher


class SearchBreakerTests(unittest.TestCase):
    def test_fixed_manifest_force_ids_overrides_legacy_skip_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.initialize_database(); db.initialize_discovery_database()
            db.save_company(COMPANY)
            db.save_website({'company_id': 1, 'status': 'VERIFIED', 'rule_version': 'phase2-mvp-2',
                             'email_status': 'DONE', 'final_url': 'https://alfa.si/'})
            # Normal selection preserves the old skip behaviour.
            self.assertEqual(load_companies(ids=[1]), [])
            # A reviewed fixed manifest deliberately re-evaluates each explicit ID once.
            self.assertEqual([x['id'] for x in load_companies(ids=[1, 1], force_ids=True)], [1])
            report = Path(tmp) / 'fixed.json'
            with patch('discovery.runner.Fetcher', side_effect=lambda: FakeFetcher({})), \
                 patch('discovery.runner.discover', return_value={'status':'REVIEW', 'confidence':0,
                                                                  'final_url':'', 'evidence':[], 'errors':[]}):
                result = run(limit=1, ids=[1], retry=False, report_path=report, force_ids=True)
            self.assertEqual([entry['company_id'] for entry in result['companies']], [1])
            # A completed fixed report is idempotent on resume.
            self.assertTrue(run(resume_report=report)['complete'])

    def test_wrapped_transport_errors_only(self):
        for message in ('ConnectError: dns error > no connections available',
                        'connection reset', 'TimeoutException: timed out',
                        'TransportError: connection lost'):
            self.assertTrue(infrastructure_failure(RuntimeError(message)))
        for message in ('No results found.', 'HTTP 429', 'invalid query'):
            self.assertFalse(infrastructure_failure(RuntimeError(message)))
        with self.assertRaises(ValueError): SearchCircuitBreaker(0)

    def test_consecutive_searches_trip_and_stop_queries(self):
        breaker = SearchCircuitBreaker(3)
        with patch('discovery.search_engine.search_ddg', side_effect=ConnectionError('dns error')) as search:
            discover_urls(COMPANY, breaker=breaker)
            with self.assertRaises(SearchInfrastructureStopped):
                discover_urls(COMPANY, breaker=breaker)
            self.assertEqual(search.call_count, 3)

    def test_success_including_empty_results_resets(self):
        breaker = SearchCircuitBreaker(2)
        for success in ([], [{'url': 'https://alfa.si/'}]):
            with patch('discovery.search_engine.search_ddg', side_effect=[ConnectionError(), success]):
                discover_urls(COMPANY, breaker=breaker)
            self.assertEqual(breaker.consecutive_failures, 0)

    def test_no_results_exception_does_not_trigger(self):
        breaker = SearchCircuitBreaker(1)
        with patch('discovery.search_engine.search_ddg', side_effect=RuntimeError('No results found.')):
            discover_urls(COMPANY, breaker=breaker)
        self.assertEqual(breaker.consecutive_failures, 0)

    def test_site_failures_and_not_found_do_not_trigger(self):
        breaker = SearchCircuitBreaker(1)
        fetcher = FakeFetcher({})
        fetcher.errors = ['ConnectionError: DNS website failure']
        with patch('discovery.search_engine.search_ddg', return_value=[]):
            result = discover(COMPANY, fetcher, breaker=breaker)
        self.assertEqual(result['status'], 'NOT_FOUND')
        self.assertEqual(breaker.consecutive_failures, 0)

    def test_batch_preserves_completed_and_resumes_interrupted_and_unattempted(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.initialize_database(); db.initialize_discovery_database()
            for i in range(1, 5):
                db.save_company({**COMPANY, 'tax_number': str(12345677+i), 'registration_number': str(7654320+i)})
            report_path = Path(tmp)/'report.json'
            with patch('discovery.runner.Fetcher', side_effect=lambda: FakeFetcher({})), \
                 patch('discovery.search_engine.search_ddg', side_effect=[[], [], ConnectionError('dns error'), ConnectionError('dns error'), ConnectionError('dns error')]) as search:
                report = run(limit=4, report_path=report_path, search_failure_threshold=3)
            self.assertFalse(report['complete'])
            self.assertTrue(report['circuit_breaker']['triggered'])
            self.assertEqual([e['company_id'] for e in report['companies']], [1, 2])
            self.assertEqual(report['search_calls'], 5)
            self.assertEqual(search.call_count, 5)
            self.assertEqual(json.loads(report_path.read_text()), report)
            self.assertEqual([c['id'] for c in load_companies()], [3, 4])
            with db.get_connection() as conn:
                self.assertEqual([tuple(r) for r in conn.execute('SELECT company_id,status FROM website_discovery ORDER BY company_id')], [(1, 'NOT_FOUND'), (2, 'ERROR')])
            with patch('discovery.runner.Fetcher', side_effect=lambda: FakeFetcher({})), \
                 patch('discovery.search_engine.search_ddg', return_value=[]) as search:
                resumed = run(resume_report=report_path)
            self.assertTrue(resumed['complete'])
            self.assertFalse(resumed['circuit_breaker']['triggered'])
            self.assertEqual([e['company_id'] for e in resumed['companies']], [1, 2, 3, 4])
            self.assertEqual(search.call_count, 4)
            self.assertEqual(resumed['search_calls'], 9)


if __name__ == '__main__': unittest.main()
