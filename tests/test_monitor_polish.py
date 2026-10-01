"""Product identity and frontend behavior; no worker, network or production DB use."""
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright
from engine_identity import ENGINE_IDENTITY
from monitor.app import create_app
from monitor.metrics import activity_entry

ROOT = Path(__file__).resolve().parents[1]


def status_fixture():
    return dict(timestamp='2026-09-26T12:00:00Z',
        database=dict(total=100, processed=42, remaining=58, percent=42,
                      statuses={'VERIFIED': 40, 'ERROR': 2}, emails=10, email_companies=8),
        discovery_v2=dict(available=True, note=None, database='batch/results.sqlite3',
            run_id='run-1', run_status='RUNNING', selected=100, processed=42,
            pending=58, percent=42, company_statuses={'COMPLETED':40,'PARTIAL':2,
            'FAILED':0,'INELIGIBLE':0,'RUNNING':1,'PENDING':57},
            success=42, success_denominator=42, success_percent=100,
            website_statuses={'VERIFIED':5,'HIGH':16,'MEDIUM':6,'REVIEW':15},
            website_total=42, website_percentages={'VERIFIED':11.9,'HIGH':38.1,
            'MEDIUM':14.3,'REVIEW':35.7}, current_company={'id':6353,'name':'FRBEŽAR d.o.o.'},
            recent_activity=[],
            usable_websites=27, default_email_companies=25,
            default_phone_companies=16, serper_evidence_companies=41,
            last_activity='2026-09-26T11:59:00Z'),
        active=dict(available=True, jobs=[dict(identity='1:100', pid=1,
            job_type='WEBSITE + EMAIL DISCOVERY', report='example/batch.json',
            selected=100, processed=42, remaining=58, percent=42, runtime_seconds=123,
            current_company={'id': 6353, 'name': 'FRBEŽAR d.o.o.'},
            last_completed={'company_id': 6352, 'company_name': 'MARTIN BARBIČ d.o.o.', 'status': 'ERROR'},
            circuit_breaker=False)]),
        workload=dict(state='active', active_batches=1, batch_remaining=58,
                      database_remaining=58, remaining_after_batch=0),
        activity=dict(source='active', note='Fixture', entries=[dict(company_id=6352,
            company_name='MARTIN BARBIČ d.o.o.', status='ERROR', report='example/batch.json')]),
        recent=dict(jobs=[], skipped=0), services=[])


class MonitorIdentityTests(unittest.TestCase):
    def test_shared_identity_and_header(self):
        self.assertEqual(ENGINE_IDENTITY.engine_name, 'SPARROW')
        self.assertEqual(ENGINE_IDENTITY.engine_version, '0.9.0')
        client = create_app({'TESTING': True}).test_client()
        html = client.get('/').get_data(as_text=True)
        self.assertIn('SPARROW 0.9.0', html)
        self.assertNotIn('ENGINE ·', html)
        logo = client.get('/static/assets/dastabase-napis-prozorna-za-temno.svg')
        self.assertEqual(logo.status_code, 200)
        self.assertEqual(logo.mimetype, 'image/svg+xml')
        self.assertNotIn('READ ONLY / DUKE', html)
        self.assertIn('easter_eggs.js', html)
        self.assertIn('read-only SQLite', html)
        self.assertEqual(client.get('/static/easter_eggs.js').status_code, 200)

    def test_status_semantics_and_no_controls(self):
        self.assertEqual(activity_entry({'company_id': 1, 'website': {'status': 'ERROR'}}, None)['status'], 'ERROR')
        app = create_app({'TESTING': True})
        client = app.test_client()
        for route in ('/', '/api/status', '/api/live'):
            for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                self.assertEqual(client.open(route, method=method).status_code, 405)
        self.assertEqual({r.rule for r in app.url_map.iter_rules()}, {'/', '/api/status', '/api/live', '/static/<path:filename>'})


class MonitorPolishBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 1280, 'height': 1000})

    def tearDown(self):
        self.page.close()

    def eggs(self, code):
        self.page.set_content('<div id="egg"></div>')
        self.page.add_script_tag(path=str(ROOT/'monitor/static/easter_eggs.js'))
        return self.page.evaluate('''code => {
            let clock=0, draws=0, draw=0.5, visible=true;
            const timers=[], target=document.querySelector('#egg');
            const controller=DastabaseEasterEggs.create(target, {
                now:()=>clock, random:()=>{draws++;return draw}, visible:()=>visible,
                schedule:(fn,ms)=>timers.push({fn,ms})});
            const scene=(n,report='batch',extra={})=>({active:{available:true,jobs:[{
                report,processed:n,selected:200,circuit_breaker:false,last_completed:{status:'VERIFIED'},...extra}]},recent:{jobs:[]}});
            const show=()=>target.textContent;
            const clear=()=>{for(const timer of timers.splice(0))timer.fn()};
            return eval(code);
        }''', code)

    def test_42_once_per_batch_even_after_resume(self):
        result = self.eggs('''(() => {
            controller.observe(scene(41)); const before=show();
            controller.observe(scene(42)); const first=show(),delay=timers[0].ms; clear();
            clock=180000; controller.observe(scene(42)); const held=show();
            controller.observe(scene(43));controller.observe(scene(42,'batch',{identity:'resumed'}));const resumed=show();
            controller.observe(scene(42,'second'));const second=show();
            return {before,first,delay,held,resumed,second,draws};
        })()''')
        self.assertEqual(result['first'], '🚀 deep space enrichment in progress')
        self.assertEqual(result['second'], result['first'])
        self.assertEqual(result['delay'], 6000)
        for key in ('before', 'held', 'resumed'):
            self.assertEqual(result[key], '')
        self.assertEqual(result['draws'], 0)

    def test_cookie_observed_crossing_or_completed_active_batch(self):
        result = self.eggs('''(() => {
            controller.observe(scene(100));const history=show();
            controller.observe(scene(99,'live'));controller.observe(scene(101,'live'));const crossing=show();clear();
            clock=180000;controller.observe(scene(101,'live'));const repeat=show();
            controller.observe(scene(99,'finishing'));
            controller.observe({active:{available:true,jobs:[]},recent:{jobs:[{name:'finishing',status:'complete',selected:100,processed:100}]}});
            return {history,crossing,repeat,completion:show()};
        })()''')
        self.assertEqual(result['history'], '')
        self.assertEqual(result['repeat'], '')
        self.assertEqual(result['crossing'], '🍪 cookie break avoided. still working.')
        self.assertEqual(result['completion'], result['crossing'])

    def test_hidden_cooldown_and_missing_report_suppress_without_queue(self):
        result = self.eggs('''(() => {
            controller.observe(scene(42,null));const unknown=show();
            visible=false;controller.observe(scene(42,'hidden'));visible=true;
            clock=180000;controller.observe(scene(42,'hidden'));const hidden=show();
            controller.observe(scene(42,'one'));clear();controller.observe(scene(42,'two'));
            clock=360000;controller.observe(scene(42,'two'));
            return {unknown,hidden,suppressed:show(),timers:timers.length};
        })()''')
        self.assertEqual(result, {'unknown': '', 'hidden': '', 'suppressed': '', 'timers': 0})

    def test_skipped_or_regressing_counts_do_not_invent_milestones(self):
        result = self.eggs("""(() => {
            controller.observe(scene(41));controller.observe(scene(43));
            controller.observe(scene(42));const skipped=show();
            controller.observe(scene(101,'old'));controller.observe(scene(99,'old'));
            controller.observe(scene(100,'old'));return {skipped,regressed:show()};
        })()""")
        self.assertEqual(result, {'skipped': '', 'regressed': ''})

    def test_rare_motifs_throttled_and_dove_has_no_explanation(self):
        for draw, expected in ((0.0001,'🦖'),(0.001,'🎿'),(0.008,'🕊'),(0.02,'⛵')):
            with self.subTest(draw=draw):
                result = self.eggs(f'''(() => {{
                    draw={draw};controller.observe(scene(1));clock=1199999;controller.observe(scene(2));const before=draws;
                    clock=1200000;controller.observe(scene(3));const first=show();clear();
                    clock=1300000;controller.observe(scene(4));return {{before,first,draws,after:show()}};
                }})()''')
                self.assertTrue(result['first'].startswith(expected))
                if expected == '🕊':
                    self.assertEqual(result['first'], expected)
                self.assertEqual((result['before'], result['draws'], result['after']), (0, 1, ''))

    def test_rendered_labels_compaction_and_safe_loading(self):
        client = create_app({'TESTING': True}).test_client()
        errors, requests = [], []
        self.page.on('pageerror', lambda e: errors.append(str(e)))
        self.page.on('request', lambda r: requests.append(r.url))
        def serve(route):
            path = route.request.url.removeprefix('http://monitor.test')
            if path == '/api/status':
                route.fulfill(json=status_fixture())
            elif path == '/api/live':
                route.fulfill(json={'timestamp':'2026-09-26T12:00:00Z', 'server': {}, 'processes':{'available':True,'jobs':[]}})
            else:
                response = client.get(path)
                route.fulfill(status=response.status_code,body=response.data,content_type=response.content_type)
        self.page.route('**/*', serve)
        self.page.goto('http://monitor.test/')
        self.page.locator('#active').filter(has_text='UNRESOLVED').wait_for()
        active = self.page.locator('#active').inner_text()
        self.assertIn('6353 · FRBEŽAR d.o.o.', active)
        self.assertIn('6352 · MARTIN BARBIČ d.o.o. · UNRESOLVED', active)
        self.assertNotIn('Current ID', active)
        self.assertNotIn('Current name', active)
        self.assertIn('Unresolved', self.page.locator('#results').inner_text())
        self.assertIn('UNRESOLVED', self.page.locator('#activity').inner_text())
        discovery = self.page.locator('#discovery-v2').inner_text()
        self.assertIn('42 / 100 · 42%', discovery)
        self.assertIn('Companies with Serper evidence', discovery)
        self.assertIn('27', discovery)
        self.assertIn('SUCCESS', discovery.upper())
        self.assertEqual(self.page.locator('#discovery-v2 .segment').count(), 4)
        self.assertIn('SPARROW 0.9.0', self.page.locator('header').inner_text())
        self.assertNotIn('ENGINE', self.page.locator('header').inner_text())
        self.assertTrue(self.page.locator('.dastabase-logo').evaluate('(img) => img.complete && img.naturalWidth > 0'))
        for label in ('PID','Runtime','Circuit breaker','Batch','Companies','Remaining'):
            self.assertIn(label, active)
        fixture = status_fixture()
        returned = self.page.evaluate('data => {renderStatus(data); return data}', fixture)
        self.assertEqual(returned, fixture)
        fixture['active']['jobs'][0]['current_company'] = None
        self.page.evaluate('data => renderStatus(data)', fixture)
        self.assertNotIn('FRBEŽAR', self.page.locator('#active').inner_text())
        self.page.set_viewport_size({'width':390,'height':844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertFalse(errors)
        self.assertTrue(all(url.startswith('http://monitor.test/') for url in requests))
