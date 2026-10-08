import csv, hashlib, json, sqlite3
from pathlib import Path

import pytest

from discovery_v2.store import APPLICATION_ID, SCHEMA_VERSION
import profile_v1.importer as importer_module
import profile_v1.runner as runner_module
from profile_v1.contracts import CandidateClaim, Citation
from profile_v1.blocks import extract_blocks
from profile_v1.export import export
from profile_v1.runner import create, replay, run
from profile_v1.pilot import freeze, select
from profile_v1.store import Store


class FixtureInterpreter:
    version='fixture-1'
    def interpret(self,company,blocks):
        block=next(b for b in blocks if 'industrial pumps' in b['text'])
        quote='We manufacture industrial pumps for European factories.'
        return [
            CandidateClaim('ACTUAL_PRIMARY_ACTIVITY','pump manufacturing','Pump manufacturing',(Citation(block['block_id'],quote),),'HIGH'),
            CandidateClaim('BUSINESS_AUDIENCE','B2B','B2B',(Citation(block['block_id'],'European factories'),),'HIGH'),
            CandidateClaim('MANUFACTURER_SIGNAL','YES','YES',(Citation(block['block_id'],'manufacture'),),'HIGH'),
            CandidateClaim('BUSINESS_DESCRIPTION','Industrial pump manufacturer','Industrial pump manufacturer',(Citation(block['block_id'],quote),),'HIGH')]


def discovery_db(tmp_path):
    path=tmp_path/'discovery.sqlite3'; conn=sqlite3.connect(path)
    conn.executescript(Path('discovery_v2/schema.sql').read_text())
    conn.execute(f'PRAGMA application_id={APPLICATION_ID}'); conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    identity={'id':7,'company_name':'PUMPA d.o.o.','tax_number':'SI7','registration_number':'7','address':'Road 1','municipality':'Kranj','registered_activity':'C28'}
    conn.execute("INSERT INTO discovery_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",('dr','DISCOVERY_CONTACTS','FRESH_DISCOVERY','COMPLETED','e','r','{}','{}','h','m','now','now','now'))
    conn.execute("INSERT INTO discovery_run_companies VALUES (?,?,?,?,?,?,?,?,?)",('dr',7,'lite',0,json.dumps(identity),'ELIGIBLE',None,'COMPLETED','a2'))
    for attempt,num in [('a1',1),('a2',2)]: conn.execute("INSERT INTO discovery_attempts VALUES (?,?,?,?,?,?,?,?,?)",(attempt,'dr',7,num,'COMPLETED','now','now','{}','{}'))
    html='<html><head><title>Pumpa</title><meta name="description" content="Pumps"></head><body><nav>Cookie menu</nav><h1>Solutions</h1><p>We manufacture industrial pumps for European factories.</p><footer>boilerplate</footer><script type="application/ld+json">{"@type":"Organization","name":"Pumpa"}</script></body></html>'
    payload=json.dumps({'html':html})
    conn.execute("INSERT INTO discovery_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",('old','dr','a1',7,'FETCHED_PAGE',None,'FIRST_PARTY','now',None,None,None,None,None,None,None,'https://old.si','https://old.si',200,'oldhash',payload))
    digest=hashlib.sha256(html.encode()).hexdigest()
    conn.execute("INSERT INTO discovery_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",('page','dr','a2',7,'FETCHED_PAGE',None,'FIRST_PARTY','now',None,None,None,None,None,'Pumpa',None,'https://pumpa.si','https://pumpa.si',200,digest,payload))
    conn.execute("INSERT INTO discovery_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",('search','dr','a2',7,'SEARCH_RESULT','SERPER','SEARCH','now','DIRECT', 'q',1,'https://publisher.si/pumpa','publisher.si','Profile','Pumpa makes pumps',None,None,None,'searchhash','{}'))
    conn.execute("INSERT INTO discovery_company_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",('result','dr','a2',7,'HIGH','https://pumpa.si','https://pumpa.si/',None,'["page"]','{}',None,None,'NO_CONTACTS','now'))
    conn.commit(); conn.close(); return path,digest


def prepared(tmp_path):
    source,digest=discovery_db(tmp_path); profile=tmp_path/'profile.sqlite3'
    run_id=create(source,'dr',[7],profile); run(profile,run_id,FixtureInterpreter())
    return source,profile,run_id,digest


