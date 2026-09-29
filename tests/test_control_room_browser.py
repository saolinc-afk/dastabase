"""Local Chromium check for the Control Room foundation UI."""
import tempfile
import unittest
import hashlib
from pathlib import Path

from playwright.sync_api import sync_playwright

from control_room.app import create_app
from control_room.fake import FakeEnrichmentAdapter
from control_room.worker import Worker


class ControlRoomBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.app = create_app({'TESTING': True, 'CONTROL_DB': root/'control.sqlite3',
            'CONTROL_STORAGE_ROOT':root/'storage','CANONICAL_DB': root/'missing.sqlite3',
            'DISCOVERY_V2_PATH': root/'missing-v2'})
        self.client = self.app.test_client()
        self.page = self.browser.new_page(viewport={'width': 1280, 'height': 900})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

        def serve(route):
            path = route.request.url.removeprefix('http://control.test')
            response = self.client.get(path)
            route.fulfill(status=response.status_code, body=response.data,
                          content_type=response.content_type)
        self.page.route('**/*', serve)

    def tearDown(self):
        self.page.close()
        self.temp.cleanup()

    def test_shell_completed_job_and_phone_layout(self):
        repository = self.app.extensions['control_repository']
        job = repository.create_job('Browser persistence', 10)
        Worker(repository, FakeEnrichmentAdapter(delay=0, sleeper=lambda _: None),
               worker_id='browser-worker').run_once()
        self.page.goto(f'http://control.test/jobs/{job["job_id"]}')
        self.page.locator('[data-field="status"]').filter(has_text='COMPLETED').wait_for()
        self.assertIn('dastabase_', self.page.locator('header').inner_text())
        self.assertIn('CONTROL ROOM', self.page.locator('header').inner_text())
        self.assertIn('SPARROW v0.9.0', self.page.locator('header').inner_text())
        self.assertIn('10 / 10', self.page.locator('.detail').inner_text())
        self.assertIn('enrichment complete', self.page.locator('[data-events]').inner_text())
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertFalse(self.errors)

    def test_polling_adds_only_downloadable_artifacts_without_reload(self):
        repository=self.app.extensions['control_repository']; job=repository.create_job('Artifacts',1)
        self.page.goto(f'http://control.test/jobs/{job["job_id"]}')
        self.assertEqual(self.page.locator('[data-artifacts] a').count(),0)
        storage=self.root/'storage'; storage.mkdir()
        for kind,name in (('DISCOVERY_EXPORT','companies.csv'),
                          ('UPLOAD_RECONCILIATION','enriched_upload.csv'),
                          ('DISCOVERY_RESULTS_DB','results.sqlite3')):
            path=storage/name; path.write_text(kind)
            repository.add_artifact(job['job_id'],kind,name,path.stat().st_size,
                                    hashlib.sha256(path.read_bytes()).hexdigest())
        self.page.get_by_role('link',name='DISCOVERY EXPORT').wait_for(timeout=5000)
        self.assertEqual(self.page.get_by_role('link',name='ENRICHED UPLOAD').count(),1)
        self.assertEqual(self.page.get_by_role('link',name='results.sqlite3').count(),0)
        self.assertFalse(self.errors)


if __name__ == '__main__':
    unittest.main()
