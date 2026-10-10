"""Read-only Control Room DATA company explorer tests."""
import hashlib
import sqlite3

import pytest

from control_room.app import create_app


def canonical_fixture(path):
    conn=sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE companies_lite(id INTEGER PRIMARY KEY,company_name TEXT,
        registration_number TEXT,tax_number TEXT,address TEXT,municipality TEXT,
        revenue_2025 REAL,profit_2025 REAL,employees_2025 REAL,
        assets_2025 REAL,capital_2025 REAL,collected_at TEXT);
      INSERT INTO companies_lite VALUES
        (1,'ALFA d.o.o.','1000001','11111111','Alfa 1','Kranj',1000000,200000,30,1500000,100000,'2026-01-01'),
        (2,'BETA INDUSTRIJA d.o.o.','1000002','22222222','Beta 2','Celje',2500000,400000,55,3000000,250000,'2026-01-02'),
        (3,'GAMA STORITVE d.o.o.','1000003','33333333','Gama 3','Koper',NULL,NULL,NULL,NULL,NULL,'2026-01-03');
      CREATE TABLE website_discovery(id INTEGER PRIMARY KEY,company_id INTEGER,
        website TEXT,confidence INTEGER,status TEXT,checked_at TEXT,discovered_at TEXT,
        rule_version TEXT,relationship TEXT,verified_scope TEXT);
      INSERT INTO website_discovery VALUES
        (1,1,'https://alfa.si/',95,'VERIFIED','2026-01-01',NULL,'sparrow','LEGAL_ENTITY','https://alfa.si/');
      CREATE TABLE email_discovery(id INTEGER PRIMARY KEY,company_id INTEGER,email TEXT,
        confidence INTEGER,website TEXT,page_url TEXT,checked_at TEXT,discovered_at TEXT,
        evidence_json TEXT);
      INSERT INTO email_discovery VALUES
        (1,1,'info@alfa.si',90,'https://alfa.si/','https://alfa.si/contact','2026-01-01',NULL,'{}');
    ''')
    conn.commit(); conn.close()


def discovery_fixture(path, run, completed, website, email, phone):
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
    attempt=f'a-{run}'; result=f'r-{run}'
    email_id=f'e-{run}' if email else None; phone_id=f'p-{run}' if phone else None
    conn.execute('INSERT INTO discovery_runs VALUES (?,?,?,?)',(run,'COMPLETED','engine','rules'))
    conn.execute('INSERT INTO discovery_run_companies VALUES (?,?,?,?)',(run,2,'COMPLETED',attempt))
    conn.execute('INSERT INTO discovery_company_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
      (result,run,attempt,2,'HIGH',website,'website-observation','["website-evidence"]',
       email_id,phone_id,'FOUND',completed))
    if email:
        conn.execute('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
          (email_id,run,attempt,2,'EMAIL',email,'email-observation','["email-evidence"]',
           'ATTRIBUTED','rules','["GENERAL"]'))
    if phone:
        conn.execute('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
          (phone_id,run,attempt,2,'PHONE',phone,'phone-observation','["phone-evidence"]',
           'ATTRIBUTED','rules','["GENERAL"]'))
    conn.commit(); conn.close()


@pytest.fixture
def data_app(tmp_path, monkeypatch):
    canonical=tmp_path/'canonical.sqlite3'; canonical_fixture(canonical)
    older=tmp_path/'older.sqlite3'
    discovery_fixture(older,'older','2026-02-01T00:00:00+00:00',
                      'https://beta.si/','old@beta.si','+38611111111')
    newer=tmp_path/'newer.sqlite3'
    discovery_fixture(newer,'newer','2026-03-01T00:00:00+00:00',
                      'https://beta-new.si/','new@beta-new.si','+38612222222')
    monkeypatch.setattr('requests.sessions.Session.request',
                        lambda *_args, **_kwargs: pytest.fail('network call'))
    app=create_app({'TESTING':True,'CONTROL_DB':tmp_path/'control.sqlite3',
        'CONTROL_STORAGE_ROOT':tmp_path,'CANONICAL_DB':canonical,
        'DISCOVERY_V2_PATH':tmp_path/'missing-discovery',
        'KNOWLEDGE_DISCOVERY_RESULTS':(older,newer),'DATA_PAGE_SIZE':2})
    return app,canonical,older,newer


def test_empty_data_page_is_bounded_and_paginated(data_app):
    app,*_=data_app; html=app.test_client().get('/data').get_data(as_text=True)
    assert '3 matches' in html and 'Page 1 / 2' in html
    assert 'ALFA d.o.o.' in html and 'BETA INDUSTRIJA d.o.o.' in html
    assert 'GAMA STORITVE d.o.o.' not in html
    assert 'page=2' in html


@pytest.mark.parametrize(('query','expected'),[
    ('gama','GAMA STORITVE d.o.o.'),
    ('22222222','BETA INDUSTRIJA d.o.o.'),
    ('1000001','ALFA d.o.o.'),
    ('2','BETA INDUSTRIJA d.o.o.'),
])
def test_data_searches_supported_identity_fields(data_app,query,expected):
    app,*_=data_app
    html=app.test_client().get('/data',query_string={'q':query}).get_data(as_text=True)
    assert expected in html and '1 match' in html


def test_data_pagination_preserves_query(data_app):
    app,*_=data_app
    response=app.test_client().get('/data',query_string={'q':'a','page':2})
    html=response.get_data(as_text=True)
    assert response.status_code==200 and 'GAMA STORITVE d.o.o.' in html
    assert 'q=a' in html and 'page=1' in html


def test_company_detail_formats_accepted_facts_quality_and_provenance(data_app):
    app,*_=data_app; response=app.test_client().get('/data/company/2')
    html=response.get_data(as_text=True)
    assert response.status_code==200
    for value in ('BETA INDUSTRIJA d.o.o.','2,500,000','400,000','55','3,000,000',
                  '250,000','https://beta-new.si/','new@beta-new.si',
                  'old@beta.si','+38612222222','+38611111111'):
        assert value in html
    assert 'DEFAULT' in html and 'CONFLICTS' in html and 'https://beta.si/' in html
    assert 'DISCOVERY_V2' in html and 'run newer' in html and 'CANONICAL_LITE' in html


def test_missing_knowledge_and_unknown_company_are_safe(data_app):
    app,*_=data_app; client=app.test_client()
    html=client.get('/data/company/3').get_data(as_text=True)
    assert html.count('UNKNOWN') >= 5
    assert 'WEBSITE, DEFAULT EMAIL, DEFAULT PHONE' in html
    assert client.get('/data/company/999999').status_code==404
    assert client.get('/data/company/not-an-id').status_code==404


def test_data_requests_do_not_write_any_database(data_app):
    app,canonical,older,newer=data_app; control=app.config['CONTROL_DB']
    paths=(canonical,older,newer,control)
    before={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    client=app.test_client()
    assert client.get('/data').status_code==200
    assert client.get('/data/company/2').status_code==200
    after={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    assert after==before
