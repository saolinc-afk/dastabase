"""Offline monitor regressions: temporary databases and fake Linux processes."""
import json
import os
import time
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor.app import create_app
from monitor.metrics import (ROOT, active_jobs, database_metrics, log_progress,
                             parse_report, recent_jobs, runner_args, safe_file, service_metrics)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.logs = self.root/'logs'
        self.logs.mkdir()
        self.db = self.root/'test.db'

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self):
        conn = sqlite3.connect(self.db)
        conn.executescript('''CREATE TABLE companies_lite(id INTEGER PRIMARY KEY, company_name TEXT);
        CREATE TABLE website_discovery(id INTEGER PRIMARY KEY, company_id INTEGER, status TEXT,
            website TEXT, rule_version TEXT, evidence_json TEXT, ownership_json TEXT);
        CREATE TABLE email_discovery(company_id INTEGER, email TEXT, page_url TEXT,
            page_title TEXT, evidence_json TEXT);
        INSERT INTO companies_lite VALUES (1, 'One'), (2, 'Two'), (3, 'Three');
        INSERT INTO website_discovery VALUES
          (1, 1, 'ERROR', '', '', '[]', '{}'),
          (2, 1, 'VERIFIED', 'https://one.si', 'phase2-ownership-1', '[]', '{}'),
          (3, 2, 'REVIEW', '', '', '[]', '{}');
        INSERT INTO email_discovery VALUES
          (1, 'hello@one.si', '', '', '{}'), (2, 'no@two.si', '', '', '{}');''')
        conn.commit()
        conn.close()

    def test_latest_counts_and_read_only(self):
        self.fixture()
        before = self.db.read_bytes()
        with patch('monitor.metrics.email_attribution', return_value={'attributable': True}) as attribution:
            result = database_metrics(self.db)
        self.assertEqual(result['total'], 3)
        self.assertEqual((result['processed'], result['remaining'], result['percent']), (2, 1, 66.7))
        self.assertEqual(result['statuses']['ERROR'], 0)
        self.assertEqual(result['statuses']['VERIFIED'], 1)
        self.assertEqual((result['emails'], result['email_companies']), (1, 1))
        self.assertEqual(attribution.call_count, 1)
        self.assertIsNone(result['phase1_complete'])
        self.assertEqual(before, self.db.read_bytes())

    def test_missing_database_is_not_created(self):
        self.assertFalse(database_metrics(self.db)['available'])
        self.assertFalse(self.db.exists())

    def test_empty_old_schema_and_malformed_provenance(self):
        self.fixture()
        with patch('monitor.metrics.email_attribution', side_effect=TypeError):
            result = database_metrics(self.db)
        self.assertEqual(result['emails'], 0)
        self.assertIn('Malformed', result['email_note'])
        conn = sqlite3.connect(self.db)
        conn.execute('DROP TABLE email_discovery')
        conn.commit(); conn.close()
        result = database_metrics(self.db)
        self.assertEqual(result['processed'], 2)
        self.assertIsNone(result['emails'])

    def test_real_attribution_rejects_missing_proof(self):
        self.fixture()
        self.assertEqual(database_metrics(self.db)['emails'], 0)

    def test_report_states_and_malformed(self):
        report = {'selected_ids': [1, 2], 'companies': [{'company_id': 1}], 'complete': False}
        self.assertEqual(parse_report(report)['status'], 'incomplete')
        for extra, state in [({'complete': True}, 'complete'), ({'interrupted': True}, 'interrupted'),
                             ({'circuit_breaker': {'triggered': True}}, 'circuit-breaker')]:
            self.assertEqual(parse_report({**report, **extra})['status'], state)
        for value in ([], {}, {'selected_ids': 'bad', 'companies': []},
                      {'selected_ids': [1], 'companies': [None]}):
            self.assertIsNone(parse_report(value))

    def test_whitelist_does_not_expose_secrets(self):
        job = runner_args(['python3.13', '-m', 'discovery.runner', '--limit=20', '--report', 'logs/a.json', '--token', 'SECRET'])
        self.assertEqual(job, {'module': 'discovery.runner', 'limit': 20, 'report': 'logs/a.json'})
        self.assertIsNone(runner_args(['echo', 'python', '-m', 'discovery.runner']))
        self.assertIsNone(runner_args(['python', '-m', 'other.runner']))

    def test_proc_report_and_actual_stdout(self):
        proc = self.root/'proc'; proc.mkdir()
        (proc/'uptime').write_text('1000 0')
        pid = proc/'123'; pid.mkdir(); (pid/'fd').mkdir()
        (pid/'cwd').symlink_to(ROOT, target_is_directory=True)
        report = self.logs/'batch.json'
        report.write_text(json.dumps({'selected_ids': [1, 2], 'companies': [{'company_id': 1}], 'complete': False}))
        (pid/'cmdline').write_bytes(('python\0-m\0discovery.runner\0--report\0'+str(report)+'\0--secret\0SECRET\0').encode())
        (pid/'stat').write_text('123 (python worker) '+' '.join(['S']+['0']*18+['100']))
        out = self.logs/'run.out'; out.write_text('[2/2] ID 2 Test company\n')
        (pid/'fd'/'1').symlink_to(out)
        result = active_jobs(self.logs, proc)
        self.assertEqual(len(result['jobs']), 1)
        job = result['jobs'][0]
        self.assertEqual((job['processed'], job['selected']), (1, 2))
        self.assertEqual(job['current_company']['id'], 2)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(recent_jobs(self.logs, result)['jobs'][0]['status'], 'running')
        out.write_text('[2/2] ID 2 Test company\n  VERIFIED x confidence=90; emails=1 (FOUND); HTTP=1\n')
        self.assertNotIn('current_company', log_progress(out))
        out.write_text('[2/2] ID 2 Test company\n')
        old = time.time()-2000
        os.utime(out, (old, old))
        self.assertIsNone(active_jobs(self.logs, proc)['jobs'][0]['current_company'])
        out.write_text('[2/2] ID 2 Test company\n')
        report.write_text(json.dumps({'selected_ids': [1, 2], 'companies': [{'company_id': 1}, {'company_id': 2}]}))
        self.assertIsNone(active_jobs(self.logs, proc)['jobs'][0]['current_company'])

    def test_reports_are_confined_and_malformed_skipped(self):
        outside = self.root/'outside.json'; outside.write_text('{}')
        (self.logs/'link.json').symlink_to(outside)
        self.assertIsNone(safe_file(self.logs/'link.json', self.logs))
        (self.logs/'broken.json').write_text('{')
        result = recent_jobs(self.logs, {'jobs': []})
        self.assertEqual(result['jobs'], [])
        self.assertEqual(result['skipped'], 1)

    def test_services_read_only_and_unavailable(self):
        def fake(args):
            if args[0] == 'docker':
                return json.dumps({'Names': 'nocodb', 'Image': 'nocodb/nocodb', 'State': 'running', 'Status': 'Up (unhealthy)'})
            return None
        with patch('monitor.metrics.command', side_effect=fake):
            services = service_metrics()
        self.assertEqual(services[2]['status'], 'unhealthy')
        self.assertEqual(services[0]['status'], 'unknown')

    def test_routes_cache_and_no_mutations(self):
        self.fixture()
        app = create_app({'TESTING': True, 'DB_PATH': self.db, 'LOGS_PATH': self.logs})
        with patch('monitor.app.service_metrics', return_value=[]), patch('monitor.app.active_jobs', return_value={'available': False, 'jobs': []}):
            client = app.test_client()
            self.assertEqual(client.get('/').status_code, 200)
            response = client.get('/api/status')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['database']['total'], 3)
            self.assertEqual(response.json['timestamp'], client.get('/api/status').json['timestamp'])
            self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
            self.assertEqual(client.post('/api/status').status_code, 405)
            self.assertEqual(client.get('/api/status?db=/etc/passwd').json['database']['total'], 3)