def test_vertical_slice_import_validation_export_and_read_only(tmp_path):
    source,profile,run_id,digest=prepared(tmp_path); before=source.read_bytes()
    store=Store(profile)
    evidence=store.conn.execute('SELECT * FROM profile_evidence ORDER BY source_kind').fetchall()
    assert {x['origin_evidence_id'] for x in evidence}=={'page','search'}
    page=next(x for x in evidence if x['source_class']=='FIRST_PARTY')
    assert page['content_hash']==digest==page['origin_content_hash']
    texts=[x[0] for x in store.conn.execute('SELECT text FROM profile_content_blocks')]
    assert any('industrial pumps' in x for x in texts)
    assert not any('Cookie menu' in x or 'boilerplate' in x for x in texts)
    assert any('Organization' in x for x in texts)
    result=store.conn.execute('SELECT * FROM profile_company_results').fetchone()
    assert result['profile_status']=='PROFILED' and result['claim_count']==4
    deterministic=store.conn.execute('SELECT * FROM profile_deterministic_results').fetchone()
    assert deterministic['corpus_hash'] and deterministic['rule_version']=='profile-deterministic-1'
    store.close(); assert source.read_bytes()==before
    csv_path=tmp_path/'review.csv'; json_path=tmp_path/'review.json'; export(profile,run_id,csv_path,json_path)
    row=next(csv.DictReader(csv_path.open(encoding='utf-8-sig')))
    assert row['actual_primary_activity']=='Pump manufacturing' and row['registered_activity']=='"C28"'
    assert row['official_website']=='https://pumpa.si' and row['supported_claim_count']=='4'
    assert row['PRIMARY_ACTIVITY_CORRECT']=='' and row['REVIEW_NOTES']==''
    assert json.loads(json_path.read_text())[0]['claims'][0]['citations']


@pytest.mark.parametrize(('claim','reason'),[
 (CandidateClaim('PRODUCTS','pump','Pump',(Citation('missing','pump'),)), 'MISSING_BLOCK'),
 (CandidateClaim('PRODUCTS','pump','Pump',(Citation('BLOCK','fabricated'),)), 'FABRICATED_QUOTE'),
 (CandidateClaim('BUSINESS_AUDIENCE','ENTERPRISE','Enterprise',(Citation('BLOCK','manufacture'),)), 'INVALID_ENUM'),
 (CandidateClaim('BRANDS','x','X',(Citation('BLOCK','manufacture'),)), 'UNSUPPORTED_CLAIM_TYPE'),
 (CandidateClaim('BUSINESS_DESCRIPTION','x','X',()), 'EVIDENCE_REQUIRED')])
def test_invalid_candidates_remain_rejected(tmp_path,claim,reason):
    source,profile,run_id,_=prepared(tmp_path); store=Store(profile)
    row=store.conn.execute('SELECT * FROM profile_run_companies').fetchone(); attempt=store.start_attempt(run_id,7)
    # Copy one permitted block/evidence into the new attempt through offline replay machinery first.
    store.close(); replay(profile,run_id); store=Store(profile)
    attempt=store.conn.execute('SELECT selected_attempt_id FROM profile_run_companies').fetchone()[0]
    block=store.conn.execute('SELECT block_id FROM profile_content_blocks WHERE attempt_id=?',(attempt,)).fetchone()[0]
    from profile_v1.validation import validate_and_store
    if claim.citations and claim.citations[0].block_id=='BLOCK': claim=CandidateClaim(claim.claim_type,claim.normalized_value,claim.display_value,(Citation(block,claim.citations[0].quote),),claim.confidence)
    validate_and_store(store,{'run_id':run_id,'attempt_id':attempt,'company_id':7},[claim],'bad-fixture')
    assert store.conn.execute('SELECT rejection_reason FROM profile_claims ORDER BY rowid DESC').fetchone()[0]==reason
    store.close()


def test_insufficient_evidence_and_offline_replay(tmp_path):
    source,profile,run_id,_=prepared(tmp_path)
    source.unlink(); replay(profile,run_id)
    store=Store(profile); result=store.conn.execute('SELECT * FROM profile_company_results ORDER BY completed_at DESC,rowid DESC').fetchone()
    assert result['profile_status']=='PROFILED'; store.close()
    source2,_=discovery_db(tmp_path); empty=tmp_path/'empty.sqlite3'; rid=create(source2,'dr',[7],empty); run(empty,rid)
    store=Store(empty); assert store.conn.execute('SELECT profile_status FROM profile_company_results').fetchone()[0]=='INSUFFICIENT_EVIDENCE'; store.close()


def test_wrong_run_company_and_source_mutation_are_rejected(tmp_path):
    source,_=discovery_db(tmp_path)
    with pytest.raises(ValueError,match='Unknown Discovery run'): create(source,'wrong',[7],tmp_path/'a.sqlite3')
    with pytest.raises(ValueError,match='no selected'): create(source,'dr',[8],tmp_path/'b.sqlite3')
    profile=tmp_path/'profile.sqlite3'; rid=create(source,'dr',[7],profile)
    with source.open('ab') as handle: handle.write(b'changed')
    with pytest.raises(ValueError,match='hash mismatch'): run(profile,rid)


