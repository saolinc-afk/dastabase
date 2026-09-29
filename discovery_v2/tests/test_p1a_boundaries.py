"""Authorization boundaries using isolated temporary run databases."""
import json
import unittest
from unittest.mock import patch
from discovery_v2.tests import test_phase_a as fixtures
from discovery_v2.interfaces import evaluate_website

class BoundaryTests(unittest.TestCase):
    setUp = fixtures.PhaseATests.setUp
    create = fixtures.PhaseATests.create
    execute = fixtures.PhaseATests.execute
    rows = fixtures.PhaseATests.rows

    def test_runner_rejects_ruleless_and_unknown_authorizers(self):
        for rule in (None,'OWN-99_UNKNOWN'):
            def fake(company,url,fetcher):
                fetcher.fetch(url)
                return dict(verified=True,status='VERIFIED',verified_scope=url,
                    ownership=dict(status='VERIFIED',scope=url,relationship='STANDALONE'),
                    p1a=dict(rule_id=rule))
            _, result, _, _ = self.execute(evaluator=fake)
            self.assertFalse(any(r['website_status']=='VERIFIED' for r in self.rows('discovery_company_results')))

    def test_valid_recorded_rule_persists_and_witnesses_exist(self):
        run_id,result,_,_=self.execute()
        self.assertEqual(result['failed'],0)
        record=self.store.current_results(run_id)[0]
        self.assertEqual(record['website_status'],'VERIFIED')
        assessment=json.loads(record['website_assessment_json'])
        chosen=next(c['assessment']['p1a'] for c in assessment['candidates'] if c['observation_id']==record['website_observation_id'])
        persisted={o['observation_id'] for o in self.rows('discovery_observations')}
        self.assertTrue(set(chosen['supporting_observation_ids'])<=persisted)
        self.assertTrue(chosen['operator_evidence'])

    def test_store_rejects_tampered_authorization_and_scope(self):
        for change in ('ruleless','unknown','scope','provenance','blocker','offline','authorization_basis'):
            original=self.store.complete
            reached=[]
            def corrupt(context,contacts,result):
                reached.append(True)
                assessment=json.loads(result['website_assessment_json'])
                chosen=next(c['assessment'] for c in assessment['candidates'] if c['observation_id']==result['website_observation_id'])
                if change=='ruleless': chosen['p1a'].pop('rule_id')
                elif change=='unknown': chosen['p1a']['rule_id']='OWN-99_UNKNOWN'
                elif change=='scope': result['verified_scope']=result['official_website']='https://wrong.example/'
                elif change=='provenance': chosen['p1a']['supporting_observation_ids']=[]
                elif change=='blocker': chosen['p1a']['blockers']=['EXACT_IDENTIFIER_CONFLICT']
                elif change=='authorization_basis': chosen['p1a']['authorization_basis']='STRONG_LEGAL_PAGE'
                else: chosen['p1a']['verified_without_fetch']=True
                result['website_assessment_json']=json.dumps(assessment)
                original(context,contacts,result)
            html='<section>Website operated by ALFA d.o.o.; VAT: 12345678</section>'
            with self.subTest(change=change), patch.object(self.store,'complete',side_effect=corrupt):
                _, result, _, _ = self.execute(pages={'https://alfa.si/':fixtures.response(html)})
                self.assertEqual(result['failed'],1)
                self.assertEqual(reached,[True])
            self.assertEqual(self.rows('discovery_company_results'),[])

    def test_store_persists_high_reasoning_and_rejects_altered_confidence_rule(self):
        original = self.store.complete
        reached = []
        def corrupt(context, contacts, result):
            reached.append(True)
            assessment = json.loads(result['website_assessment_json'])
            chosen = next(c['assessment'] for c in assessment['candidates']
                          if c['observation_id'] == result['website_observation_id'])
            self.assertEqual(result['website_status'], 'HIGH')
            self.assertEqual(chosen['p1a']['confidence_rule_id'],
                             'CONF-01_LEGAL_ADDRESS_CONVERGENCE')
            chosen['p1a']['confidence_rule_id'] = 'CONF-02_LEGAL_CONTACT_CONVERGENCE'
            result['website_assessment_json'] = json.dumps(assessment)
            original(context, contacts, result)
        html = '<section>ALFA d.o.o.; Glavna ulica 12, 1000 Ljubljana</section>'
        with patch.object(self.store, 'complete', side_effect=corrupt):
            _, outcome, _, _ = self.execute(pages={'https://alfa.si/': fixtures.response(html)})
        self.assertEqual(outcome['failed'], 1)
        self.assertEqual(reached, [True])
        self.assertEqual(self.rows('discovery_company_results'), [])

    def test_store_preserves_high_confidence_class_and_reasoning(self):
        html = '<section>ALFA d.o.o.; Glavna ulica 12, 1000 Ljubljana</section>'
        run_id, outcome, _, _ = self.execute(
            pages={'https://alfa.si/': fixtures.response(html)})
        self.assertEqual(outcome['failed'], 0)
        result = self.store.current_results(run_id)[0]
        self.assertEqual(result['website_status'], 'HIGH')
        assessment = json.loads(result['website_assessment_json'])
        chosen = next(c['assessment'] for c in assessment['candidates']
                      if c['observation_id'] == result['website_observation_id'])
        self.assertEqual(chosen['p1a']['confidence_rule_id'],
                         'CONF-01_LEGAL_ADDRESS_CONVERGENCE')
        self.assertTrue(chosen['p1a']['confidence_reasons'])

if __name__=='__main__': unittest.main()
