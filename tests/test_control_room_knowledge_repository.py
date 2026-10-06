"""Read-only accepted knowledge and Import reuse tests."""
import hashlib
import json
import sqlite3

import pytest

from control_room.knowledge_repository import KnowledgeRepository


def lite(path):
    conn=sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE companies_lite(id INTEGER PRIMARY KEY,company_name TEXT,
        registration_number TEXT,tax_number TEXT,address TEXT,municipality TEXT,
        revenue_2025 REAL,profit_2025 REAL,employees_2025 REAL);
      INSERT INTO companies_lite VALUES
        (1,'ALFA d.o.o.','1000001','11111111','Alfa 1','Kranj',10,2,3),
        (2,'BETA d.o.o.','1000002','22222222','Beta 2','Celje',20,4,5),
        (3,'GAMA d.o.o.','1000003','33333333','Gama 3','Koper',30,6,7);
      CREATE TABLE website_discovery(id INTEGER PRIMARY KEY,company_id INTEGER,
        website TEXT,confidence INTEGER,status TEXT,checked_at TEXT,discovered_at TEXT,
        rule_version TEXT,relationship TEXT,verified_scope TEXT);
      INSERT INTO website_discovery VALUES
        (1,1,'https://alfa.si/',95,'VERIFIED','2026-01-01',NULL,'sparrow','LEGAL_ENTITY','https://alfa.si/'),
        (2,2,'https://directory.example/beta',95,'VERIFIED','2026-01-01',NULL,'sparrow','THIRD_PARTY','https://directory.example/beta'),
        (3,3,'https://gama.si/',40,'REVIEW','2026-01-01',NULL,'sparrow','UNRESOLVED','');
      CREATE TABLE email_discovery(id INTEGER PRIMARY KEY,company_id INTEGER,email TEXT,
        confidence INTEGER,website TEXT,page_url TEXT,checked_at TEXT,discovered_at TEXT,
        evidence_json TEXT);
      INSERT INTO email_discovery VALUES
        (1,1,'info@alfa.si',80,'https://alfa.si/','https://alfa.si/contact','2026-01-01',NULL,'{}'),
        (2,1,'alfa@gmail.com',90,'https://alfa.si/','https://alfa.si/contact','2026-01-01',NULL,'{}'),
        (3,1,'vendor@alfa.si',90,'https://alfa.si/','https://bizi.si/alfa','2026-01-01',NULL,'{}'),
        (4,2,'info@beta.si',90,'https://beta.si/','https://beta.si/','2026-01-01',NULL,'{}');
    '''); conn.commit(); conn.close()


def discovery(path, *, run='run', completed='2026-02-01T00:00:00+00:00',
              company=2, status='HIGH', website='https://beta.si/', email='info@beta.si',
              phone='+38611234567', company_status='COMPLETED'):
    conn=sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE discovery_runs(run_id TEXT,status TEXT,engine_version TEXT,rule_version TEXT);
      CREATE TABLE discovery_run_companies(run_id TEXT,company_id INTEGER,status TEXT,
        selected_attempt_id TEXT);
      CREATE TABLE discovery_company_results(result_id TEXT,run_id TEXT,attempt_id TEXT,
        company_id INTEGER,website_status TEXT,official_website TEXT,
        website_observation_id TEXT,website_evidence_ids_json TEXT,
        default_email_contact_id TEXT,default_phone_contact_id TEXT,contact_outcome TEXT,
        completed_at TEXT);
      CREATE TABLE discovery_contacts(contact_id TEXT,run_id TEXT,attempt_id TEXT,
        company_id INTEGER,contact_type TEXT,normalized_value TEXT,
        primary_observation_id TEXT,supporting_observation_ids_json TEXT,
        attribution_status TEXT,rule_version TEXT,roles_json TEXT);
      CREATE TABLE discovery_observations(company_id INTEGER,observation_type TEXT,
        normalized_value TEXT);
    ''')
    attempt='a'+run; result='r'+run
    conn.execute('INSERT INTO discovery_runs VALUES (?,?,?,?)',(run,'RUNNING','engine','rules'))
    conn.execute('INSERT INTO discovery_run_companies VALUES (?,?,?,?)',
                 (run,company,company_status,attempt if company_status in ('COMPLETED','PARTIAL') else None))
    if company_status in ('COMPLETED','PARTIAL'):
        email_id='e'+run if email else None; phone_id='p'+run if phone else None
        conn.execute('INSERT INTO discovery_company_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
          (result,run,attempt,company,status,website if status in ('VERIFIED','HIGH','MEDIUM') else None,
           'wo','["we"]',email_id,phone_id,'FOUND',completed))
        if email:
            conn.execute('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
              (email_id,run,attempt,company,'EMAIL',email,'eo','["eo"]','ATTRIBUTED','rules','["GENERAL"]'))
        if phone:
            conn.execute('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
              (phone_id,run,attempt,company,'PHONE',phone,'po','["po"]','ATTRIBUTED','rules','["GENERAL"]'))
    conn.commit(); return conn