def test_pilot_selection_is_deterministic_and_frozen(tmp_path):
    source,_=discovery_db(tmp_path)
    first=select(source,'dr',1); second=select(source,'dr',1)
    assert first['companies']==second['companies'] and first['manifest_sha256']==second['manifest_sha256']
    path=tmp_path/'pilot.json'; frozen=freeze(source,'dr',path,1)
    assert json.loads(path.read_text())['manifest_sha256']==frozen['manifest_sha256']
    with pytest.raises(ValueError,match='already exists'): freeze(source,'dr',path,1)


def test_nested_noise_decomposition_does_not_crash_or_remove_valid_content():
    html='''<html><body><div class="menu"><section><p>Discarded navigation</p></section></div>
      <main><h1>Useful heading</h1><p>Useful company activity content.</p></main></body></html>'''
    blocks=extract_blocks({'html':html})
    texts=[block['text'] for block in blocks]
    assert 'Useful heading' in texts
    assert 'Useful company activity content.' in texts
    assert 'Discarded navigation' not in texts


def test_deterministic_only_runner_has_no_semantic_result(tmp_path,monkeypatch):
    source,_=discovery_db(tmp_path); profile=tmp_path/'deterministic.sqlite3'
    run_id=create(source,'dr',[7],profile)
    monkeypatch.setattr('profile_v1.semantic.LiveInterpreter.call',
      lambda *args:pytest.fail('provider called'))
    monkeypatch.setattr('requests.sessions.Session.request',
      lambda *args,**kwargs:pytest.fail('network called'))
    run(profile,run_id,deterministic_only=True)
    store=Store(profile)
    company=store.conn.execute('SELECT status,selected_attempt_id FROM profile_run_companies').fetchone()
    assert company['status']=='COMPLETED' and company['selected_attempt_id']
    assert store.conn.execute('SELECT COUNT(*) FROM profile_deterministic_results').fetchone()[0]==1
    assert store.conn.execute('SELECT COUNT(*) FROM profile_company_results').fetchone()[0]==0
    assert store.conn.execute('SELECT COUNT(*) FROM profile_interpretation_requests').fetchone()[0]==0
    store.close()


def test_deterministic_only_replay_has_no_network_or_semantic_path(tmp_path,monkeypatch):
    source,_=discovery_db(tmp_path); profile=tmp_path/'deterministic-replay.sqlite3'
    run_id=create(source,'dr',[7],profile); run(profile,run_id,deterministic_only=True)
    monkeypatch.setattr('profile_v1.semantic.LiveInterpreter.call',
      lambda *args:pytest.fail('provider called'))
    monkeypatch.setattr('requests.sessions.Session.request',
      lambda *args,**kwargs:pytest.fail('network called'))
    replay(profile,run_id,deterministic_only=True)
    store=Store(profile)
    assert store.conn.execute('SELECT COUNT(*) FROM profile_deterministic_results').fetchone()[0]==2
    assert store.conn.execute('SELECT COUNT(*) FROM profile_interpretation_requests').fetchone()[0]==0
    store.close()


def test_review_fetched_page_is_preserved_as_candidate_but_not_interpreted(tmp_path):
    source,_=discovery_db(tmp_path); conn=sqlite3.connect(source)
    conn.execute("UPDATE discovery_company_results SET website_status='REVIEW',official_website=NULL,verified_scope=NULL")
    conn.commit(); conn.close()
    profile=tmp_path/'review.sqlite3'; run_id=create(source,'dr',[7],profile)
    run(profile,run_id,deterministic_only=True)
    store=Store(profile)
    assert store.conn.execute("SELECT COUNT(*) FROM profile_evidence WHERE source_class='CANDIDATE'").fetchone()[0]==1
    result=store.conn.execute('SELECT corpus_status FROM profile_deterministic_results').fetchone()[0]
    title=store.conn.execute("SELECT state FROM profile_deterministic_facts WHERE field_name='site_title'").fetchone()[0]
    assert result=='CANDIDATE_ONLY' and title=='UNKNOWN'
    store.close()


