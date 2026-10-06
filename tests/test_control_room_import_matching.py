"""Deterministic, no-network Import & Enrich matching foundation tests."""
import hashlib
import json
import sqlite3

from control_room.enrichment_index import EnrichmentIndex
from control_room.matching import ImportMatcher
from control_room.uploads import normalize_import_row, suggest_import_mapping


def canonical(path):
    conn = sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE companies_lite(
        id INTEGER PRIMARY KEY, company_name TEXT, registration_number TEXT,
        tax_number TEXT, address TEXT, municipality TEXT, revenue_2025 REAL,
        profit_2025 REAL, employees_2025 REAL, assets_2025 REAL,
        capital_2025 REAL, gvin_company_id TEXT, gvin_detail_url TEXT,
        financial_status TEXT, collected_at TEXT);
      INSERT INTO companies_lite VALUES
        (1,'ALFA d.o.o.','1000001','11111111','Alfa 1','Kranj',10,1,2,20,5,'g1','u1','COMPLETE','now'),
        (2,'BETA d.o.o.','1000002','22222222','Beta 2','Ljubljana',20,2,3,30,6,'g2','u2','COMPLETE','now'),
        (3,'DVOJNIK d.o.o.','1000003','33333333','Prva 3','Celje',30,3,4,40,7,'g3','u3','COMPLETE','now'),
        (4,'DVOJNIK d.o.o.','1000004','44444444','Druga 4','Maribor',40,4,5,50,8,'g4','u4','COMPLETE','now'),
        (5,'KOVINARSTVO HORVAT d.o.o.','1000005','55555555','Peta 5','Ptuj',50,5,6,60,9,'g5','u5','COMPLETE','now'),
        (6,'PUBLIC d.o.o.','1000006','66666666','Sesta 6','Koper',60,6,7,70,10,'g6','u6','COMPLETE','now'),
        (7,'ANDREJA d.o.o.','1000007','77777777','Sedma 7','Kranj',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
        (8,'ZAK d.o.o.','1000008','88888888','Osma 8','Kranj',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
        (9,'PSE d.o.o.','1000009','99999999','Deveta 9','Kranj',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
        (10,'OMEGA AIR d.o.o. Ljubljana','1000010','10101010','Deseta 10','Ljubljana',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
        (11,'REBOLJ, d.o.o. Medvode','1000011','11110000','Enajsta 11','Medvode',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
        (12,'IRBIS d.o.o.','1000012','12121212','Dvanajsta 12','Trnovo',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
        (13,'AERO 4 M d.o.o.','1000013','13131313','Trinajsta 13','Kranj',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
        (14,'TURK M & Z d.o.o.','1000014','14141414','Stirinajsta 14','Kranj',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
        (15,'MOJA ČOKOLADA, prodaja čokolad, d.o.o.','1000015','15151515','Petnajsta 15','Kranj',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
        (16,'VIZUALNE KOMUNIKACIJE, Andraž Jenkole s.p.','1000016','16161616','Sestnajsta 16','Kranj',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
        (17,'OBMOČNA OBRTNO-PODJETNIŠKA ZBORNICA SEŽANA','1000017','17171717','Sedemnajsta 17','Sežana',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
      CREATE TABLE website_discovery(
        id INTEGER PRIMARY KEY,company_id INTEGER,website TEXT,status TEXT,
        verified_scope TEXT,relationship TEXT);
      INSERT INTO website_discovery VALUES
        (1,1,'https://alfa.si/','VERIFIED','https://alfa.si/','LEGAL_ENTITY'),
        (2,2,'https://beta.example/','REVIEW',NULL,'AMBIGUOUS');
      CREATE TABLE email_discovery(
        id INTEGER PRIMARY KEY,company_id INTEGER,email TEXT,website TEXT);
      INSERT INTO email_discovery VALUES
        (1,1,'info@alfa.si','https://alfa.si/'),
        (2,2,'info@beta.example','https://beta.example/');
    ''')
    conn.commit()
    conn.close()


def discovery(path):
    conn = sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE discovery_runs(
        run_id TEXT PRIMARY KEY,status TEXT,engine_version TEXT,rule_version TEXT);
      CREATE TABLE discovery_run_companies(
        run_id TEXT,company_id INTEGER,status TEXT,selected_attempt_id TEXT);
      CREATE TABLE discovery_company_results(
        result_id TEXT,run_id TEXT,attempt_id TEXT,company_id INTEGER,
        website_status TEXT,official_website TEXT,website_observation_id TEXT,
        website_evidence_ids_json TEXT,default_email_contact_id TEXT,
        default_phone_contact_id TEXT,contact_outcome TEXT,completed_at TEXT);
      CREATE TABLE discovery_contacts(
        contact_id TEXT,run_id TEXT,attempt_id TEXT,company_id INTEGER,
        contact_type TEXT,normalized_value TEXT,primary_observation_id TEXT,
        supporting_observation_ids_json TEXT,attribution_status TEXT,
        rule_version TEXT,roles_json TEXT);
      INSERT INTO discovery_runs VALUES ('run','COMPLETED','engine','rules');
      INSERT INTO discovery_run_companies VALUES
        ('run',4,'COMPLETED','a4'),('run',6,'COMPLETED','a6'),
        ('run',2,'COMPLETED','a2');
      INSERT INTO discovery_company_results VALUES
        ('r4','run','a4',4,'HIGH','https://delta.si/','o4','["e4"]','mail4','phone4','FOUND','2026-01-01T00:00:00+00:00'),
        ('r6','run','a6',6,'HIGH','https://public.si/','o6','["e6"]','mail6',NULL,'FOUND','2026-01-01T00:00:01+00:00'),
        ('r2','run','a2',2,'REVIEW',NULL,NULL,'[]','mail2',NULL,'FOUND','2026-01-01T00:00:02+00:00');
      INSERT INTO discovery_contacts VALUES
        ('mail4','run','a4',4,'EMAIL','registrations@external-mail.eu','om4','["om4"]','ATTRIBUTED','contact-rules','["GENERAL"]'),
        ('phone4','run','a4',4,'PHONE','+38621234567','op4','["op4"]','ATTRIBUTED','contact-rules','["MAIN"]'),
        ('mail6','run','a6',6,'EMAIL','public.company@gmail.com','om6','["om6"]','ATTRIBUTED','contact-rules','["GENERAL"]'),
        ('mail2','run','a2',2,'EMAIL','review-only@beta.example','om2','["om2"]','ATTRIBUTED','contact-rules','["GENERAL"]');
    ''')
    conn.commit()
    conn.close()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def row(**values):
    return {
        'person_name': '', 'email': '', 'phone': '', 'company_name': '',
        'tax_number': '', 'registration_number': '', 'address': '',
        'municipality': '', **values,
    }


def test_registration_headers_and_normalized_view_preserve_source_values():
    headers = ['Ime', 'E-pošta', 'Telefon', 'Podjetje', 'Davčna številka',
               'Matična številka', 'Source']
    values = [' Ana Č. ', 'ANA@ALFA.SI', '041 123 456', 'ALFA, d.o.o.',
              'SI 11111111', '1 000 001', 'Webinar']
    mapping = suggest_import_mapping(headers)
    assert mapping['person_name'] == 0
    assert mapping['email'] == 1
    assert mapping['phone'] == 2
    assert mapping['company_name'] == 3
    normalized = normalize_import_row(headers, values, mapping, row_key=17)
    assert values[0] == ' Ana Č. '  # matching does not mutate the caller's row
    assert normalized['person_name'] == ' Ana Č. '
    assert normalized['normalized_person_name'] == 'ana c'
    assert normalized['normalized_email'] == 'ana@alfa.si'
    assert normalized['normalized_phone'] == '38641123456'
    assert normalized['normalized_name'] == 'alfa'
    assert normalized['normalized_tax_number'] == '11111111'
    assert normalized['normalized_registration_number'] == '1000001'
    assert normalized['row_key'] == 17


def test_index_is_read_only_and_reuses_only_strict_accepted_data(tmp_path):
    lite = tmp_path / 'lite.db'
    results = tmp_path / 'results.sqlite3'
    canonical(lite)
    discovery(results)
    before = digest(lite), digest(results)
    index = EnrichmentIndex(lite, [results])
    assert len(index.companies) == 17
    assert index.company(1)['profit_2025'] == 1
    assert index.company(1)['assets_2025'] == 20
    assert index.enrichment[1]['website'] == 'https://alfa.si/'
    assert index.enrichment[2]['website'] is None
    assert 'beta.example' not in index.domains
    assert index.enrichment[4]['website_status'] == 'HIGH'
    assert index.emails['registrations@external-mail.eu'] == {4}
    assert 'external-mail.eu' not in index.domains
    assert 'gmail.com' not in index.domains
    assert 'public.company@gmail.com' not in index.emails
    assert before == (digest(lite), digest(results))


def test_decisive_and_name_rules_with_routing(tmp_path):
    lite = tmp_path / 'lite.db'; results = tmp_path / 'results.sqlite3'
    canonical(lite); discovery(results)
    matcher = ImportMatcher(EnrichmentIndex(lite, [results]))

    tax = matcher.match(row(tax_number='SI 11111111'), row_key=1)
    assert (tax.outcome, tax.company_id, tax.match_method) == ('MATCHED', 1, 'TAX_EXACT')
    assert tax.route_hint == 'RESOLVED_WITHOUT_AI'
    assert tax.ai_eligibility == 'NOT_EVALUATED'
    registration = matcher.match(row(registration_number='1 000 001'), row_key=3)
    assert (registration.outcome, registration.company_id,
            registration.match_method) == ('MATCHED', 1, 'REGISTRATION_EXACT')
    beta = matcher.match(row(company_name='Beta d.o.o.'), row_key=2)
    assert (beta.outcome, beta.company_id, beta.match_method) == ('MATCHED', 2, 'NAME_EXACT')
    assert beta.route_hint == 'MATCHED_REQUIRES_ENRICHMENT'
    assert any(item.kind == 'NAME_EXACT' for item in beta.evidence)


def test_name_location_and_accepted_domain_disambiguate_duplicates(tmp_path):
    lite = tmp_path / 'lite.db'; results = tmp_path / 'results.sqlite3'
    canonical(lite); discovery(results)
    matcher = ImportMatcher(EnrichmentIndex(lite, [results]))

    located = matcher.match(row(company_name='DVOJNIK', municipality='Celje'))
    assert (located.outcome, located.company_id, located.match_method) == (
        'MATCHED', 3, 'NAME_LOCATION')
    domain = matcher.match(row(company_name='DVOJNIK', email='ana@delta.si'))
    assert (domain.outcome, domain.company_id, domain.match_method) == (
        'MATCHED', 4, 'NAME_DOMAIN')
    external = matcher.match(row(company_name='DVOJNIK',
                                 email='registrations@external-mail.eu'))
    assert external.company_id == 4
    assert {item.kind for item in external.evidence} >= {'NAME_EXACT', 'EMAIL_EXACT'}
    unrelated_external = matcher.match(row(company_name='DVOJNIK',
                                         email='someone-else@external-mail.eu'))
    assert unrelated_external.outcome == 'AMBIGUOUS'
    assert unrelated_external.company_id is None


def test_weak_or_public_signals_never_auto_match(tmp_path):
    lite = tmp_path / 'lite.db'; results = tmp_path / 'results.sqlite3'
    canonical(lite); discovery(results)
    matcher = ImportMatcher(EnrichmentIndex(lite, [results]))

    assert matcher.match(row(email='someone@gmail.com')).outcome == 'UNRESOLVED'
    assert matcher.match(row(phone='+386 2 123 45 67')).outcome == 'UNRESOLVED'
    assert matcher.match(row(address='Alfa 1')).outcome == 'UNRESOLVED'
    assert matcher.match(row(person_name='ALFA')).outcome == 'UNRESOLVED'
    fuzzy = matcher.match(row(company_name='KOVINARSTVO HORVA'))
    assert fuzzy.outcome == 'AMBIGUOUS'
    assert fuzzy.company_id is None
    assert any(item.kind == 'FUZZY_NAME' for item in fuzzy.evidence)


def test_exact_name_safety_guards_short_single_token_and_diacritics(tmp_path):
    lite = tmp_path / 'lite.db'; canonical(lite)
    matcher = ImportMatcher(EnrichmentIndex(lite))

    person_like = matcher.match(row(company_name='andreja'))
    assert person_like.outcome == 'AMBIGUOUS'
    assert person_like.conflicts == ('EXACT_NAME_REQUIRES_CORROBORATION',)
    folded_only = matcher.match(row(company_name='ŽAK d.o.o.'))
    assert folded_only.outcome == 'AMBIGUOUS'
    assert folded_only.conflicts == ('EXACT_NAME_REQUIRES_CORROBORATION',)
    explicit_short_legal_name = matcher.match(row(company_name='PSE d.o.o.'))
    assert (explicit_short_legal_name.outcome, explicit_short_legal_name.company_id,
            explicit_short_legal_name.match_method) == ('MATCHED', 9, 'NAME_EXACT')


def test_controlled_structural_aliases_authorize_only_unique_strong_shapes(tmp_path):
    lite = tmp_path / 'lite.db'; canonical(lite)
    matcher = ImportMatcher(EnrichmentIndex(lite))

    expected = {
        'Omega Air': (10, 'ALIAS_LEGAL_CORE'),
        'Rebolj d.o.o.': (11, 'ALIAS_LEGAL_CORE'),
        'IRBIS d.o.o., PE Trnovo': (12, 'ALIAS_BRANCH_ALIAS'),
        'Aero4M': (13, 'ALIAS_COMPACT_FULL_NAME'),
        'Turk mz': (14, 'ALIAS_COMPACT_FULL_NAME'),
        'Moja Čokolada doo': (15, 'ALIAS_LEADING_CLAUSE'),
        'Andraz Jenkole sp': (16, 'ALIAS_PROPRIETOR'),
    }
    for submitted, (company_id, method) in expected.items():
        match = matcher.match(row(company_name=submitted))
        assert (match.outcome, match.company_id, match.match_method) == (
            'MATCHED', company_id, method), submitted
        assert any(item.kind == 'ALIAS_RULE' for item in match.evidence)

    # Expanded-name acronyms are useful candidates, but do not authorize alone.
    acronym = matcher.match(row(company_name='OOZ'))
    assert acronym.outcome == 'AMBIGUOUS'
    assert acronym.company_id is None
    assert any(item.kind == 'CONTROLLED_ALIAS' and item.company_ids == (17,)
               for item in acronym.evidence)


def test_conflicting_strong_evidence_fails_closed(tmp_path):
    lite = tmp_path / 'lite.db'; results = tmp_path / 'results.sqlite3'
    canonical(lite); discovery(results)
    matcher = ImportMatcher(EnrichmentIndex(lite, [results]))
    conflict = matcher.match(row(tax_number='11111111', company_name='BETA d.o.o.'))
    assert conflict.outcome == 'AMBIGUOUS'
    assert conflict.company_id is None
    assert conflict.conflicts == ('IDENTIFIER_CONFLICTS_WITH_IDENTITY_SIGNAL',)
    assert set(conflict.candidate_company_ids) == {1, 2}
    identifiers = matcher.match(row(tax_number='11111111',
                                    registration_number='1000002'))
    assert identifiers.outcome == 'AMBIGUOUS'
    assert identifiers.conflicts == ('CONFLICTING_IDENTIFIERS',)


def test_duplicate_rows_remain_independent_results(tmp_path):
    lite = tmp_path / 'lite.db'; canonical(lite)
    matcher = ImportMatcher(EnrichmentIndex(lite))
    first = matcher.match(row(tax_number='11111111'), row_key=2)
    second = matcher.match(row(tax_number='11111111'), row_key=9)
    assert first is not second
    assert (first.row_key, second.row_key) == (2, 9)
    assert first.company_id == second.company_id == 1
    assert first.as_dict()['route_hint'] == 'RESOLVED_WITHOUT_AI'


def test_missing_configured_database_fails_without_fallback(tmp_path):
    missing = tmp_path / 'production.db'
    try:
        EnrichmentIndex(missing)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError('missing configured database must fail')
    assert not missing.exists()
