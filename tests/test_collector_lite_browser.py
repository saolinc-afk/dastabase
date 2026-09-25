"""Synthetic DOM tests in an isolated browser; all network requests are aborted."""
import tempfile
import json
import io
from contextlib import redirect_stdout
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import collector_lite as collector
import database_lite as db


def row(company_id):
    return f'''<li class="newsearchIcon"><h3><a href="https://www.gvin.com/detail?CompanyId={company_id}">Company {company_id}</a></h3>
    <span class="registrationNumber">Matična: {company_id}</span></li>'''


def document(action="", disabled=""):
    return f'''<ul id="results">{row(1)}{row(2)}</ul>
    <a id="ctl00_bamSearch_lnkPagingNext" {disabled}
       href="javascript:__doPostBack('ctl00$bamSearch_lnkPagingNext','')">Next</a>
    <script>function __doPostBack() {{{action}}}</script>'''


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.requests = []
        def block(route):
            self.requests.append(route.request.url)
            route.abort()
        self.page.route('**/*', block)

    def tearDown(self):
        self.assertEqual(self.requests, [], 'Collector attempted a network request')
        self.page.close()

    def test_async_next_and_disabled_last(self):
        self.page.set_content(document("""setTimeout(() => {
            document.querySelectorAll('li a').forEach((a, i) => a.href = '/detail?CompanyId=' + (i+3));
            document.querySelector('[id$="bamSearch_lnkPagingNext"]').setAttribute('aria-disabled', 'true');
        }, 100);"""))
        previous = collector.page_identity(self.page)
        self.assertTrue(collector.next_page(self.page, previous, timeout=1500))
        self.assertEqual(collector.page_identity(self.page), ['gvin:3', 'gvin:4'])
        self.assertFalse(collector.next_page(self.page, timeout=200))

    def test_unchanged_page_timeout(self):
        self.page.set_content(document())
        self.assertFalse(collector.next_page(self.page, timeout=200))

    def test_reordered_same_results_rejected(self):
        self.page.set_content(document("const list = document.querySelector('ul'); list.append(list.firstElementChild);"))
        self.assertFalse(collector.next_page(self.page, timeout=200))

    def test_missing_next(self):
        self.page.set_content('<ul>' + row(1) + '</ul>')
        self.assertFalse(collector.next_page(self.page, timeout=200))

    def test_unrecognized_next_does_not_navigate(self):
        self.page.set_content('<ul>' + row(1) + '</ul><a id="bamSearch_lnkPagingNext" href="https://www.gvin.com/detail?CompanyId=4">Next</a>')
        self.assertFalse(collector.next_page(self.page, timeout=200))

    def test_page_commit_and_repeat_preserve_ids(self):
        self.page.set_content(document())
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.initialize_database()
            self.assertEqual(collector.process_page(self.page), 2)
            self.assertEqual(collector.process_page(self.page), 2)
            conn = db.get_connection()
            try:
                self.assertEqual([r[0] for r in conn.execute('SELECT id FROM companies_lite ORDER BY id')], [1, 2])
            finally:
                conn.close()

    def test_expected_count_stops_at_29_without_extra_next(self):
        first = ''.join(row(i) for i in range(1, 21))
        second = ''.join(row(i) for i in range(21, 30))
        html = document("window.nextCalls = (window.nextCalls || 0) + 1; document.querySelector('#results').innerHTML = " + json.dumps(second) + ";")
        html = html.replace(row(1) + row(2), first)
        self.page.set_content('<span id="ctl00_lblPageStats">29</span>' + html)
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.initialize_database()
            # Existing company counts toward this run when successfully updated.
            db.save_company(collector.parse_company_row(row(1)))
            output = io.StringIO()
            original_next = collector.next_page
            def advance(*args):
                self.assertLess(self.page.evaluate('window.nextCalls || 0'), 1)
                return original_next(*args)
            with patch.object(collector, 'next_page', side_effect=advance), redirect_stdout(output):
                saved = collector.run_collection(self.page)
            self.assertEqual(len(saved), 29)
            self.assertEqual(self.page.evaluate('window.nextCalls'), 1)
            self.assertIn('Collection complete: 29 / 29 expected companies collected.', output.getvalue())
            conn = db.get_connection()
            try:
                self.assertEqual(conn.execute('SELECT count(*) FROM companies_lite').fetchone()[0], 29)
            finally:
                conn.close()

    def test_unique_count_excludes_duplicates_and_failed_rows(self):
        html = '<span id="ctl00_lblPageStats">3</span><ul>' + row(1) + row(1) + '<li class="newsearchIcon"><h3><a>No identifiers</a></h3></li></ul>'
        self.page.set_content(html)
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.initialize_database()
            with self.assertLogs(collector.LOG, level='WARNING') as messages:
                saved = collector.run_collection(self.page)
            self.assertEqual(len(saved), 1)
            self.assertTrue(any('Collection incomplete: 1 / 3' in m for m in messages.output))

    def test_count_detection_and_unknown_count_fallback(self):
        self.page.set_content('<span id="ctl00_lblPageStats">1.234</span>')
        self.assertEqual(collector.expected_result_count(self.page), 1234)
        self.page.set_content('<span id="ctl00_lblPageStats">1–20 / 29</span>')
        self.assertIsNone(collector.expected_result_count(self.page))
        self.page.set_content('<ul>' + row(1) + '</ul>')
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.initialize_database()
            self.assertEqual(len(collector.run_collection(self.page)), 1)

    def test_actual_heading_class(self):
        headings = ['Sredstva', 'Kapital', 'Celotni prih...', 'Čisti poslov...', 'Povprečno št...']
        self.page.set_content(''.join('<div class="advenceResultDisplaySubjektFinHeader">'+h+'</div>' for h in headings))
        self.assertEqual(collector.results_financial_headers(self.page), headings)

    def test_visible_challenge_stops(self):
        self.page.set_content(document() + '<input name="captcha">')
        self.assertFalse(collector.next_page(self.page, timeout=200))


if __name__ == '__main__':
    unittest.main()
