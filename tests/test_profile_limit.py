import hashlib
import json

import pytest

from profile_v1.runner import main, run
from profile_v1.semantic import LiveConfig, LiveInterpreter
from profile_v1.store import Store, uid


def profile_run(tmp_path):
    source=tmp_path/'source.sqlite3'; source.write_bytes(b'frozen-source')
    descriptor={'database_path':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'run_id':'discovery'}
    companies=[{'identity':{'id':company_id,'company_name':f'Company {company_id}'},
      'registered_activity':None,'discovery_attempt_id':f'da-{company_id}',
      'discovery_result_id':f'dr-{company_id}','accepted_website':f'https://company{company_id}.si'}
      for company_id in (10,20)]
    target=tmp_path/'profile.sqlite3'; store=Store(target,create=True,require_new=True)
    run_id=store.create_run(descriptor,companies); store.close()
    return target,run_id


def add_corpus(store,run_id,attempt_id,company,*source_args):
    evidence_id=uid(); block_id=uid()
    with store.conn:
        store.insert('profile_evidence',{'evidence_id':evidence_id,'run_id':run_id,'attempt_id':attempt_id,
          'company_id':company['id'],'source_kind':'DISCOVERY_FETCHED_PAGE','source_class':'FIRST_PARTY',
          'requested_url':None,'final_url':f"https://company{company['id']}.si",'title':None,
          'snippet_body':None,'html':None,'content_hash':'hash','observed_at':'now','payload_json':'{}',
          'origin_database':'source','origin_run_id':'discovery','origin_attempt_id':f"da-{company['id']}",
          'origin_evidence_id':'evidence','origin_content_hash':'hash'})
        store.insert('profile_content_blocks',{'block_id':block_id,'run_id':run_id,'attempt_id':attempt_id,
          'company_id':company['id'],'evidence_id':evidence_id,'source_url':f"https://company{company['id']}.si",
          'block_type':'PARAGRAPH','source_locator':'p:1','text':'Insufficient activity information.',
          'text_hash':'text-hash','language':None})
    return []


def test_limit_one_processes_first_pending_and_makes_at_most_one_live_call(tmp_path,monkeypatch):
    target,run_id=profile_run(tmp_path); calls=[]
    monkeypatch.setattr('profile_v1.runner.import_company',add_corpus)
    interpreter=LiveInterpreter(LiveConfig('mock','model','https://invalid.example','secret'),
      lambda config,payload:(calls.append(payload) or {'choices':[{'message':{'content':'{"claims":[]}'}}]}))
    run(target,run_id,interpreter,limit=1)
    store=Store(target)
    rows=store.conn.execute('SELECT company_id,status FROM profile_run_companies ORDER BY manifest_position').fetchall()
    assert [tuple(row) for row in rows]==[(10,'INSUFFICIENT_EVIDENCE'),(20,'PENDING')]
    assert len(calls)==1
    assert store.conn.execute('SELECT COUNT(*) FROM profile_interpretation_requests').fetchone()[0]==1
    store.close()


def test_cli_passes_limit_one_to_runner(tmp_path,monkeypatch):
    interpretations=tmp_path/'interpretations.json'; interpretations.write_text(json.dumps({}))
    captured={}
    monkeypatch.setattr('profile_v1.runner.run',lambda *args,**kwargs:captured.update(kwargs))
    main(['run','--results','profile.sqlite3','--run-id','run-id',
          '--interpretations',str(interpretations),'--limit','1'])
    assert captured['limit']==1


@pytest.mark.parametrize('value',['0','-1'])
def test_cli_rejects_nonpositive_limit(value,capsys):
    with pytest.raises(SystemExit) as error:
        main(['run','--results','unused','--run-id','run','--live','--limit',value])
    assert error.value.code==2
    assert '--limit must be greater than zero' in capsys.readouterr().err
