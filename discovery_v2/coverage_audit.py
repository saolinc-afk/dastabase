"""Read-only Discovery coverage/failure funnel audit.

The audit never imports a search provider or runner.  It reads one canonical
master database, one final/current Discovery v2 run, and optional earlier v2
runs.  Reports are written only to an explicit output directory.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from discovery.domain_generator import normalize_domain
from discovery.domain_policy import (COMPANY_DATABASE, DIRECTORY, REGISTRY,
                                     policy_for_url)
from discovery.ownership import email_attribution
from discovery.website_verifier import RULE_VERSION as LEGACY_RULE_VERSION
from discovery_v2.store import APPLICATION_ID, SCHEMA_VERSION


USABLE_WEBSITE_STATUSES = frozenset({'VERIFIED', 'HIGH', 'MEDIUM'})
SAMPLE_COHORTS = ('HIGH_POTENTIAL_RECOVERY', 'SEARCH_RECOVERY',
                  'CONTACT_RECOVERY', 'AMBIGUOUS')
OUTPUT_NAMES = ('coverage_companies.csv', 'unresolved_companies.csv',
                'samples.csv', 'snippet_support.csv', 'summary.json')
SNIPPET_HEADERS = ('company_id', 'company_name', 'cohort', 'official_website',
    'unresolved', 'source_label', 'source_path', 'run_id', 'attempt_id',
    'observation_id', 'evidence_id', 'candidate_origin', 'candidate_domain',
    'candidate_url', 'candidate_raw_value', 'identity_match', 'provider',
    'query_type', 'query_text',
    'result_rank', 'result_url', 'result_host', 'source_class', 'publisher_classification',
    'registered_directory_source', 'source_locator', 'extraction_method')

MASTER_REQUIRED = {
    'companies_lite': {'id', 'company_name', 'tax_number', 'registration_number',
                       'address', 'municipality', 'revenue_2025', 'employees_2025'},
    'website_discovery': {'id', 'company_id', 'website', 'status', 'rule_version',
                          'evidence_json', 'ownership_json'},
    'email_discovery': {'id', 'company_id', 'email', 'page_url', 'page_title',
                        'evidence_json'},
}
RESULT_REQUIRED = {
    'discovery_runs': {'run_id', 'status', 'engine_version', 'rule_version'},
    'discovery_run_companies': {'run_id', 'company_id', 'manifest_position',
        'identity_snapshot_json', 'eligibility_status', 'eligibility_reason', 'status',
        'selected_attempt_id'},
    'discovery_attempts': {'attempt_id', 'run_id', 'company_id', 'attempt_number',
        'status', 'diagnostics_json', 'request_counts_json'},
    'discovery_evidence': {'evidence_id', 'run_id', 'attempt_id', 'company_id',
        'source_kind', 'provider', 'query_type', 'query_text', 'result_rank',
        'result_url', 'result_host', 'source_class'},
    'discovery_observations': {'observation_id', 'run_id', 'attempt_id', 'company_id',
        'evidence_id', 'observation_type', 'raw_value', 'normalized_value', 'value_json',
        'extraction_method', 'source_locator'},
    'discovery_contacts': {'contact_id', 'run_id', 'attempt_id', 'company_id',
        'contact_type', 'normalized_value', 'attribution_status'},
    'discovery_company_results': {'result_id', 'run_id', 'attempt_id', 'company_id',
        'website_status', 'official_website', 'default_email_contact_id',
        'default_phone_contact_id', 'contact_outcome'},
}

METRIC_DEFINITIONS = {
    'total_master_companies': 'Distinct rows in canonical companies_lite.',
    'usable_reusable_coverage': ('Master companies with an accepted official website, '
        'attributable default email, or attributable default phone in legacy/master, '
        'the current run, or an explicitly supplied reusable run.'),
    'official_website_selected': ('Master companies with latest legacy status VERIFIED '
        'or a selected-attempt v2 result in VERIFIED/HIGH/MEDIUM, and a nonempty website.'),
    'attributable_email_found': ('Master companies with a v2 attributed default company '
        'email, or a legacy email revalidated by the repository attribution rule against '
        'the latest VERIFIED legacy website and ownership evidence.'),
    'attributable_phone_found': ('Master companies with a v2 attributed default company '
        'phone. Canonical phone fields are not relabelled as Discovery attribution.'),
    'website_without_email': 'Official website selected but no attributable email found.',
    'search_evidence_without_accepted_website': ('At least one persisted SEARCH_RESULT '
        'on a latest v2 attempt, but no accepted official website from any supplied source.'),
    'no_useful_candidate': ('No accepted website and no persisted WEBSITE_CANDIDATE '
        'observation/assessment in any supplied v2 source or legacy candidate website.'),
    'unresolved_failed': 'No accepted website and current manifest status FAILED.',
    'unresolved_ineligible': 'No accepted website and current manifest status INELIGIBLE.',
    'unresolved_other': ('No accepted website and neither current FAILED nor INELIGIBLE; '
        'this overlaps search/candidate diagnostics only through the unresolved population.'),
}

REASON_DEFINITIONS = {
    'INELIGIBLE_BANKRUPTCY': 'Current manifest explicitly records BANKRUPTCY ineligibility.',
    'INELIGIBLE_OTHER': 'Current manifest is ineligible for another persisted reason.',
    'ATTEMPT_FAILED': 'Current manifest status is FAILED.',
    'ATTEMPT_INTERRUPTED': 'Latest current attempt is INTERRUPTED.',
    'ATTEMPT_RUNNING': 'Current manifest or latest attempt is RUNNING.',
    'NOT_ATTEMPTED': 'Current manifest entry has no attempt yet.',
    'NOT_IN_CURRENT_MANIFEST': 'Company is outside the explicitly selected current run.',
    'DISCOVERY_INCOMPLETE': 'Selected v2 result has contact_outcome=INCOMPLETE.',
    'SEARCH_FAILED': 'At least one persisted query has status FAILED.',
    'SEARCH_BUDGET_EXHAUSTED': 'A persisted query was skipped for exhausted query budget.',
    'SEARCH_NO_RESULTS': 'Executed searches all persisted zero results and no search evidence.',
    'SEARCH_EVIDENCE_NO_ACCEPTED_WEBSITE': 'Search evidence exists but no official website was accepted.',
    'CANDIDATES_NOT_VERIFIED': 'Website candidates exist but none became an accepted website.',
    'NO_USEFUL_CANDIDATE': 'No website candidate is persisted for an unresolved company.',
    'NO_DISCOVERY_EVIDENCE': 'No v2 attempt evidence or legacy discovery row is available.',
    'AMBIGUOUS_IDENTITY': 'Persisted assessment/resolution explicitly records ambiguity or conflict.',
    'OWNERSHIP_EVIDENCE_INSUFFICIENT': 'A candidate remained REVIEW without an ownership/confidence rule.',
    'THIRD_PARTY_OR_DISALLOWED_CANDIDATE': 'A candidate was explicitly marked ineligible/third-party/disallowed.',
    'FETCH_DNS_FAILURE': 'Persisted acquisition diagnostics identify DNS/name-resolution failure.',
    'FETCH_TIMEOUT': 'Persisted acquisition diagnostics identify a timeout.',
    'FETCH_ACCESS_BLOCKED': 'Persisted acquisition diagnostics identify access denial/rate limiting.',
    'FETCH_TLS_FAILURE': 'Persisted acquisition diagnostics identify TLS/certificate failure.',
    'FETCH_REDIRECT_OUT_OF_SCOPE': 'Persisted acquisition diagnostics identify an out-of-scope redirect.',
    'FETCH_HTTP_FAILURE': 'Persisted acquisition diagnostics identify another HTTP failure.',
    'FETCH_NETWORK_OTHER': 'Persisted acquisition diagnostics identify another transport failure.',
    'WEBSITE_NO_ATTRIBUTED_EMAIL': 'An official website exists but no attributable email is selected.',
    'WEBSITE_NO_ATTRIBUTED_PHONE': 'An official website exists but no attributable phone is selected.',
    'EMAIL_CANDIDATE_NOT_ATTRIBUTED': 'Email observations exist but no attributable email is selected.',
    'PHONE_CANDIDATE_NOT_ATTRIBUTED': 'Phone observations exist but no attributable phone is selected.',
    'LEGACY_CANDIDATE_NOT_ACCEPTED': 'Latest legacy website row contains a site but is not VERIFIED.',
    'LEGACY_DISCOVERY_ERROR': 'Latest legacy website result is ERROR.',
    'LEGACY_NOT_FOUND': 'Latest legacy website result is NOT_FOUND.',
    'MALFORMED_STORED_DIAGNOSTICS': 'Persisted optional diagnostic/evidence JSON could not be decoded.',
    'SNIPPET_URL_CANDIDATE': 'A persisted WEBSITE_CANDIDATE was derived from a URL in search title/snippet text.',
    'SNIPPET_EMAIL_DOMAIN_CANDIDATE': 'A persisted WEBSITE_CANDIDATE was derived from an email domain in search title/snippet text.',
    'SNIPPET_DOMAIN_SUPPORT_UNRESOLVED': 'Snippet-derived candidate support exists but no official website is accepted.',
    'REGISTERED_DIRECTORY_SNIPPET_SUPPORT': ('Snippet-derived support came from a result URL classified by the exact '
        'domain-policy registry as DIRECTORY or COMPANY_DATABASE.'),
}

COHORT_DEFINITIONS = {
    'INELIGIBLE': 'Explicit current-run ineligibility; excluded from a recovery pass.',
    'SOLVED': 'Accepted official website and attributable email; phone is an independent dimension.',
    'CONTACT_RECOVERY': 'Accepted official website but no attributable email.',
    'AMBIGUOUS': 'No accepted website and persisted identity/ownership ambiguity.',
    'HIGH_POTENTIAL_RECOVERY': 'No accepted website, but at least one website candidate is persisted.',
    'LOW_SIGNAL_LOW_VALUE': ('No accepted website/candidate, completed zero-result search, '
        'employees <1 and revenue < EUR 100k, with no acquisition/search failure.'),
    'SEARCH_RECOVERY': 'Remaining eligible companies without an accepted official website.',
}


def readonly(path):
    resolved = Path(path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(resolved.as_uri()+'?mode=ro', uri=True,
                           isolation_level=None, timeout=0.2)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return resolved, conn


def columns(conn, table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def require_schema(conn, required, label):
    for table, expected in required.items():
        missing = expected-columns(conn, table)
        if missing:
            raise ValueError(f'{label}: incompatible schema; {table} missing {sorted(missing)}')


def json_object(raw):
    try:
        value = json.loads(raw or '{}')
    except (TypeError, json.JSONDecodeError):
        return {}, False
    return (value, True) if isinstance(value, dict) else ({}, False)


def json_array(raw):
    try:
        value = json.loads(raw or '[]')
    except (TypeError, json.JSONDecodeError):
        return [], False
    return (value, True) if isinstance(value, list) else ([], False)


def number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value


def employee_band(value):
    value = number(value)
    if value is None:
        return 'UNKNOWN'
    if value < 1:
        return '<1'
    if value < 5:
        return '1-4'
    if value < 10:
        return '5-9'
    if value < 50:
        return '10-49'
    if value < 250:
        return '50-249'
    return '250+'


def revenue_band(value):
    value = number(value)
    if value is None:
        return 'UNKNOWN'
    if value <= 0:
        return '<=0'
    if value < 100_000:
        return '<100k'
    if value < 1_000_000:
        return '100k-999k'
    if value < 10_000_000:
        return '1m-9.9m'
    if value < 50_000_000:
        return '10m-49.9m'
    return '50m+'


def postal_fields(value):
    match = re.match(r'^\s*(\d{4})\b', value or '')
    return (match.group(1), match.group(1)[0]+'xxx') if match else ('', 'UNKNOWN')


def load_master(path):
    resolved, conn = readonly(path)
    try:
        require_schema(conn, MASTER_REQUIRED, str(resolved))
        company_columns = columns(conn, 'companies_lite')
        activity_column = next((name for name in
            ('registered_activity', 'activity', 'industry', 'skd_code')
            if name in company_columns), None)
        selected = ['id', 'company_name', 'tax_number', 'registration_number',
                    'address', 'municipality', 'revenue_2025', 'employees_2025']
        if activity_column:
            selected.append(f'{activity_column} AS registered_activity')
        companies = {}
        for stored in conn.execute(f'SELECT {",".join(selected)} FROM companies_lite ORDER BY id'):
            row = dict(stored)
            row.setdefault('registered_activity', '')
            row['legacy'] = {'website_row_id': None, 'website': '', 'website_status': '',
                             'rule_version': '', 'website_evidence': [], 'ownership': {},
                             'email': '', 'email_row_id': None, 'malformed': False}
            if row['id'] in companies:
                raise ValueError(f'{resolved}: duplicate canonical company ID {row["id"]}')
            companies[row['id']] = row

        latest = '''SELECT w.* FROM website_discovery w JOIN
            (SELECT company_id,MAX(id) AS id FROM website_discovery GROUP BY company_id) x
            ON x.id=w.id'''
        for stored in conn.execute(latest):
            row = dict(stored)
            if row['company_id'] not in companies:
                continue
            legacy = companies[row['company_id']]['legacy']
            website_evidence, valid_pages = json_array(row.get('evidence_json'))
            ownership, valid_ownership = json_object(row.get('ownership_json'))
            legacy.update(website_row_id=row['id'], website=row.get('website') or '',
                          website_status=row.get('status') or '',
                          rule_version=row.get('rule_version') or '',
                          website_evidence=website_evidence, ownership=ownership,
                          malformed=not (valid_pages and valid_ownership))

        email_query = f'''SELECT e.id,e.company_id,e.email,e.page_url,e.page_title,
            e.evidence_json
            FROM email_discovery e JOIN ({latest}) w ON w.company_id=e.company_id
            WHERE w.status='VERIFIED' AND w.rule_version=? AND COALESCE(w.website,'')<>''
            ORDER BY e.company_id,e.id'''
        email_rows = [dict(row) for row in
                      conn.execute(email_query, (LEGACY_RULE_VERSION,)).fetchall()]
    finally:
        conn.close()
    # Attribution is CPU-only and intentionally runs after the SQLite connection closes.
    for row in email_rows:
        company = companies.get(row['company_id'])
        if not company or company['legacy']['email']:
            continue
        evidence, valid = json_object(row['evidence_json'])
        if not valid:
            company['legacy']['malformed'] = True
            continue
        legacy = company['legacy']
        block = evidence.get('contact_block')
        block = block if isinstance(block, dict) else None
        try:
            attribution = email_attribution(company, row['email'] or '',
                row['page_url'] or '', evidence.get('publication') or '',
                legacy['ownership'], legacy['website_evidence'], row['page_title'] or '',
                block, evidence.get('visible_email') or '')
        except (TypeError, ValueError, KeyError, AttributeError):
            legacy['malformed'] = True
            continue
        if attribution.get('attributable') is True:
            company['legacy'].update(email=row['email'] or '', email_row_id=row['id'])
    return resolved, companies, activity_column


def _error_reasons(errors):
    reasons = set()
    for error in errors:
        text = str(error).casefold()
        if any(value in text for value in ('name resolution', 'failed to resolve',
                'nodename nor servname', 'no address associated')):
            reasons.add('FETCH_DNS_FAILURE')
        if any(value in text for value in ('timeout', 'timed out')):
            reasons.add('FETCH_TIMEOUT')
        if any(value in text for value in ('access denied', 'rate limited', 'http 403', 'robots')):
            reasons.add('FETCH_ACCESS_BLOCKED')
        if any(value in text for value in ('ssl', 'tls', 'certificate')):
            reasons.add('FETCH_TLS_FAILURE')
        if 'redirect outside accepted site' in text:
            reasons.add('FETCH_REDIRECT_OUT_OF_SCOPE')
        if re.search(r'\bhttp\s+[45]\d\d\b', text):
            reasons.add('FETCH_HTTP_FAILURE')
        if any(value in text for value in ('connectionerror', 'connection reset',
                'connection refused', 'proxyerror')) and not reasons & {
                    'FETCH_DNS_FAILURE', 'FETCH_TIMEOUT', 'FETCH_TLS_FAILURE'}:
            reasons.add('FETCH_NETWORK_OTHER')
    return reasons


def _diagnostic_summary(raw):
    diagnostics, valid = json_object(raw)
    result = {'reasons': set(), 'queries_executed': 0, 'queries_all_zero': False,
              'candidate_assessments': 0}
    if not valid:
        result['reasons'].add('MALFORMED_STORED_DIAGNOSTICS')
        return result
    queries = diagnostics.get('queries')
    queries = queries if isinstance(queries, list) else []
    executed = [q for q in queries if isinstance(q, dict) and q.get('status') == 'COMPLETED']
    result['queries_executed'] = len(executed)
    result['queries_all_zero'] = bool(executed) and all(q.get('result_count') == 0 for q in executed)
    if any(isinstance(q, dict) and q.get('status') == 'FAILED' for q in queries):
        result['reasons'].add('SEARCH_FAILED')
    if any(isinstance(q, dict) and q.get('reason') == 'Search query budget exhausted'
           for q in queries):
        result['reasons'].add('SEARCH_BUDGET_EXHAUSTED')

    errors = diagnostics.get('fetch_errors')
    errors = errors if isinstance(errors, list) else []
    completeness = diagnostics.get('completeness')
    if isinstance(completeness, dict) and isinstance(completeness.get('errors'), list):
        errors += completeness['errors']
    result['reasons'].update(_error_reasons(errors))

    resolution = diagnostics.get('search_evidence_resolution')
    if isinstance(resolution, dict):
        status = str(resolution.get('status') or '').upper()
        if 'AMBIGU' in status or 'CONFLICT' in status:
            result['reasons'].add('AMBIGUOUS_IDENTITY')
    candidates = diagnostics.get('website_candidates')
    candidates = candidates if isinstance(candidates, list) else []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        result['candidate_assessments'] += 1
        status = str(candidate.get('status') or '').upper()
        reason = str(candidate.get('reason') or '').casefold()
        assessment = candidate.get('assessment')
        assessment = assessment if isinstance(assessment, dict) else {}
        p1a = assessment.get('p1a')
        p1a = p1a if isinstance(p1a, dict) else {}
        relationship = str(assessment.get('relationship') or
                           p1a.get('relationship') or '').upper()
        if relationship == 'AMBIGUOUS':
            result['reasons'].add('AMBIGUOUS_IDENTITY')
        if status == 'INELIGIBLE' or 'third-party' in reason or 'disallowed' in reason:
            result['reasons'].add('THIRD_PARTY_OR_DISALLOWED_CANDIDATE')
        if (status in ('REVIEW', 'GROUP_REVIEW') and not p1a.get('rule_id')
                and not p1a.get('confidence_rule_id')):
            result['reasons'].add('OWNERSHIP_EVIDENCE_INSUFFICIENT')
    return result


def _latest_attempt_features(conn, run_id):
    query = '''WITH latest AS (
      SELECT a.* FROM discovery_attempts a WHERE a.run_id=? AND a.attempt_number=(
        SELECT MAX(x.attempt_number) FROM discovery_attempts x
        WHERE x.run_id=a.run_id AND x.company_id=a.company_id))
      SELECT a.company_id,a.attempt_id,a.status,a.diagnostics_json,a.request_counts_json,
        SUM(CASE WHEN e.source_kind='SEARCH_RESULT' THEN 1 ELSE 0 END) search_evidence_count,
        SUM(CASE WHEN e.source_kind='FETCH_FAILURE' THEN 1 ELSE 0 END) fetch_failure_count
      FROM latest a LEFT JOIN discovery_evidence e ON e.run_id=a.run_id
        AND e.company_id=a.company_id AND e.attempt_id=a.attempt_id
      GROUP BY a.company_id,a.attempt_id,a.status,a.diagnostics_json,a.request_counts_json'''
    features = {}
    for stored in conn.execute(query, (run_id,)).fetchall():
        row = dict(stored)
        diagnostic = _diagnostic_summary(row['diagnostics_json'])
        features[row['company_id']] = {
            'attempt_id': row['attempt_id'], 'attempt_status': row['status'],
            'search_evidence_count': row['search_evidence_count'] or 0,
            'fetch_failure_count': row['fetch_failure_count'] or 0,
            'candidate_count': diagnostic['candidate_assessments'],
            'search_candidate_count': 0, 'email_candidate_count': 0,
            'phone_candidate_count': 0, 'snippet_support': [],
            'diagnostic': diagnostic,
        }

    observation_query = '''WITH latest AS (
      SELECT a.* FROM discovery_attempts a WHERE a.run_id=? AND a.attempt_number=(
        SELECT MAX(x.attempt_number) FROM discovery_attempts x
        WHERE x.run_id=a.run_id AND x.company_id=a.company_id))
      SELECT a.company_id,
       SUM(CASE WHEN o.observation_type='WEBSITE_CANDIDATE' THEN 1 ELSE 0 END) candidate_count,
       SUM(CASE WHEN o.observation_type='WEBSITE_CANDIDATE'
                 AND e.source_kind='SEARCH_RESULT' THEN 1 ELSE 0 END) search_candidate_count,
       SUM(CASE WHEN o.observation_type='EMAIL_CANDIDATE' THEN 1 ELSE 0 END) email_candidate_count,
       SUM(CASE WHEN o.observation_type='PHONE_CANDIDATE' THEN 1 ELSE 0 END) phone_candidate_count
      FROM latest a LEFT JOIN discovery_observations o ON o.run_id=a.run_id
        AND o.company_id=a.company_id AND o.attempt_id=a.attempt_id
      LEFT JOIN discovery_evidence e ON e.evidence_id=o.evidence_id AND e.run_id=o.run_id
        AND e.company_id=o.company_id AND e.attempt_id=o.attempt_id
      GROUP BY a.company_id'''
    for stored in conn.execute(observation_query, (run_id,)):
        feature = features[stored['company_id']]
        for name in ('candidate_count', 'search_candidate_count',
                     'email_candidate_count', 'phone_candidate_count'):
            feature[name] = max(feature[name], stored[name] or 0)

    snippet_query = '''WITH latest AS (
      SELECT a.* FROM discovery_attempts a WHERE a.run_id=? AND a.attempt_number=(
        SELECT MAX(x.attempt_number) FROM discovery_attempts x
        WHERE x.run_id=a.run_id AND x.company_id=a.company_id))
      SELECT a.company_id,a.attempt_id,o.observation_id,o.evidence_id,
        o.raw_value AS candidate_raw_value,o.normalized_value,o.value_json,
        o.extraction_method,o.source_locator,
        e.provider,e.query_type,e.query_text,e.result_rank,e.result_url,e.result_host,
        e.source_class
      FROM latest a JOIN discovery_observations o ON o.run_id=a.run_id
        AND o.company_id=a.company_id AND o.attempt_id=a.attempt_id
      JOIN discovery_evidence e ON e.evidence_id=o.evidence_id AND e.run_id=o.run_id
        AND e.company_id=o.company_id AND e.attempt_id=o.attempt_id
      WHERE o.observation_type='WEBSITE_CANDIDATE'
        AND e.source_kind='SEARCH_RESULT'
        AND o.extraction_method IN ('snippet_url','email_domain')
      ORDER BY a.company_id,o.rowid'''
    for stored in conn.execute(snippet_query, (run_id,)).fetchall():
        row = dict(stored)
        value, valid = json_object(row.pop('value_json'))
        origin = ('SNIPPET_URL' if row['extraction_method'] == 'snippet_url'
                  else 'SNIPPET_EMAIL_DOMAIN')
        # Newer artifacts duplicate this contract in candidate_origin; older v3
        # artifacts retain the authoritative extraction_method only.
        persisted_origin = value.get('candidate_origin')
        origin_mismatch = (persisted_origin in ('SNIPPET_URL', 'SNIPPET_EMAIL_DOMAIN')
                           and persisted_origin != origin)
        publisher = row.get('result_url') or row.get('result_host') or ''
        policy = policy_for_url(publisher) if normalize_domain(publisher) else None
        classification = policy.classification if policy else ''
        row.update(candidate_origin=origin,
                   candidate_domain=normalize_domain(row['normalized_value']),
                   candidate_url=row.pop('normalized_value'),
                   identity_match=value.get('identity_match') is True,
                   publisher_classification=classification,
                   registered_directory_source=(
                       classification in (DIRECTORY, COMPANY_DATABASE)))
        feature = features[row['company_id']]
        feature['snippet_support'].append(row)
        if not valid or origin_mismatch:
            feature['diagnostic']['reasons'].add('MALFORMED_STORED_DIAGNOSTICS')
    return features


def load_v2(path, run_id, label, role):
    resolved, conn = readonly(path)
    try:
        if (conn.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or
                conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
            raise ValueError(f'{resolved}: not a compatible Discovery v2 result database')
        require_schema(conn, RESULT_REQUIRED, str(resolved))
        run = conn.execute('''SELECT run_id,status,engine_version,rule_version
            FROM discovery_runs WHERE run_id=?''', (run_id,)).fetchone()
        if run is None:
            raise ValueError(f'{resolved}: unknown requested run ID {run_id}')

        attempt_statuses = Counter(row[0] for row in conn.execute(
            'SELECT status FROM discovery_attempts WHERE run_id=?', (run_id,)))
        latest = _latest_attempt_features(conn, run_id)
        records = {}
        for stored in conn.execute('''SELECT company_id,manifest_position,identity_snapshot_json,
            eligibility_status,eligibility_reason,status,selected_attempt_id
            FROM discovery_run_companies
            WHERE run_id=? ORDER BY manifest_position''', (run_id,)):
            row = dict(stored)
            if row['company_id'] in records:
                raise ValueError(f'{resolved}: duplicate run-company row {row["company_id"]}')
            identity, valid_identity = json_object(row.pop('identity_snapshot_json'))
            if not valid_identity or identity.get('id') != row['company_id']:
                raise ValueError(f'{resolved}: malformed/mismatched frozen identity for company {row["company_id"]}')
            records[row['company_id']] = {
                **row, 'source_label': label, 'source_path': str(resolved), 'run_id': run_id,
                'role': role, 'identity': identity, 'result': None,
                'latest': latest.get(row['company_id'], {'attempt_id': '', 'attempt_status': '',
                    'search_evidence_count': 0, 'fetch_failure_count': 0,
                    'candidate_count': 0, 'search_candidate_count': 0,
                    'email_candidate_count': 0, 'phone_candidate_count': 0,
                    'snippet_support': [],
                    'diagnostic': {'reasons': set(), 'queries_executed': 0,
                                   'queries_all_zero': False}}),
            }

        result_query = '''SELECT rc.company_id,r.result_id,r.attempt_id,r.website_status,
          r.official_website,r.contact_outcome,r.default_email_contact_id,
          r.default_phone_contact_id,
          ec.contact_id email_contact_id,ec.contact_type email_type,
          ec.normalized_value email_value,ec.attribution_status email_attribution,
          pc.contact_id phone_contact_id,pc.contact_type phone_type,
          pc.normalized_value phone_value,pc.attribution_status phone_attribution
          FROM discovery_run_companies rc JOIN discovery_company_results r
            ON r.run_id=rc.run_id AND r.company_id=rc.company_id
            AND r.attempt_id=rc.selected_attempt_id
          LEFT JOIN discovery_contacts ec ON ec.contact_id=r.default_email_contact_id
            AND ec.run_id=r.run_id AND ec.company_id=r.company_id AND ec.attempt_id=r.attempt_id
          LEFT JOIN discovery_contacts pc ON pc.contact_id=r.default_phone_contact_id
            AND pc.run_id=r.run_id AND pc.company_id=r.company_id AND pc.attempt_id=r.attempt_id
          WHERE rc.run_id=?'''
        seen_results = set()
        for stored in conn.execute(result_query, (run_id,)):
            row = dict(stored)
            company_id = row['company_id']
            if company_id in seen_results or company_id not in records:
                raise ValueError(f'{resolved}: duplicate/orphan selected result for company {company_id}')
            seen_results.add(company_id)
            for prefix, expected in (('email', 'EMAIL'), ('phone', 'PHONE')):
                if (row[f'default_{prefix}_contact_id'] is not None
                        and row[f'{prefix}_contact_id'] is None):
                    raise ValueError(f'{resolved}: missing selected default {prefix} '
                                     f'for company {company_id}')
                if row[f'{prefix}_contact_id'] is not None and (
                        row[f'{prefix}_type'] != expected or
                        row[f'{prefix}_attribution'] != 'ATTRIBUTED'):
                    raise ValueError(f'{resolved}: invalid selected default {prefix} for company {company_id}')
            usable = row['website_status'] in USABLE_WEBSITE_STATUSES
            if usable != bool(row['official_website']):
                raise ValueError(f'{resolved}: inconsistent selected website for company {company_id}')
            records[company_id]['result'] = row

        for company_id, record in records.items():
            if record['status'] in ('COMPLETED', 'PARTIAL') and record['result'] is None:
                raise ValueError(f'{resolved}: terminal company lacks selected result {company_id}')

        return {
            'path': str(resolved), 'label': label, 'role': role, 'run_id': run_id,
            'run_status': run['status'], 'engine_version': run['engine_version'],
            'rule_version': run['rule_version'], 'records': records,
            'company_statuses': dict(sorted(Counter(
                record['status'] for record in records.values()).items())),
            'attempt_statuses': dict(sorted(attempt_statuses.items())),
            'latest_attempt_statuses': dict(sorted(Counter(
                record['latest']['attempt_status'] for record in records.values()
                if record['latest']['attempt_status']).items())),
        }
    finally:
        conn.close()


def validate_master_identities(companies, sources):
    """Reject coincidental numeric-ID joins across canonical/source namespaces."""
    for source in sources:
        for company_id, record in source['records'].items():
            company = companies.get(company_id)
            if company is None:
                continue
            identity = record['identity']
            for field in ('tax_number', 'registration_number'):
                frozen = str(identity.get(field) or '').strip()
                canonical = str(company.get(field) or '').strip()
                if frozen and canonical and frozen != canonical:
                    raise ValueError(f'{source["path"]}: frozen {field} does not match '
                                     f'canonical company {company_id}')


def _accepted_v2(record):
    result = record.get('result')
    if not result:
        return {'website': '', 'email': '', 'phone': ''}
    website = (result['official_website'] or '') if (
        result['website_status'] in USABLE_WEBSITE_STATUSES) else ''
    return {'website': website, 'email': result['email_value'] or '',
            'phone': result['phone_value'] or ''}


def _source_reference(record):
    result = record.get('result') or {}
    latest = record['latest']
    return ':'.join((record['source_label'], record['run_id'],
                     str(result.get('attempt_id') or latest.get('attempt_id') or ''),
                     str(result.get('result_id') or '')))


def _company_row(company, current, reusable):
    legacy = company['legacy']
    sources = ([current] if current else []) + list(reusable)
    v2_records = [source['records'][company['id']] for source in sources
                  if company['id'] in source['records']]
    current_record = (current['records'].get(company['id']) if current else None)

    legacy_website = (legacy['website'] if legacy['website_status'] == 'VERIFIED'
                      and legacy['website'] else '')
    accepted = [(_accepted_v2(record), record) for record in v2_records]
    website, website_source = legacy_website, 'MASTER_LEGACY' if legacy_website else ''
    email, email_source = legacy['email'], 'MASTER_LEGACY' if legacy['email'] else ''
    phone = phone_source = ''
    # Current v2 has precedence for presentation; coverage itself is a union.
    for values, record in reversed(accepted):
        if values['website']:
            website, website_source = values['website'], record['source_label']
        if values['email']:
            email, email_source = values['email'], record['source_label']
        if values['phone']:
            phone, phone_source = values['phone'], record['source_label']

    candidate_count = sum(record['latest']['candidate_count'] for record in v2_records)
    search_candidates = sum(record['latest']['search_candidate_count'] for record in v2_records)
    search_evidence = sum(record['latest']['search_evidence_count'] for record in v2_records)
    email_candidates = sum(record['latest']['email_candidate_count'] for record in v2_records)
    phone_candidates = sum(record['latest']['phone_candidate_count'] for record in v2_records)
    snippet_support = [item for record in v2_records
                       for item in record['latest']['snippet_support']]
    snippet_url_count = sum(item['candidate_origin'] == 'SNIPPET_URL'
                            for item in snippet_support)
    snippet_email_domain_count = sum(item['candidate_origin'] ==
                                     'SNIPPET_EMAIL_DOMAIN' for item in snippet_support)
    snippet_domains = {item['candidate_domain'] for item in snippet_support
                       if item['candidate_domain']}
    snippet_identity_count = sum(item['identity_match'] for item in snippet_support)
    snippet_directory_count = sum(item['registered_directory_source']
                                  for item in snippet_support)
    legacy_candidate = bool(legacy['website'] and not legacy_website)
    if legacy_candidate:
        candidate_count += 1

    reasons = set()
    for record in v2_records:
        reasons.update(record['latest']['diagnostic']['reasons'])
        result = record.get('result')
        if result and result['contact_outcome'] == 'INCOMPLETE':
            reasons.add('DISCOVERY_INCOMPLETE')
    if current_record:
        status = current_record['status']
        latest_status = current_record['latest']['attempt_status']
        if status == 'INELIGIBLE':
            reasons.add('INELIGIBLE_BANKRUPTCY' if
                current_record['eligibility_reason'] == 'BANKRUPTCY' else 'INELIGIBLE_OTHER')
        if status == 'FAILED':
            reasons.add('ATTEMPT_FAILED')
        if latest_status == 'INTERRUPTED':
            reasons.add('ATTEMPT_INTERRUPTED')
        if status == 'RUNNING' or latest_status == 'RUNNING':
            reasons.add('ATTEMPT_RUNNING')
        if not latest_status and status == 'PENDING':
            reasons.add('NOT_ATTEMPTED')
    else:
        reasons.add('NOT_IN_CURRENT_MANIFEST')

    if not website:
        if search_evidence:
            reasons.add('SEARCH_EVIDENCE_NO_ACCEPTED_WEBSITE')
        if candidate_count:
            reasons.add('CANDIDATES_NOT_VERIFIED')
        else:
            reasons.add('NO_USEFUL_CANDIDATE')
        if any(record['latest']['diagnostic']['queries_all_zero']
               and not record['latest']['search_evidence_count'] for record in v2_records):
            reasons.add('SEARCH_NO_RESULTS')
        if not v2_records and not legacy['website_row_id']:
            reasons.add('NO_DISCOVERY_EVIDENCE')
    else:
        if not email:
            reasons.add('WEBSITE_NO_ATTRIBUTED_EMAIL')
        if not phone:
            reasons.add('WEBSITE_NO_ATTRIBUTED_PHONE')
        if not email and email_candidates:
            reasons.add('EMAIL_CANDIDATE_NOT_ATTRIBUTED')
        if not phone and phone_candidates:
            reasons.add('PHONE_CANDIDATE_NOT_ATTRIBUTED')

    if legacy_candidate:
        reasons.add('LEGACY_CANDIDATE_NOT_ACCEPTED')
    if legacy['website_status'] == 'ERROR':
        reasons.add('LEGACY_DISCOVERY_ERROR')
    if legacy['website_status'] == 'NOT_FOUND':
        reasons.add('LEGACY_NOT_FOUND')
    if legacy['malformed']:
        reasons.add('MALFORMED_STORED_DIAGNOSTICS')
    if snippet_url_count:
        reasons.add('SNIPPET_URL_CANDIDATE')
    if snippet_email_domain_count:
        reasons.add('SNIPPET_EMAIL_DOMAIN_CANDIDATE')
    if snippet_directory_count:
        reasons.add('REGISTERED_DIRECTORY_SNIPPET_SUPPORT')
    if snippet_support and not website:
        reasons.add('SNIPPET_DOMAIN_SUPPORT_UNRESOLVED')

    employee = number(company['employees_2025'])
    revenue = number(company['revenue_2025'])
    failure_reasons = {'SEARCH_FAILED', 'FETCH_DNS_FAILURE', 'FETCH_TIMEOUT',
        'FETCH_ACCESS_BLOCKED', 'FETCH_TLS_FAILURE', 'FETCH_HTTP_FAILURE',
        'FETCH_NETWORK_OTHER', 'FETCH_REDIRECT_OUT_OF_SCOPE'}
    if current_record and current_record['status'] == 'INELIGIBLE':
        cohort = 'INELIGIBLE'
    elif website and email:
        cohort = 'SOLVED'
    elif website:
        cohort = 'CONTACT_RECOVERY'
    elif 'AMBIGUOUS_IDENTITY' in reasons:
        cohort = 'AMBIGUOUS'
    elif candidate_count:
        cohort = 'HIGH_POTENTIAL_RECOVERY'
    elif ('SEARCH_NO_RESULTS' in reasons and employee is not None and employee < 1
          and revenue is not None and revenue < 100_000
          and not reasons & failure_reasons):
        cohort = 'LOW_SIGNAL_LOW_VALUE'
    else:
        cohort = 'SEARCH_RECOVERY'

    postal_code, postal_prefix = postal_fields(company['municipality'])
    references = sorted(_source_reference(record) for record in v2_records)
    coverage_sources = sorted({value for value in
        (website_source, email_source, phone_source) if value})
    current_result = (current_record or {}).get('result') or {}
    return {
        'company_id': company['id'], 'company_name': company['company_name'] or '',
        'tax_number': company['tax_number'] or '',
        'registration_number': company['registration_number'] or '',
        'address': company['address'] or '', 'municipality': company['municipality'] or '',
        'postal_code': postal_code, 'postal_prefix': postal_prefix,
        'employees_2025': company['employees_2025'],
        'employee_band': employee_band(company['employees_2025']),
        'revenue_2025': company['revenue_2025'],
        'revenue_band': revenue_band(company['revenue_2025']),
        'registered_activity': company.get('registered_activity') or '',
        'cohort': cohort, 'diagnostic_reasons': '|'.join(sorted(reasons)),
        'usable_coverage': 'YES' if website or email or phone else 'NO',
        'official_website': website, 'website_source': website_source,
        'attributable_email': email, 'email_source': email_source,
        'attributable_phone': phone, 'phone_source': phone_source,
        'legacy_website_status': legacy['website_status'],
        'legacy_website_row_id': legacy['website_row_id'] or '',
        'current_company_status': current_record['status'] if current_record else '',
        'current_attempt_status': (current_record['latest']['attempt_status']
                                   if current_record else ''),
        'current_eligibility_reason': (current_record['eligibility_reason']
                                       if current_record else ''),
        'current_website_status': current_result.get('website_status') or '',
        'current_contact_outcome': current_result.get('contact_outcome') or '',
        'search_evidence_count': search_evidence,
        'website_candidate_count': candidate_count,
        'search_website_candidate_count': search_candidates,
        'email_candidate_count': email_candidates,
        'phone_candidate_count': phone_candidates,
        'snippet_url_candidate_count': snippet_url_count,
        'snippet_email_domain_candidate_count': snippet_email_domain_count,
        'snippet_supported_domain_count': len(snippet_domains),
        'identity_matched_snippet_support_count': snippet_identity_count,
        'registered_directory_snippet_support_count': snippet_directory_count,
        'unresolved_snippet_domain_support': ('YES' if snippet_support and not website
                                              else 'NO'),
        'unresolved_identity_matched_snippet_support': (
            'YES' if snippet_identity_count and not website else 'NO'),
        'snippet_support_evidence_ids': '|'.join(sorted({item['evidence_id']
            for item in snippet_support})),
        'snippet_support_observation_ids': '|'.join(sorted({item['observation_id']
            for item in snippet_support})),
        'discovery_references': '|'.join(references),
        'coverage_sources': '|'.join(coverage_sources),
    }


def deterministic_sample(rows, cohorts=SAMPLE_COHORTS, sample_size=25, seed='20261008'):
    selected = []
    for cohort in cohorts:
        choices = [row for row in rows if row['cohort'] == cohort]
        choices.sort(key=lambda row: (hashlib.sha256(
            f'{seed}:{cohort}:{row["company_id"]}'.encode()).hexdigest(), row['company_id']))
        for position, row in enumerate(choices[:sample_size], 1):
            selected.append({**row, 'sample_cohort': cohort,
                             'sample_position': position, 'sample_seed': seed})
    return selected


def _segments(rows):
    result = {}
    for cohort in sorted(COHORT_DEFINITIONS):
        subset = [row for row in rows if row['cohort'] == cohort]
        result[cohort] = {
            'total': len(subset),
            'employee_bands': dict(sorted(Counter(row['employee_band'] for row in subset).items())),
            'revenue_bands': dict(sorted(Counter(row['revenue_band'] for row in subset).items())),
            'postal_prefixes': dict(sorted(Counter(row['postal_prefix'] for row in subset).items())),
            'registered_activity': dict(sorted(Counter(
                row['registered_activity'] or 'UNAVAILABLE' for row in subset).items())),
        }
    return result


def _snippet_summary(rows, snippet_rows):
    cross_tab = {}
    for cohort in sorted(COHORT_DEFINITIONS):
        subset = [row for row in rows if row['cohort'] == cohort]
        cross_tab[cohort] = {
            'total_companies': len(subset),
            'companies_with_snippet_support': sum(
                bool(row['snippet_supported_domain_count']) for row in subset),
            'companies_with_snippet_url': sum(
                bool(row['snippet_url_candidate_count']) for row in subset),
            'companies_with_snippet_email_domain': sum(
                bool(row['snippet_email_domain_candidate_count']) for row in subset),
            'companies_with_identity_matched_snippet_support': sum(
                bool(row['identity_matched_snippet_support_count']) for row in subset),
            'companies_with_registered_directory_snippet_support': sum(
                bool(row['registered_directory_snippet_support_count']) for row in subset),
        }
    registry = {domain: {'classification': policy.classification, 'policy': policy.policy}
                for domain, policy in sorted(REGISTRY.items())}
    return {
        'observations': len(snippet_rows),
        'companies_with_snippet_support': sum(
            bool(row['snippet_supported_domain_count']) for row in rows),
        'unresolved_companies_with_snippet_support': sum(
            row['unresolved_snippet_domain_support'] == 'YES' for row in rows),
        'unresolved_companies_with_identity_matched_snippet_support': sum(
            row['unresolved_identity_matched_snippet_support'] == 'YES' for row in rows),
        'companies_with_snippet_url': sum(
            bool(row['snippet_url_candidate_count']) for row in rows),
        'companies_with_snippet_email_domain': sum(
            bool(row['snippet_email_domain_candidate_count']) for row in rows),
        'companies_with_registered_directory_snippet_support': sum(
            bool(row['registered_directory_snippet_support_count']) for row in rows),
        'origin_observations': dict(sorted(Counter(
            row['candidate_origin'] for row in snippet_rows).items())),
        'publisher_classifications': dict(sorted(Counter(
            row['publisher_classification'] or 'UNREGISTERED'
            for row in snippet_rows).items())),
        'cohort_cross_tab': cross_tab,
        'publisher_registry_sha256': hashlib.sha256(json.dumps(
            registry, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'interpretation': ('Supporting candidate evidence only. These dimensions never '
                           'select an official website or make a company SOLVED.'),
    }


def build_audit(master, current_results, current_run_id, reusable=(),
                sample_size=25, seed='20261008', sample_cohorts=SAMPLE_COHORTS):
    master_path, companies, activity_column = load_master(master)
    if not companies:
        raise ValueError(f'{master_path}: canonical master population is empty')
    current = load_v2(current_results, current_run_id, 'CURRENT_V2', 'CURRENT')
    reusable_sources = []
    seen = {(current['path'], current['run_id'])}
    for index, (path, run_id) in enumerate(reusable, 1):
        source = load_v2(path, run_id, f'REUSABLE_V2_{index}', 'REUSABLE')
        key = source['path'], source['run_id']
        if key in seen:
            raise ValueError(f'Duplicate Discovery source/run supplied: {key}')
        seen.add(key)
        reusable_sources.append(source)

    validate_master_identities(companies, [current, *reusable_sources])

    rows = [_company_row(company, current, reusable_sources)
            for company in companies.values()]
    master_ids = set(companies)
    outside = sorted({company_id for source in [current, *reusable_sources]
                      for company_id in source['records'] if company_id not in master_ids})
    funnel = {
        'total_master_companies': len(rows),
        'usable_reusable_coverage': sum(row['usable_coverage'] == 'YES' for row in rows),
        'official_website_selected': sum(bool(row['official_website']) for row in rows),
        'attributable_email_found': sum(bool(row['attributable_email']) for row in rows),
        'attributable_phone_found': sum(bool(row['attributable_phone']) for row in rows),
        'website_without_email': sum(bool(row['official_website']) and
                                     not row['attributable_email'] for row in rows),
        'search_evidence_without_accepted_website': sum(
            bool(row['search_evidence_count']) and not row['official_website'] for row in rows),
        'no_useful_candidate': sum(not row['official_website'] and
                                   not row['website_candidate_count'] for row in rows),
        'unresolved_failed': sum(not row['official_website'] and
                                 row['current_company_status'] == 'FAILED' for row in rows),
        'unresolved_ineligible': sum(not row['official_website'] and
                                     row['current_company_status'] == 'INELIGIBLE' for row in rows),
    }
    funnel['unresolved_other'] = sum(not row['official_website'] and
        row['current_company_status'] not in ('FAILED', 'INELIGIBLE') for row in rows)
    row_by_id = {row['company_id']: row for row in rows}
    snippet_rows = []
    for source in [current, *reusable_sources]:
        for company_id, record in source['records'].items():
            if company_id not in master_ids:
                continue
            company_row = row_by_id[company_id]
            for support in record['latest']['snippet_support']:
                snippet_rows.append({
                    'company_id': company_id,
                    'company_name': company_row['company_name'],
                    'cohort': company_row['cohort'],
                    'official_website': company_row['official_website'],
                    'unresolved': 'NO' if company_row['official_website'] else 'YES',
                    'source_label': source['label'], 'source_path': source['path'],
                    'run_id': source['run_id'], **support,
                })
    snippet_rows.sort(key=lambda row: (row['company_id'], row['source_label'],
                                      row['candidate_domain'], row['observation_id']))

    summary = {
        'audit_version': 2,
        'inputs': {'master': str(master_path), 'current_results': current['path'],
                   'current_run_id': current_run_id,
                   'reusable_results': [{'path': source['path'], 'run_id': source['run_id']}
                                        for source in reusable_sources]},
        'funnel': funnel,
        'current_run': {key: current[key] for key in ('run_id', 'run_status',
            'engine_version', 'rule_version', 'company_statuses', 'attempt_statuses',
            'latest_attempt_statuses')},
        'cohorts': dict(sorted(Counter(row['cohort'] for row in rows).items())),
        'diagnostic_reasons': dict(sorted(Counter(reason for row in rows
            for reason in row['diagnostic_reasons'].split('|') if reason).items())),
        'commercial_segments': _segments(rows),
        'snippet_support': _snippet_summary(rows, snippet_rows),
        'master_boundary': {'outside_master_result_company_count': len(outside),
                            'outside_master_result_company_ids': outside},
        'availability': {'registered_activity_column': activity_column,
                         'legacy_phone_counted_as_attributable': False},
        'definitions': {'metrics': METRIC_DEFINITIONS, 'diagnostic_reasons': REASON_DEFINITIONS,
                        'cohorts': COHORT_DEFINITIONS,
                        'status_semantics': {
                            'COMPLETED': ('Current company has an atomically selected result and '
                                'contact acquisition was not marked incomplete; website may still be REVIEW/NOT_FOUND.'),
                            'PARTIAL': ('Current company has a selected useful result, but search/transport '
                                'diagnostics made contact_outcome INCOMPLETE and the company is retryable.'),
                            'FAILED': 'Latest company processing raised an exception; evidence/attempt history remains.',
                            'INELIGIBLE': 'Frozen legal identity matched explicit bankruptcy eligibility exclusion; no attempt.',
                            'PENDING': 'Not yet processed or returned to pending after interruption.',
                            'RUNNING': 'An attempt is currently active.',
                            'INTERRUPTED': ('Attempt-history status only; interrupted work returns the company '
                                'to PENDING and a resume creates another attempt.'),
                        }},
        'sampling': {'seed': str(seed), 'sample_size_per_cohort': sample_size,
                     'cohorts': list(sample_cohorts)},
    }
    samples = deterministic_sample(rows, sample_cohorts, sample_size, str(seed))
    return rows, summary, samples, snippet_rows


def _safe(value):
    text = '' if value is None else str(value)
    return "'"+text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def _atomic_csv(path, rows, headers, overwrite):
    if path.exists() and not overwrite:
        raise ValueError(f'Output already exists (use --overwrite): {path}')
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction='ignore')
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _safe(row.get(key, '')) for key in headers})
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_json(path, value, overwrite):
    if path.exists() and not overwrite:
        raise ValueError(f'Output already exists (use --overwrite): {path}')
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_reports(output_dir, rows, summary, samples, snippet_rows, inputs,
                  overwrite=False):
    output_dir = Path(output_dir).expanduser().absolute()
    resolved_inputs = {Path(value).expanduser().resolve() for value in inputs}
    if output_dir.resolve() in resolved_inputs:
        raise ValueError('Output directory cannot be a SQLite input')
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {name: output_dir/name for name in OUTPUT_NAMES}
    if any(path.resolve() in resolved_inputs for path in paths.values()):
        raise ValueError('Output report cannot overwrite a SQLite input')
    if not overwrite:
        existing = [str(path) for path in paths.values() if path.exists()]
        if existing:
            raise ValueError(f'Output already exists (use --overwrite): {existing}')
    unresolved = [row for row in rows if row['cohort'] not in ('SOLVED', 'INELIGIBLE')]
    headers = list(rows[0])
    sample_headers = headers + ['sample_cohort', 'sample_position', 'sample_seed']
    _atomic_csv(paths['coverage_companies.csv'], rows, headers, overwrite)
    _atomic_csv(paths['unresolved_companies.csv'], unresolved, headers, overwrite)
    _atomic_csv(paths['samples.csv'], samples, sample_headers, overwrite)
    _atomic_csv(paths['snippet_support.csv'], snippet_rows, SNIPPET_HEADERS, overwrite)
    _atomic_json(paths['summary.json'], summary, overwrite)
    return {name: str(path) for name, path in paths.items()}


def parse_reusable(values):
    result = []
    for value in values:
        if '=' not in value:
            raise ValueError('--reusable-results must use RESULTS_DB=RUN_ID')
        path, run_id = value.rsplit('=', 1)
        if not path or not run_id:
            raise ValueError('--reusable-results must use RESULTS_DB=RUN_ID')
        result.append((path, run_id))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--master', required=True, help='Canonical master SQLite database')
    parser.add_argument('--current-results', required=True,
                        help='Final/current Discovery v2 results.sqlite3')
    parser.add_argument('--current-run-id', required=True)
    parser.add_argument('--reusable-results', action='append', default=[],
                        metavar='RESULTS_DB=RUN_ID',
                        help='Earlier Discovery v2 source; repeat as needed')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--sample-size', type=int, default=25)
    parser.add_argument('--seed', default='20261008')
    parser.add_argument('--sample-cohort', action='append', choices=tuple(COHORT_DEFINITIONS),
                        help='Override default sampled cohorts; repeat as needed')
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.sample_size < 0:
            raise ValueError('--sample-size must be nonnegative')
        reusable = parse_reusable(args.reusable_results)
        cohorts = tuple(args.sample_cohort or SAMPLE_COHORTS)
        rows, summary, samples, snippet_rows = build_audit(args.master, args.current_results,
            args.current_run_id, reusable, args.sample_size, args.seed, cohorts)
        paths = write_reports(args.output_dir, rows, summary, samples, snippet_rows,
            [args.master, args.current_results, *[path for path, _ in reusable]], args.overwrite)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f'error: {exc}\n')
    for key, value in summary['funnel'].items():
        print(f'{key.upper()}={value}')
    print('CURRENT_COMPANY_STATUSES='+json.dumps(
        summary['current_run']['company_statuses'], sort_keys=True, separators=(',', ':')))
    print('CURRENT_ATTEMPT_STATUSES='+json.dumps(
        summary['current_run']['attempt_statuses'], sort_keys=True, separators=(',', ':')))
    print('COHORTS='+json.dumps(summary['cohorts'], sort_keys=True, separators=(',', ':')))
    for name, path in paths.items():
        print(f'{name.upper().replace(".", "_")}={path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