class LiveMonitorTests(unittest.TestCase):
    setUp = MonitorTests.setUp
    tearDown = MonitorTests.tearDown
    fixture = MonitorTests.fixture

    def test_batch_progress_and_activity_projection(self):
        report = {'selected_ids': [1, 2, 3], 'rule_version': 'phase2-ownership-1',
                  'companies': [{'company_id': 1, 'company_name': 'One',
                    'website': {'status': 'VERIFIED', 'final_url': 'https://user:SECRET@one.si/contact?token=SECRET'},
                    'email_result': {'emails': [{'email': 'a@one.si', 'evidence': {'attribution': {'attributable': True}}}]}}]}
        result = parse_report(report)
        self.assertEqual((result['processed'], result['selected'], result['remaining'], result['percent']), (1, 3, 2, 33.3))
        event = result['last_completed']
        self.assertEqual(event, {'company_id': 1, 'company_name': 'One', 'status': 'VERIFIED', 'domain': 'one.si', 'usable_emails': 1})
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('time', event)
        report['companies'][0]['email_result']['emails'][0]['evidence'] = {}
        self.assertNotIn('usable_emails', parse_report(report)['last_completed'])
        report['companies'][0]['website']['status'] = 'REVIEW'
        self.assertNotIn('domain', parse_report(report)['last_completed'])
        report['selected_ids'] = [1, 1]
        self.assertIsNone(parse_report(report))

    def test_workload_retries_overlaps_and_no_worker(self):
        from monitor.state import workload
        database = {'remaining': 10}
        companies = {1: {'processed': True}, 2: {'processed': False}, 3: {'processed': False}}
        jobs = [{'selected_ids': [1, 2], 'completed_ids': [], 'remaining': 2},
                {'selected_ids': [2, 3], 'completed_ids': [], 'remaining': 2}]
        result = workload(database, {'available': True, 'jobs': jobs}, companies)
        self.assertEqual(result['batch_remaining'], 4)
        self.assertEqual(result['unprocessed_in_active_batches'], 2)
        self.assertEqual(result['remaining_after_batch'], 8)
        self.assertFalse(result['persistent_queue'])
        idle = workload(database, {'available': True, 'jobs': []}, {})
        self.assertEqual((idle['state'], idle['remaining_after_batch']), ('idle', 10))
        hidden = workload(database, {'available': False, 'jobs': []}, {})
        self.assertEqual(hidden['state'], 'unknown')
        self.assertIsNone(hidden['remaining_after_batch'])
        for active in ({'available': True, 'jobs': [{}]}, {'available': True, 'jobs': jobs, 'inaccessible_processes': 1}):
            self.assertIsNone(workload(database, active, companies)['remaining_after_batch'])

    def test_lookup_and_activity_name_fallback(self):
        from monitor.state import company_lookup, operation_details, public_jobs
        self.fixture()
        self.assertEqual(company_lookup(self.db, [1, 3]), {1: {'name': 'One', 'processed': True}, 3: {'name': 'Three', 'processed': False}})
        summary = parse_report({'selected_ids': [1, 3], 'companies': [{'company_id': 1}]})
        job = {**summary, 'current_company': {'id': 3, 'name': ''}, 'report': 'a.json'}
        active = {'available': True, 'jobs': [job]}
        work, activity = operation_details({'remaining': 1}, active, {'jobs': []}, self.db)
        self.assertEqual(job['current_company']['name'], 'Three')
        self.assertEqual(job['last_completed']['company_name'], 'One')
        self.assertEqual(activity['entries'][0]['company_name'], 'One')
        self.assertEqual(work['remaining_after_batch'], 0)
        public, _ = public_jobs(active, {'jobs': []})
        self.assertNotIn('selected_ids', public['jobs'][0])
        idle, activity = operation_details({'remaining': 1}, {'available': True, 'jobs': []},
                                           {'jobs': [{**summary, 'name': 'a.json'}]}, self.db)
        self.assertEqual(activity['source'], 'recent')
        self.assertEqual(activity['entries'][0]['company_name'], 'One')

    def test_activity_last_ten_and_malformed_subfields(self):
        entries = [{'company_id': i, 'website': [], 'email_result': 'bad'} for i in range(20)]
        report = parse_report({'selected_ids': list(range(20)), 'companies': entries})
        self.assertEqual([e['company_id'] for e in report['activity']], list(range(19, 9, -1)))
        self.assertIsNone(report['circuit_breaker'])
        self.assertEqual(parse_report({'selected_ids': [], 'companies': []})['percent'], 0)
        self.assertIsNone(parse_report({'selected_ids': [1], 'companies': [{'company_id': 2}]}))

    def test_append_log_resets_old_breaker(self):
        path = self.logs/'a.out'
        path.write_text('Search circuit breaker stopped batch: old\n2 companies selected (hard limit 200). Report: logs/new.json\n[1/2] ID 3\n')
        progress = log_progress(path)
        self.assertFalse(progress['circuit_breaker'])
        self.assertEqual(progress['current_company'], {'id': 3, 'name': ''})

    def test_report_cache_only_reparses_changed_files(self):
        from monitor.state import ReportCache
        path = self.logs/'a.json'
        path.write_text(json.dumps({'selected_ids': [1], 'companies': []}))
        cache = ReportCache()
        with patch('monitor.state.bounded_read', wraps=__import__('monitor.metrics', fromlist=['bounded_read']).bounded_read) as read:
            first = cache.read(path)
            first['selected_ids'].append(9)
            self.assertEqual(cache.read(path)['selected_ids'], [1])
            self.assertEqual(read.call_count, 1)
            path.write_text(json.dumps({'selected_ids': [1], 'companies': [{'company_id': 1}]}))
            self.assertEqual(cache.read(path)['processed'], 1)
            self.assertEqual(read.call_count, 2)
            path.write_text('{')
            with self.assertRaises(ValueError):
                cache.read(path)
            with self.assertRaises(ValueError):
                cache.read(path)
            self.assertEqual(read.call_count, 3)

    def test_database_cache_wal_and_attribution_reuse(self):
        from monitor.state import DatabaseCache
        self.fixture()
        cache = DatabaseCache()
        with patch('monitor.metrics.email_attribution', return_value={'attributable': True}) as attribution:
            first = cache.collect(self.db)
            self.assertEqual(first['emails'], 1)
            self.assertIs(cache.collect(self.db), first)
            # Simulate WAL changes; unchanged per-company inputs reuse attribution.
            wal = Path(str(self.db)+'-wal'); wal.write_bytes(b'changed')
            self.assertEqual(cache.collect(self.db)['emails'], 1)
            self.assertEqual(attribution.call_count, 1)
            wal.unlink()
            conn = sqlite3.connect(self.db)
            conn.execute("UPDATE email_discovery SET page_title='new' WHERE company_id=1")
            conn.commit(); conn.close()
            cache.collect(self.db)
            self.assertEqual(attribution.call_count, 2)

    def test_separate_cache_intervals_and_live_never_reads_database(self):
        from monitor.state import SnapshotCache
        clock = [10.0]
        with patch('monitor.state.time.monotonic', side_effect=lambda: clock[0]):
            cache = SnapshotCache(2.5)
            calls = []
            def collect():
                calls.append(1)
                return len(calls)
            self.assertEqual(cache.get(collect), 1)
            clock[0] += 2
            self.assertEqual(cache.get(collect), 1)
            clock[0] += 0.5
            self.assertEqual(cache.get(collect), 2)
        app = create_app({'TESTING': True, 'DB_PATH': self.db, 'LOGS_PATH': self.logs})
        with patch('monitor.state.database_metrics') as db, patch('monitor.app.recent_jobs') as reports, patch('monitor.app.service_metrics') as services, patch('monitor.app.active_jobs', return_value={'available': True, 'jobs': []}) as processes:
            client = app.test_client()
            self.assertEqual(client.get('/api/live').status_code, 200)
            client.get('/api/live')
            processes.assert_called_once_with(self.logs, details=False)
            db.assert_not_called(); reports.assert_not_called(); services.assert_not_called()
            self.assertEqual(client.post('/api/live').status_code, 405)

    def test_multiple_clients_share_one_collection(self):
        from concurrent.futures import ThreadPoolExecutor
        from monitor.state import SnapshotCache
        from threading import Barrier
        cache = SnapshotCache(10)
        barrier = Barrier(4)
        calls = []
        def client():
            barrier.wait(timeout=2)
            return cache.get(lambda: calls.append(1) or {'ok': True})
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: client(), range(4)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(r == {'ok': True} for r in results))

    def test_service_checks_not_repeated_with_status_refresh(self):
        self.fixture()
        app = create_app({'TESTING': True, 'DB_PATH': self.db, 'LOGS_PATH': self.logs, 'CACHE_SECONDS': 0})
        with patch('monitor.app.service_metrics', return_value=[]) as services, patch('monitor.app.active_jobs', return_value={'available': True, 'jobs': []}):
            client = app.test_client()
            for _ in range(3):
                self.assertEqual(client.get('/api/status').status_code, 200)
            services.assert_called_once()
