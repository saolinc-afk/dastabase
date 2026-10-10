"""Control Room foundation tests; no network or production databases."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_room.app import create_app
from control_room.fake import FakeEnrichmentAdapter
from control_room.repository import JobRepository
from control_room.worker import Worker


class ControlRoomRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root/'control.sqlite3'
        self.repository = JobRepository(self.db)
        self.repository.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_job_creation_and_persisted_event(self):
        job = self.repository.create_job('Test enrichment', 100)
        self.assertEqual((job['status'], job['selected_company_count'], job['processed_company_count']),
                         ('QUEUED', 100, 0))
        self.assertEqual(self.repository.events(job['job_id'])[0]['event_code'], 'JOB_QUEUED')
        self.assertIn('+00:00', job['created_at'])

    def test_valid_and_invalid_transitions(self):
        job = self.repository.create_job('Transitions', 5)
        claimed = self.repository.claim_oldest('worker-1')
        self.assertEqual(claimed['status'], 'STARTING')
        running = self.repository.transition(job['job_id'], 'RUNNING', worker_id='worker-1')
        self.assertEqual(running['status'], 'RUNNING')
        completed = self.repository.transition(job['job_id'], 'COMPLETED', worker_id='worker-1')
        self.assertEqual(completed['status'], 'COMPLETED')
        with self.assertRaisesRegex(ValueError, 'Invalid job transition'):
            self.repository.transition(job['job_id'], 'RUNNING')

    def test_atomic_claim_and_two_workers_cannot_claim_same_job(self):
        job = self.repository.create_job('Atomic', 10)
        second_job = self.repository.create_job('Still queued', 10)
        barrier_results = []
        def claim(worker):
            repository = JobRepository(self.db)
            barrier_results.append(repository.claim_oldest(worker))
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(claim, ('worker-a', 'worker-b')))
        claimed = [item for item in barrier_results if item]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]['job_id'], job['job_id'])
        self.assertEqual(self.repository.get_job(job['job_id'])['status'], 'STARTING')
        self.assertEqual(self.repository.get_job(second_job['job_id'])['status'], 'QUEUED')

    def test_fake_progress_completion_and_repository_recreation(self):
        job = self.repository.create_job('Persistent', 20)
        worker = Worker(self.repository, FakeEnrichmentAdapter(delay=0, sleeper=lambda _: None),
                        worker_id='worker-test')
        self.assertTrue(worker.run_once())
        recreated = JobRepository(self.db)
        result = recreated.get_job(job['job_id'])
        self.assertEqual((result['status'], result['processed_company_count']), ('COMPLETED', 20))
        self.assertEqual((result['emails_found'], result['websites_found'], result['phones_found']),
                         (10, 15, 6))
        codes = [event['event_code'] for event in recreated.events(job['job_id'])]
        self.assertEqual(codes[:3], ['JOB_QUEUED', 'WORKER_CLAIMED', 'FAKE_STARTED'])
        self.assertEqual(codes[-1], 'ENRICHMENT_COMPLETE')

    def test_failed_fake_job_is_persisted(self):
        job = self.repository.create_job('Fail safely', 20)
        worker = Worker(self.repository,
            FakeEnrichmentAdapter(delay=0, fail_at=5, sleeper=lambda _: None),
            worker_id='worker-fail')
        self.assertTrue(worker.run_once())
        failed = self.repository.get_job(job['job_id'])
        self.assertEqual(failed['status'], 'FAILED')
        self.assertEqual(failed['error_code'], 'FAKE_ADAPTER_ERROR')
        self.assertNotIn('Traceback', failed['error_message'])
        self.assertEqual(self.repository.events(job['job_id'])[-1]['event_code'],
                         'ENRICHMENT_FAILED')

    def test_progress_must_be_running_bounded_and_monotonic(self):
        job = self.repository.create_job('Bounds', 10)
        with self.assertRaisesRegex(ValueError, 'running'):
            self.repository.update_progress(job['job_id'], 1, 0, 0, 0, 'worker')
        self.repository.claim_oldest('worker')
        self.repository.transition(job['job_id'], 'RUNNING', worker_id='worker')
        self.repository.update_progress(job['job_id'], 5, 2, 3, 1, 'worker')
        with self.assertRaisesRegex(ValueError, 'regressing'):
            self.repository.update_progress(job['job_id'], 4, 2, 3, 1, 'worker')

    def test_stale_worker_is_fenced_after_recovery_claim(self):
        job = self.repository.create_job('Fence stale worker', 10)
        self.repository.heartbeat('old-worker')
        self.repository.claim_oldest('old-worker')
        self.repository.transition(job['job_id'], 'RUNNING', worker_id='old-worker')
        old = (datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE control_jobs SET worker_heartbeat_at=? WHERE job_id=?',
                     (old, job['job_id']))
        conn.execute('UPDATE control_workers SET heartbeat_at=? WHERE worker_id=?',
                     (old, 'old-worker'))
        conn.commit(); conn.close()

        recovered = self.repository.claim_oldest('new-worker', stale_seconds=15)
        self.assertEqual((recovered['status'], recovered['worker_id']),
                         ('STARTING', 'new-worker'))
        with self.assertRaisesRegex(ValueError, 'another worker|running job'):
            self.repository.update_progress(job['job_id'], 1, 0, 0, 0, 'old-worker')
        with self.assertRaisesRegex(ValueError, 'another worker'):
            self.repository.transition(job['job_id'], 'RUNNING', worker_id='old-worker')
        self.repository.transition(job['job_id'], 'RUNNING', worker_id='new-worker')
        with self.assertRaisesRegex(ValueError, 'another worker'):
            self.repository.transition(job['job_id'], 'COMPLETED', worker_id='old-worker')
        with self.assertRaisesRegex(ValueError, 'another worker'):
            self.repository.add_artifact(
                job['job_id'], 'DISCOVERY_EXPORT', 'jobs/stale.csv', 1, 'sha',
                'old-worker')
        self.assertEqual(self.repository.artifacts(job['job_id']), [])
        self.repository.update_progress(job['job_id'], 10, 1, 1, 1, 'new-worker')
        result = self.repository.transition(
            job['job_id'], 'COMPLETED', worker_id='new-worker')
        self.assertEqual(result['status'], 'COMPLETED')

    def test_worker_online_stale_and_idle_heartbeat(self):
        self.assertEqual(self.repository.worker_status()['status'], 'OFFLINE')
        worker = Worker(self.repository, FakeEnrichmentAdapter(delay=0), worker_id='idle-worker')
        self.assertFalse(worker.run_once())
        self.assertEqual(self.repository.worker_status()['status'], 'ONLINE')
        old = (datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE control_workers SET heartbeat_at=?', (old,)); conn.commit(); conn.close()
        self.assertEqual(self.repository.worker_status(stale_seconds=15)['status'], 'OFFLINE')


class ControlRoomWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root/'control.sqlite3'
        self.app = create_app({'TESTING': True, 'CONTROL_DB': self.db,
            'CANONICAL_DB': self.root/'missing-canonical.sqlite3',
            'DISCOVERY_V2_PATH': self.root/'missing-discovery'})
        self.client = self.app.test_client()
        self.repository = self.app.extensions['control_repository']

    def tearDown(self):
        self.temp.cleanup()

    def test_enrich_request_only_queues_job(self):
        response = self.client.post('/enrich', data={
            'display_name': 'Browser-safe job', 'company_count': '100'})
        self.assertEqual(response.status_code, 303)
        jobs = self.repository.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]['status'], jobs[0]['processed_company_count']), ('QUEUED', 0))
        self.assertIn('/jobs/', response.headers['Location'])

    def test_invalid_form_is_bounded(self):
        response = self.client.post('/enrich', data={'display_name': '', 'company_count': '5000'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.repository.list_jobs(), [])

    def test_pages_json_progress_and_persistence_after_app_recreation(self):
        job = self.repository.create_job('Persistent UI', 10)
        for path in ('/', '/enrich', '/jobs', f'/jobs/{job["job_id"]}'):
            self.assertEqual(self.client.get(path).status_code, 200)
        unavailable = self.client.get('/data')
        self.assertEqual(unavailable.status_code, 503)
        self.assertIn('Knowledge Repository is unavailable', unavailable.get_data(as_text=True))
        payload = self.client.get(f'/api/jobs/{job["job_id"]}').get_json()
        self.assertEqual(payload['job']['status'], 'QUEUED')
        self.assertEqual(payload['events'][0]['event_code'], 'JOB_QUEUED')
        second = create_app({'TESTING': True, 'CONTROL_DB': self.db,
            'CANONICAL_DB': self.root/'missing-canonical.sqlite3',
            'DISCOVERY_V2_PATH': self.root/'missing-discovery'}).test_client()
        self.assertIn('Persistent UI', second.get('/jobs').get_data(as_text=True))

    def test_identity_navigation_and_security_headers(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('dastabase_', html)
        self.assertIn('CONTROL ROOM', html)
        self.assertIn('SPARROW v0.9.0', html)
        for label in ('OVERVIEW', 'ENRICH', 'JOBS', 'DATA'):
            self.assertIn(label, html)
        response = self.client.get('/api/current-job')
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        self.assertEqual(self.client.post('/api/current-job').status_code, 405)


if __name__ == '__main__':
    unittest.main()