def test_lite_fields_strict_sparrow_and_contact_policy(tmp_path):
    source=tmp_path/'lite.db'; lite(source); before=hashlib.sha256(source.read_bytes()).hexdigest()
    repo=KnowledgeRepository(source)
    alfa=repo.snapshot(1)
    assert alfa.identity['company_name']=='ALFA d.o.o.'
    assert alfa.financials=={'revenue_2025':10.0,'profit_2025':2.0,
        'employees_2025':3.0,'assets_2025':None,'capital_2025':None}
    assert alfa.website.value=='https://alfa.si/'
    assert alfa.default_email.value=='info@alfa.si'
    assert alfa.default_email.provenance[0]['role']=='UNKNOWN'
    assert repo.snapshot(2).website is None
    assert repo.snapshot(3).website is None
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before


def test_discovery_acceptance_contacts_and_review_exclusion(tmp_path):
    source=tmp_path/'lite.db'; lite(source)
    accepted=tmp_path/'accepted.sqlite3'; discovery(accepted).close()
    review=tmp_path/'review.sqlite3'; discovery(review,run='review',company=3,
        status='REVIEW',website='https://gama.si/',email='info@gama.si').close()
    repo=KnowledgeRepository(source,[accepted,review])
    beta=repo.snapshot(2)
    assert beta.website.value=='https://beta.si/' and beta.website.status=='HIGH'
    assert beta.default_email.value=='info@beta.si'
    assert beta.default_phone.value=='+38611234567'
    assert beta.default_email.provenance[0]['status']=='ATTRIBUTED'
    assert repo.snapshot(3).website is None and repo.snapshot(3).default_email is None


def test_unselected_candidate_or_rejected_contacts_are_not_promoted(tmp_path):
    source=tmp_path/'lite.db'; lite(source)
    results=tmp_path/'contacts.sqlite3'
    conn=discovery(results,email=None,phone=None)
    conn.executemany('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',[
      ('candidate','run','arun',2,'EMAIL','candidate@beta.si','x','["x"]',
       'CANDIDATE','rules','["GENERAL"]'),
      ('rejected','run','arun',2,'PHONE','+38619999999','y','["y"]',
       'REJECTED','rules','["GENERAL"]')])
    conn.commit(); conn.close()
    snapshot=KnowledgeRepository(source,[results]).snapshot(2)
    assert snapshot.default_email is None and snapshot.default_phone is None


def test_multiple_sources_field_precedence_review_fallback_conflicts_and_dedup(tmp_path):
    source=tmp_path/'lite.db'; lite(source)
    older=tmp_path/'older.sqlite3'; discovery(older,run='old',completed='2026-01-01T00:00:00+00:00').close()
    newer=tmp_path/'newer.sqlite3'; discovery(newer,run='new',completed='2026-03-01T00:00:00+00:00',
        website='https://beta-new.si/',email='new@beta-new.si',phone=None).close()
    latest=KnowledgeRepository(source,[older,newer]).snapshot(2)
    assert latest.website.value=='https://beta-new.si/'
    assert latest.default_phone.value=='+38611234567'  # missing newer fact does not erase it
    assert latest.website.conflicts[0]['conflicting_value']=='https://beta.si/'
    ordered=KnowledgeRepository(source,[older,newer],precedence='input-order').snapshot(2)
    assert ordered.website.value=='https://beta.si/'
    duplicate=tmp_path/'duplicate.sqlite3'; discovery(duplicate,run='dup',
        completed='2026-04-01T00:00:00+00:00').close()
    merged=KnowledgeRepository(source,[older,duplicate]).snapshot(2)
    assert merged.default_email.value=='info@beta.si'
    assert len(merged.default_email.provenance)==2
    review=tmp_path/'new-review.sqlite3'; discovery(review,run='rev',
        completed='2026-05-01T00:00:00+00:00',status='REVIEW').close()
    assert KnowledgeRepository(source,[older,review]).snapshot(2).website.value=='https://beta.si/'


def test_missing_path_no_fallback_and_inventory_counts_accepted_only(tmp_path):
    source=tmp_path/'lite.db'; lite(source)
    with pytest.raises(FileNotFoundError): KnowledgeRepository(tmp_path/'missing.db')
    with pytest.raises(FileNotFoundError): KnowledgeRepository(source,[tmp_path/'missing.sqlite3'])
    report=KnowledgeRepository(source).inventory()['coverage']
    assert report['companies']==3
    assert report['accepted_websites']==1
    assert report['accepted_emails']==1
    assert report['sufficient_for_import']==1
    assert report.get('accepted_phones',0)==0


def test_live_writing_ignores_running_then_reads_later_completed_result(tmp_path):
    source=tmp_path/'lite.db'; lite(source)
    results=tmp_path/'live.sqlite3'
    writer=discovery(results,run='live',company_status='RUNNING')
    writer.execute('PRAGMA journal_mode=WAL')
    assert KnowledgeRepository(source,[results]).snapshot(2).website is None
    attempt='alive'
    writer.execute("UPDATE discovery_run_companies SET status='COMPLETED',selected_attempt_id=?",(attempt,))
    writer.execute('INSERT INTO discovery_company_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
      ('rlive','live',attempt,2,'HIGH','https://beta.si/','wo','["we"]',None,None,
       'FOUND','2026-02-01T00:00:00+00:00'))
    writer.commit()
    assert KnowledgeRepository(source,[results]).snapshot(2).website.value=='https://beta.si/'
    writer.close()


def test_requested_missing_and_staleness_interface(tmp_path):
    source=tmp_path/'lite.db'; lite(source)
    repo=KnowledgeRepository(source,stale_policy=lambda fact: fact['value'].endswith('.si/'))
    assert repo.missing(1)==('default_phone',)
    assert repo.snapshot(1).stale_fields==('website',)
