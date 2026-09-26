"""Offline monitor regressions: temporary databases and fake Linux processes."""
import json
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
