"""Durable Import identity resolution; all search payloads are injected."""
import hashlib
import sqlite3

from control_room.enrichment_index import EnrichmentIndex
from control_room.canonical_writer import CanonicalEntityWriter
from control_room.identity_resolver import (local_resolve, plan_queries,
    registration_input, research_allowed, resolve_results)
from control_room.import_enrich_adapter import ImportEnrichAdapter
from control_room.repository import JobRepository
from control_room.worker import Worker


def canonical(path):
    conn = sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE companies_lite(id INTEGER PRIMARY KEY,company_name TEXT,
        registration_number TEXT,tax_number TEXT,address TEXT,municipality TEXT,
        revenue_2025 REAL,profit_2025 REAL,employees_2025 REAL);
      INSERT INTO companies_lite VALUES
        (1,'ALFA d.o.o.','1000001','11111111','Alfa 1','Kranj',1,1,3),
        (2,'ROBOTINA d.o.o.','1000002','22222222','Robot 2','Kranj',1,1,3),
        (3,'DVOJNIK d.o.o.','1000003','33333333','A 3','Celje',1,1,3),
        (4,'DVOJNIK d.o.o.','1000004','44444444','B 4','Maribor',1,1,3),
        (5,'BETA d.o.o.','1000005','55555555','Beta 5','Ljubljana',1,1,3);
      CREATE TABLE website_discovery(id INTEGER PRIMARY KEY,company_id INTEGER,
        website TEXT,status TEXT,verified_scope TEXT,relationship TEXT);
      INSERT INTO website_discovery VALUES
        (1,1,'https://alfa.si/','VERIFIED','https://alfa.si/','LEGAL_ENTITY'),
        (2,2,'https://robotina.com/','VERIFIED','https://robotina.com/','LEGAL_ENTITY'),
        (3,3,'https://shared.si/celje','VERIFIED','https://shared.si/celje','LEGAL_ENTITY'),
        (4,4,'https://shared.si/maribor','VERIFIED','https://shared.si/maribor','LEGAL_ENTITY'),
        (5,5,'https://beta.si/','VERIFIED','https://beta.si/','LEGAL_ENTITY');
      CREATE TABLE email_discovery(id INTEGER PRIMARY KEY,company_id INTEGER,
        email TEXT,website TEXT);
    ''')
    conn.commit(); conn.close()


def normalized(**values):
    base = {'company_name':'','person_name':'','email':'','phone':'','tax_number':'',
            'registration_number':'','address':'','municipality':''}
    base.update(values)
    return registration_input(base)


def test_local_identifier_domain_public_shared_person_and_phone_rules(tmp_path):
    db = tmp_path/'canonical.db'; canonical(db); index = EnrichmentIndex(db)
    exact = local_resolve(index, normalized(tax_number='SI11111111'), 'UNRESOLVED')
    assert (exact.status, exact.canonical_company_id,
            exact.resolution_rule) == ('LOCAL_RESOLVED', 1, 'IDR-01_EXACT_IDENTIFIER')
    domain = local_resolve(index, normalized(company_name='Robotina',
        email='person@robotina.com'), 'AMBIGUOUS', (2,))
    assert (domain.status, domain.canonical_company_id,
            domain.resolution_rule) == ('LOCAL_RESOLVED', 2, 'IDR-02_OFFICIAL_DOMAIN_IDENTITY')
    shared = local_resolve(index, normalized(company_name='DVOJNIK',
        email='person@shared.si'), 'AMBIGUOUS', (3,4))
    assert shared.status == 'SEARCH_ELIGIBLE' and set(shared.alternatives) == {3,4}
    assert local_resolve(index, normalized(person_name='Ana',
        email='ana@gmail.com'), 'UNRESOLVED').status == 'NOT_ELIGIBLE'
    assert local_resolve(index, normalized(person_name='Ana',
        phone='+38640123456'), 'UNRESOLVED').status == 'NOT_ELIGIBLE'


def test_query_planning_is_deterministic_bounded_and_public_domain_free():
    inputs = normalized(company_name='ALFA d.o.o.', person_name='Ana Novak',
                        email='ana@alfa.si', municipality='Kranj')
    first = plan_queries(inputs)
    assert first == plan_queries(inputs)
    assert len(first) == 2
    assert first[0]['query_strategy'] == 'COMPANY_DOMAIN'
    public = plan_queries(normalized(company_name='ALFA', email='ana@gmail.com'))
    assert len(public) <= 2 and all('gmail.com' not in item['query_text'] for item in public)


def test_search_resolution_requires_canonical_identity_not_rank_or_website(tmp_path):
    db = tmp_path/'canonical.db'; canonical(db); index = EnrichmentIndex(db)
    inputs = normalized(company_name='Beta')
    official = [{'results': [{'url':'https://beta.si/','title':'BETA d.o.o.',
                              'snippet':'BETA d.o.o., Beta 5, Ljubljana'}]}]
    resolved = resolve_results(index, inputs, 'UNRESOLVED', (5,), official)
    assert (resolved.status, resolved.canonical_company_id) == ('SEARCH_RESOLVED', 5)
    directory = [{'results': [{'url':'https://bizi.si/BETA','title':'BETA d.o.o.',
                               'snippet':'BETA d.o.o. is first'}]}]
    rejected = resolve_results(index, inputs, 'UNRESOLVED', (5,), directory)
    assert rejected.status == 'UNRESOLVED' and rejected.canonical_company_id is None
    website_only = resolve_results(index, normalized(company_name='Unknown'),
        'UNRESOLVED', (), [{'results':[{'url':'https://plausible-example.si/','title':'Welcome'}]}])
    assert website_only.status == 'UNRESOLVED'


def test_search_conflicts_ambiguity_and_missing_canonical_target(tmp_path):
    db = tmp_path/'canonical.db'; canonical(db); index = EnrichmentIndex(db)
    conflict = resolve_results(index, normalized(company_name='Company'), 'UNRESOLVED', (),
        [{'results':[{'url':'https://one.si',
                      'title':'Tax 11111111 and registration number 1000005'}]}])
    assert conflict.status == 'AMBIGUOUS'
    ambiguous = resolve_results(index, normalized(company_name='DVOJNIK'), 'AMBIGUOUS',
        (3,4), [{'results':[{'url':'https://directory.example/dvojnik',
                             'title':'DVOJNIK d.o.o.'}]}])
    assert ambiguous.status == 'AMBIGUOUS'
    absent = resolve_results(index, normalized(company_name='Outside Entity'), 'UNRESOLVED', (),
        [{'results':[{'url':'https://outside.si','title':'Outside Entity tax 98765432'}]}])
    assert absent.status == 'CANONICAL_NOT_FOUND'


def test_verified_external_entity_is_proposed_but_never_created(tmp_path):
    db = tmp_path/'canonical.db'; canonical(db); index = EnrichmentIndex(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    decision = resolve_results(index, normalized(company_name='NOVA DRUŽBA d.o.o.'),
        'UNRESOLVED', (), [{'results':[{
            'url':'https://nova-druzba.si/kontakt',
            'title':'NOVA DRUŽBA d.o.o. | Kontakt',
            'snippet':'NOVA DRUŽBA d.o.o. Davčna številka: 98765432',
            'legal_name':'NOVA DRUŽBA d.o.o.', 'tax_number':'98765432',
            'entity_type':'d.o.o.', 'address':'Nova ulica 1',
            'municipality':'Ljubljana', 'country':'SI',
            'official_domain':'nova-druzba.si'}]}])
    assert decision.status == 'EXTERNAL_ENTITY_IDENTIFIED'
    assert decision.identity_status == 'RESOLVED_NEW_ENTITY'
    assert decision.persistence_status == 'PENDING_CREATE'
    assert decision.canonical_company_id is None
    assert decision.proposed_identity['legal_name'] == 'NOVA DRUŽBA d.o.o.'
    assert decision.proposed_identity['tax_number'] == '98765432'
    assert decision.proposed_identity['provenance_urls'] == (
        'https://nova-druzba.si/kontakt',)
    assert decision.proposed_identity['decisive_research_evidence'][0]['identifier'] == '98765432'
    assert 'Davčna številka' in decision.proposed_identity[
        'decisive_research_evidence'][0]['snippet']
    try:
        CanonicalEntityWriter().create_from_verified_identity(decision.proposed_identity)
    except RuntimeError as exc:
        assert 'disabled' in str(exc)
    else:
        raise AssertionError('canonical writer unexpectedly accepted a write')
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_external_entity_proposal_is_durable_and_does_not_enter_discovery(tmp_path):
    source=tmp_path/'canonical.db'; canonical(source)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    repo=JobRepository(tmp_path/'control.db'); repo.initialize()
    repo.create_upload({'upload_id':'external','original_filename':'external.xlsx',
        'relative_path':'uploads/external.xlsx','sha256':'x','size_bytes':1,
        'format':'XLSX','worksheet_name':'Rows','headers':['Podjetje'],
        'rows':[['NOVA DRUŽBA d.o.o.']]})
    job=repo.create_import_enrich_job('external','External',{'company_name':0})
    discovery=Discovery(tmp_path)
    def injected(task, queries):
        return {1:{'results':[{'url':'https://nova-druzba.si/kontakt',
            'title':'NOVA DRUŽBA d.o.o. | Kontakt',
            'snippet':'NOVA DRUŽBA d.o.o. Davčna številka: 98765432',
            'legal_name':'NOVA DRUŽBA d.o.o.','tax_number':'98765432',
            'entity_type':'d.o.o.','country':'SI','official_domain':'nova-druzba.si'}]}}
    adapter=ImportEnrichAdapter(repo,source,discovery_adapter=discovery,
                                identity_results=injected)
    worker=Worker(repo,adapter,worker_id='worker',adapters={'IMPORT_ENRICH':adapter})
    assert worker.run_once()
    task=repo.identity_tasks(job['job_id'])[0]
    assert task['status']=='EXTERNAL_ENTITY_IDENTIFIED'
    assert task['identity_resolution_status']=='RESOLVED_NEW_ENTITY'
    assert task['canonical_persistence_status']=='PENDING_CREATE'
    assert task['canonical_company_id'] is None
    assert task['proposed_identity']['legal_name']=='NOVA DRUŽBA d.o.o.'
    item=repo.job_items(job['job_id'])[0]
    assert item['identity_status']=='RESOLVED_NEW_ENTITY'
    assert item['company_id'] is None and item['match_status']=='UNRESOLVED'
    assert discovery.calls == []
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before


def test_external_identity_requires_legal_identifier_context_and_models_on_demand(tmp_path):
    db = tmp_path/'canonical.db'; canonical(db); index = EnrichmentIndex(db)
    weak = resolve_results(index, normalized(company_name='New Brand'), 'UNRESOLVED', (),
        [{'results':[{'url':'https://new-brand.si','title':'New Brand home'}]}])
    assert weak.status == 'UNRESOLVED'
    unlabeled = resolve_results(index, normalized(company_name='New Brand'), 'UNRESOLVED', (),
        [{'results':[{'url':'https://new-brand.si','title':'New Brand 98765432',
                     'legal_name':'NEW BRAND d.o.o.'}]}])
    assert unlabeled.status == 'UNRESOLVED'
    conflicting = resolve_results(index, normalized(company_name='NEW BRAND d.o.o.'),
        'UNRESOLVED', (), [{'results':[
            {'url':'https://new-brand.si','title':'NEW BRAND d.o.o. tax 98765432',
             'legal_name':'NEW BRAND d.o.o.'},
            {'url':'https://new-brand.si/about','title':'NEW BRAND d.o.o. tax 87654321',
             'legal_name':'NEW BRAND d.o.o.'}]}])
    assert conflicting.status != 'EXTERNAL_ENTITY_IDENTIFIED'
    assert research_allowed('OUTSIDE_BULK_SCOPE', 'ON_DEMAND') is True
    assert research_allowed('OUTSIDE_BULK_SCOPE', 'BULK') is False


def create_job(repository):
    metadata = {'upload_id':'u','original_filename':'rows.xlsx','relative_path':'uploads/u.xlsx',
        'sha256':'x','size_bytes':1,'format':'XLSX','worksheet_name':'Rows',
        'headers':['Ime','Email','Podjetje'],'rows':[
            ['Ana','ana@robotina.com','Robotina'],
            ['Bine','bine@gmail.com','Beta Trading'],
            ['Bine','bine@gmail.com','Beta Trading'],
            ['Cene','cene@gmail.com','/']]}
    repository.create_upload(metadata)
    return repository.create_import_enrich_job('u','Identity',{
        'person_name':0,'email':1,'company_name':2})


class Discovery:
    def __init__(self, root): self.calls=[]; self.worker_id=None; self.root=root
    def run_subset(self, job, ids, callback):
        self.calls.append(tuple(ids))
        path=self.root/'identity-results.sqlite3'; conn=sqlite3.connect(path)
        conn.executescript('''CREATE TABLE discovery_runs(run_id TEXT,status TEXT,
          engine_version TEXT,rule_version TEXT);
          CREATE TABLE discovery_run_companies(run_id TEXT,company_id INTEGER,status TEXT,
          selected_attempt_id TEXT);
          CREATE TABLE discovery_company_results(result_id TEXT,run_id TEXT,attempt_id TEXT,
          company_id INTEGER,website_status TEXT,official_website TEXT,
          website_observation_id TEXT,website_evidence_ids_json TEXT,
          default_email_contact_id TEXT,default_phone_contact_id TEXT,
          contact_outcome TEXT,completed_at TEXT);
          CREATE TABLE discovery_contacts(contact_id TEXT,run_id TEXT,attempt_id TEXT,
          company_id INTEGER,contact_type TEXT,normalized_value TEXT,
          primary_observation_id TEXT,supporting_observation_ids_json TEXT,
          attribution_status TEXT,rule_version TEXT,roles_json TEXT);
          INSERT INTO discovery_runs VALUES ('run','COMPLETED','engine','rules');''')
        for company_id in ids:
            conn.execute('INSERT INTO discovery_run_companies VALUES (?,?,?,?)',
                         ('run',company_id,'COMPLETED',f'a{company_id}'))
            conn.execute('INSERT INTO discovery_company_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (f'r{company_id}','run',f'a{company_id}',company_id,'HIGH',
                 f'https://company-{company_id}.si/',f'o{company_id}','["e"]',
                 f'm{company_id}',None,'FOUND','2026-01-01T00:00:00+00:00'))
            conn.execute('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (f'm{company_id}','run',f'a{company_id}',company_id,'EMAIL',
                 f'info@company-{company_id}.si',f'om{company_id}','["om"]',
                 'ATTRIBUTED','rules','["GENERAL"]'))
        conn.commit(); conn.close(); callback({'COMPLETED':len(ids)})
        return {'status':'COMPLETED'},[{'company_id':value,'website_status':'HIGH'} for value in ids],path,'run'


def test_durable_tasks_deduplicate_rows_plan_without_paid_calls_and_resume(tmp_path):
    source=tmp_path/'canonical.db'; canonical(source); before=hashlib.sha256(source.read_bytes()).hexdigest()
    repo=JobRepository(tmp_path/'control.db'); repo.initialize(); job=create_job(repo)
    discovery=Discovery(tmp_path)
    injected_calls=[]
    def injected(task, queries):
        injected_calls.append(task['task_id'])
        if task['normalized_input']['normalized_name']=='beta trading':
            return {1:{'results':[{'url':'https://beta.si/','title':'BETA d.o.o.',
                                   'snippet':'Davčna številka 55555555'}]}}
        return {}
    adapter=ImportEnrichAdapter(repo,source,discovery_adapter=discovery,
                                identity_results=injected)
    worker=Worker(repo,adapter,worker_id='worker',adapters={'IMPORT_ENRICH':adapter})
    assert worker.run_once()
    tasks=repo.identity_tasks(job['job_id'])
    assert len(tasks)==2  # duplicate unresolved registrations share one task
    beta=next(task for task in tasks if task['canonical_company_id']==5)
    assert beta['status']=='SEARCH_RESOLVED'
    items=repo.job_items(job['job_id'])
    assert items[1]['identity_task_id']==items[2]['identity_task_id']==beta['task_id']
    assert items[1]['initial_match_status'] in ('AMBIGUOUS','UNRESOLVED')
    assert items[1]['match_status']=='MATCHED' and items[1]['company_id']==5
    assert next(task for task in tasks if task['status']=='NOT_ELIGIBLE')
    result=repo.get_job(job['job_id'])
    assert (result['import_matched_count'], result['import_ambiguous_count'],
            result['import_unresolved_count']) == (3, 0, 1)
    assert result['import_identity_task_count']==2
    assert result['import_identity_local_resolved_count']==0
    assert result['import_identity_actual_query_count']==0
    assert result['import_identity_actual_provider_request_count']==0
    assert result['identity_estimated_cost'] is None and result['identity_actual_cost'] is None
    assert discovery.calls == [(2,5)]  # unresolved tasks never enter Discovery
    assert len(injected_calls)==1
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before

    # Re-running the stage reuses tasks/plans rather than duplicating them.
    from control_room.identity_adapter import IdentityResolutionAdapter
    conn=sqlite3.connect(repo.path)
    conn.execute("UPDATE control_jobs SET status='RUNNING' WHERE job_id=?",(job['job_id'],))
    conn.commit(); conn.close()
    IdentityResolutionAdapter(repo,EnrichmentIndex(source)).run(job['job_id'],'worker')
    assert len(repo.identity_tasks(job['job_id']))==2
    assert sum(len(repo.identity_queries(task['task_id'])) for task in repo.identity_tasks(job['job_id'])) <= 2
    assert len(injected_calls)==1


def test_interrupted_query_recovery_marks_uncertain_billing_without_calling_provider(tmp_path):
    source=tmp_path/'canonical.db'; canonical(source)
    repo=JobRepository(tmp_path/'control.db'); repo.initialize()
    metadata={'upload_id':'u','original_filename':'one.xlsx','relative_path':'uploads/one.xlsx',
        'sha256':'x','size_bytes':1,'format':'XLSX','worksheet_name':'Rows',
        'headers':['Ime','Podjetje'],'rows':[['Ana','Unknown Trading']]}
    repo.create_upload(metadata)
    job=repo.create_import_enrich_job('u','Recovery',{'person_name':0,'company_name':1})
    adapter=ImportEnrichAdapter(repo,source)
    worker=Worker(repo,adapter,worker_id='worker',adapters={'IMPORT_ENRICH':adapter})
    assert worker.run_once()
    task=repo.identity_tasks(job['job_id'])[0]
    query=repo.identity_queries(task['task_id'])[0]
    conn=sqlite3.connect(repo.path)
    conn.execute("UPDATE control_jobs SET status='RUNNING',worker_id='worker' WHERE job_id=?",
                 (job['job_id'],))
    conn.commit(); conn.close()
    repo.start_identity_query(query['query_id'],'future-provider','worker')
    repo.recover_identity_queries(job['job_id'],'worker')
    recovered=repo.identity_queries(task['task_id'])[0]
    assert recovered['status']=='INTERRUPTED'
    assert recovered['uncertain_billing']==1
    assert recovered['logical_call_count']==recovered['provider_request_count']==0
