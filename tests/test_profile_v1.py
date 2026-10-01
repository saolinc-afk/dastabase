import csv, hashlib, json, sqlite3
from pathlib import Path

import pytest

from discovery_v2.store import APPLICATION_ID, SCHEMA_VERSION
from profile_v1.contracts import CandidateClaim, Citation
from profile_v1.export import export
from profile_v1.runner import create, replay, run
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
    store.close(); assert source.read_bytes()==before
    csv_path=tmp_path/'review.csv'; json_path=tmp_path/'review.json'; export(profile,run_id,csv_path,json_path)
    row=next(csv.DictReader(csv_path.open(encoding='utf-8-sig')))
    assert row['actual_primary_activity']=='Pump manufacturing' and row['registered_activity']=='"C28"'
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