def test_out_of_scope_final_redirect_is_candidate_and_cannot_create_any_company_fact(tmp_path):
    source,_=discovery_db(tmp_path); conn=sqlite3.connect(source)
    html='''<html lang="en"><head><title>Third-party title</title>
      <meta name="description" content="Third-party description">
      <script type="application/ld+json">{"@graph":[
       {"@type":"Organization","name":"PUMPA d.o.o."},
       {"@type":"Product","name":"Pump","manufacturer":{"@type":"Organization","name":"PUMPA d.o.o."}},
       {"@type":"Service","name":"Installation"}]}</script></head><body>
      <h1>Products</h1><p>We manufacture and export industrial pumps B2B and B2C.</p></body></html>'''
    digest=hashlib.sha256(html.encode()).hexdigest()
    conn.execute("""UPDATE discovery_evidence SET final_url=?,evidence_payload_json=?,content_hash=?
      WHERE evidence_id='page'""",('https://third-party.example/redirected',json.dumps({'html':html}),digest))
    conn.commit(); conn.close()
    profile=tmp_path/'redirect.sqlite3'; run_id=create(source,'dr',[7],profile)
    run(profile,run_id,deterministic_only=True); store=Store(profile)
    candidate=store.conn.execute("SELECT * FROM profile_evidence WHERE origin_evidence_id='page'").fetchone()
    assert candidate['source_class']=='CANDIDATE'
    assert candidate['requested_url']=='https://pumpa.si' and candidate['final_url']=='https://third-party.example/redirected'
    assert 'Third-party title' in candidate['html']
    assert store.conn.execute('SELECT COUNT(*) FROM profile_content_blocks WHERE evidence_id=?',(candidate['evidence_id'],)).fetchone()[0]==0
    values={row['field_name']:row['state'] for row in store.conn.execute('SELECT field_name,state FROM profile_deterministic_facts')}
    for field in ('site_title','meta_description','page_metadata','declared_languages',
      'structured_organizations','manufacturer_signal','international_signal',
      'product_presence_signal','service_presence_signal','explicit_audience_markers'):
        assert values[field]=='UNKNOWN'
    store.close()


def test_source_mutation_between_runner_check_and_import_is_rejected_without_corpus(tmp_path,monkeypatch):
    source,_=discovery_db(tmp_path); profile=tmp_path/'mutated.sqlite3'
    run_id=create(source,'dr',[7],profile); original=runner_module.import_company
    def mutate_then_import(*args,**kwargs):
        conn=sqlite3.connect(source)
        conn.execute("UPDATE discovery_evidence SET title='changed after runner hash check' WHERE evidence_id='page'")
        conn.commit(); conn.close()
        return original(*args,**kwargs)
    monkeypatch.setattr(runner_module,'import_company',mutate_then_import)
    with pytest.raises(ValueError,match='hash mismatch'): run(profile,run_id,deterministic_only=True)
    store=Store(profile)
    attempt=store.conn.execute('SELECT status,diagnostics_json FROM profile_attempts').fetchone()
    assert attempt['status']=='FAILED' and 'hash mismatch' in json.loads(attempt['diagnostics_json'])['error']
    assert store.conn.execute('SELECT COUNT(*) FROM profile_evidence').fetchone()[0]==0
    assert store.conn.execute('SELECT COUNT(*) FROM profile_content_blocks').fetchone()[0]==0
    assert store.conn.execute('SELECT COUNT(*) FROM profile_deterministic_results').fetchone()[0]==0
    store.close()


def test_source_mutation_during_import_rolls_back_copied_corpus(tmp_path,monkeypatch):
    source,_=discovery_db(tmp_path); profile=tmp_path/'mutated-during-import.sqlite3'
    run_id=create(source,'dr',[7],profile); original_hash=importer_module.file_hash
    def mutate_before_final_hash(path):
        conn=sqlite3.connect(source)
        conn.execute("UPDATE discovery_evidence SET title='changed during import' WHERE evidence_id='page'")
        conn.commit(); conn.close()
        return original_hash(path)
    monkeypatch.setattr(importer_module,'file_hash',mutate_before_final_hash)
    with pytest.raises(ValueError,match='changed during import'):
        run(profile,run_id,deterministic_only=True)
    store=Store(profile)
    assert store.conn.execute('SELECT status FROM profile_attempts').fetchone()[0]=='FAILED'
    assert store.conn.execute('SELECT COUNT(*) FROM profile_evidence').fetchone()[0]==0
    assert store.conn.execute('SELECT COUNT(*) FROM profile_content_blocks').fetchone()[0]==0
    assert store.conn.execute('SELECT COUNT(*) FROM profile_deterministic_results').fetchone()[0]==0
    store.close()


def test_schema_two_semantic_artifact_remains_readable_by_export(tmp_path):
    source,profile,run_id,_=prepared(tmp_path)
    conn=sqlite3.connect(profile)
    conn.executescript('''DROP TABLE profile_deterministic_fact_evidence;
      DROP TABLE profile_deterministic_facts; DROP TABLE profile_deterministic_results;
      PRAGMA user_version=2;'''); conn.close()
    csv_path=tmp_path/'version-two.csv'; export(profile,run_id,csv_path)
    row=next(csv.DictReader(csv_path.open(encoding='utf-8-sig')))
    assert row['profile_status']=='PROFILED' and row['company_name']=='PUMPA d.o.o.'
