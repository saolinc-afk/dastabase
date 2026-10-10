"""Read-only stored-evidence recovery opportunity audit for a completed v2 run."""
import argparse
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from discovery.domain_generator import normalize_domain
from discovery.domain_policy import COMPANY_DATABASE, DIRECTORY, policy_for_url, blocks_official
from discovery_v2.coverage_audit import (_atomic_csv, _atomic_json, json_object,
                                         load_master, load_v2, readonly,
                                         validate_master_identities)
from discovery_v2.evidence import public_email_domain


OUTPUT_NAMES = ('recovery_summary.json', 'partial_recovery.csv',
                'failed_analysis.csv', 'RECOVERY_REPORT.md')
PARTIAL_HEADERS = (
    'company_id', 'company_name', 'tax_number', 'registration_number', 'address',
    'municipality', 'attempt_id', 'recovery_bucket', 'accepted_website',
    'accepted_email', 'accepted_phone', 'attributed_email_domains',
    'attributed_email_domain_candidate_count', 'raw_email_domains',
    'raw_unattributed_email_domains', 'public_email_domains',
    'multi_evidence_email_domains', 'website_candidate_domains',
    'strong_website_candidate_domains', 'strong_email_domain_candidates',
    'candidate_count', 'ambiguous_candidate_count', 'search_result_count',
    'visited_page_candidate_count', 'visited_attributable_email_count',
    'visited_raw_email_count', 'visited_attributable_phone_count',
    'visited_raw_phone_count', 'visited_identity_evidence_count',
    'stored_contact_recovery', 'candidate_details_json', 'evidence_class_counts_json',
)
FAILED_HEADERS = PARTIAL_HEADERS + (
    'failure_category', 'failure_error', 'failure_stage',
    'stored_reinterpretation_first', 'failed_only_retry_candidate',
    'retry_likely_requires_search',
)

RECOVERY_BUCKETS = {
    'STRONG_STORED_WEBSITE_CANDIDATE': (
        'Exactly one non-disallowed stored domain has exact target identity support, '
        'two independent identity-matched evidence records, or attributable-email '
        'agreement plus target identity support.'),
    'STRONG_EMAIL_DOMAIN_CANDIDATE': (
        'Exactly one non-public attributed email domain differs from the accepted '
        'website (or no website exists) and is corroborated by multiple evidence '
        'records or an identity-matched stored website candidate.'),
    'CONTACT_RECOVERY_ONLY': (
        'No strong website signal, but stored attributable/raw contact observations '
        'contain values absent from the selected defaults.'),
    'MULTIPLE_AMBIGUOUS_CANDIDATES': (
        'Two or more plausible stored domains remain, or multiple domains meet a '
        'strong diagnostic criterion. No domain is selected by this audit.'),
    'STORED_EVIDENCE_LOW_SIGNAL': (
        'Stored search/page/contact evidence exists but does not meet a conservative '
        'recovery-opportunity criterion.'),
    'LIKELY_NEEDS_NEW_SEARCH': (
        'No non-public email-domain candidate, plausible stored website candidate, '
        'or recoverable stored contact was found.'),
}

FAILURE_CATEGORIES = {
    'CIRCUIT_BREAKER_OR_INFRASTRUCTURE': 'Persisted error names a circuit/provider interruption or storage/runtime infrastructure.',
    'PROVIDER_SEARCH_FAILURE': 'Persisted query/provider error occurred during search.',
    'FETCH_TIMEOUT': 'Persisted acquisition error records a timeout.',
    'FETCH_HTTP_OR_ACCESS': 'Persisted acquisition error records HTTP/access/rate-limit failure.',
    'FETCH_DNS_OR_NETWORK': 'Persisted acquisition error records DNS/connection/transport failure.',
    'VERIFICATION_EXCEPTION': 'Persisted error records ownership/P1A/candidate verification failure.',
    'PARSER_EXTRACTION_EXCEPTION': 'Persisted error records parsing, decoding, extraction, or data-shape failure.',
    'CANDIDATE_VERIFICATION_EXHAUSTED': ('No narrower exception is persisted; stored candidates '
        'exist but every recorded assessment remained unusable.'),
    'NO_USEFUL_CANDIDATE': ('No narrower exception is persisted and no useful search result or '
        'website candidate was stored.'),
    'UNKNOWN_UNCLASSIFIED': 'Persisted error text does not support a narrower deterministic category.',
}

IDENTITY_TYPES = frozenset({'COMPANY_IDENTITY', 'LEGAL_NAME', 'TAX_NUMBER',
    'REGISTRATION_NUMBER', 'ADDRESS', 'STREET', 'POSTAL_CODE', 'MUNICIPALITY'})
EXACT_IDENTITY_TYPES = frozenset({'LEGAL_NAME', 'TAX_NUMBER', 'REGISTRATION_NUMBER',
    'ADDRESS', 'STREET'})


def _email_domain(value):
    value = str(value or '').strip().lower()
    if value.count('@') != 1:
        return ''
    return normalize_domain('https://' + value.rsplit('@', 1)[1] + '/')


def _target_attempts(conn, run_id):
    query = '''SELECT rc.company_id,rc.status,rc.selected_attempt_id,
      COALESCE(rc.selected_attempt_id,(SELECT a.attempt_id FROM discovery_attempts a
        WHERE a.run_id=rc.run_id AND a.company_id=rc.company_id
        ORDER BY a.attempt_number DESC LIMIT 1)) AS audit_attempt_id
      FROM discovery_run_companies rc
      WHERE rc.run_id=? AND rc.status IN ('PARTIAL','FAILED')
      ORDER BY rc.manifest_position'''
    return {row['company_id']: dict(row) for row in conn.execute(query, (run_id,))}


def _base_features(targets, companies, run_records):
    features = {}
    for company_id, target in targets.items():
        company = companies.get(company_id)
        if company is None:
            continue
        record = run_records[company_id]
        result = record.get('result') or {}
        accepted_website = result.get('official_website') or ''
        legacy = company['legacy']
        if not accepted_website and legacy['website_status'] == 'VERIFIED':
            accepted_website = legacy['website'] or ''
        features[company_id] = {
            'company': company, 'status': target['status'],
            'attempt_id': target['audit_attempt_id'] or '',
            'accepted_website': accepted_website,
            'accepted_email': result.get('email_value') or legacy.get('email') or '',
            'accepted_phone': result.get('phone_value') or '',
            'diagnostics': record['latest']['diagnostic'],
            'website_candidates': defaultdict(lambda: {
                'urls': set(), 'observation_ids': set(), 'evidence_ids': set(),
                'source_kinds': set(), 'providers': set(), 'publishers': set(),
                'query_occurrences': set(),
                'ranks': set(), 'origins': set(), 'directory_publishers': set(),
                'identity_types': set(), 'exact_identity_types': set(),
                'identity_matched': False, 'phone_agreement': False,
                'attributed_email_agreement': False, 'activity_observations': 0,
                'disallowed_domain': False, 'search_result_urls': set(),
            }),
            'observations_by_evidence': defaultdict(list),
            'evidence': {}, 'contacts': [], 'email_domains': defaultdict(lambda: {
                'observation_ids': set(), 'evidence_ids': set(), 'source_kinds': set(),
                'attributed': False, 'public': False}),
            'visited': {'candidate_domains': set(), 'attributed_emails': set(),
                'raw_emails': set(), 'attributed_phones': set(), 'raw_phones': set(),
                'identity_observations': set()},
            'storage_classes': Counter(), 'failure_error': '',
        }
    return features


def _load_detailed_features(path, run_id, companies, run_records):
    _, conn = readonly(path)
    try:
        targets = _target_attempts(conn, run_id)
        features = _base_features(targets, companies, run_records)
        by_attempt = {value['attempt_id']: value for value in features.values()
                      if value['attempt_id']}
        if not by_attempt:
            return features

        # Never select snippet_body/evidence_payload_json: the production artifact is
        # large because it preserves HTML, and this audit needs only compact lineage.
        evidence_query = '''SELECT e.evidence_id,e.company_id,e.source_kind,e.provider,
          e.query_type,e.query_text,e.result_rank,e.result_url,e.result_host
          FROM discovery_evidence e JOIN (
          SELECT rc.company_id,COALESCE(rc.selected_attempt_id,
            (SELECT a.attempt_id FROM discovery_attempts a WHERE a.run_id=rc.run_id
             AND a.company_id=rc.company_id ORDER BY a.attempt_number DESC LIMIT 1)) attempt_id
          FROM discovery_run_companies rc WHERE rc.run_id=?
            AND rc.status IN ('PARTIAL','FAILED')) t
          ON t.company_id=e.company_id AND t.attempt_id=e.attempt_id
          WHERE e.run_id=? ORDER BY e.company_id,e.rowid'''
        for stored in conn.execute(evidence_query, (run_id, run_id)):
            row = dict(stored); feature = features.get(row['company_id'])
            if feature is None: continue
            feature['evidence'][row['evidence_id']] = row

        observation_query = '''SELECT o.* FROM discovery_observations o JOIN (
          SELECT rc.company_id,COALESCE(rc.selected_attempt_id,
            (SELECT a.attempt_id FROM discovery_attempts a WHERE a.run_id=rc.run_id
             AND a.company_id=rc.company_id ORDER BY a.attempt_number DESC LIMIT 1)) attempt_id
          FROM discovery_run_companies rc WHERE rc.run_id=?
            AND rc.status IN ('PARTIAL','FAILED')) t
          ON t.company_id=o.company_id AND t.attempt_id=o.attempt_id
          WHERE o.run_id=? ORDER BY o.company_id,o.rowid'''
        for stored in conn.execute(observation_query, (run_id, run_id)):
            row = dict(stored); feature = features.get(row['company_id'])
            if feature is None: continue
            value, valid = json_object(row['value_json'])
            row['value'] = value; row['value_valid'] = valid
            feature['observations_by_evidence'][row['evidence_id']].append(row)

        contact_query = '''SELECT c.* FROM discovery_contacts c JOIN (
          SELECT rc.company_id,COALESCE(rc.selected_attempt_id,
            (SELECT a.attempt_id FROM discovery_attempts a WHERE a.run_id=rc.run_id
             AND a.company_id=rc.company_id ORDER BY a.attempt_number DESC LIMIT 1)) attempt_id
          FROM discovery_run_companies rc WHERE rc.run_id=?
            AND rc.status IN ('PARTIAL','FAILED')) t
          ON t.company_id=c.company_id AND t.attempt_id=c.attempt_id
          WHERE c.run_id=? ORDER BY c.company_id,c.rowid'''
        for stored in conn.execute(contact_query, (run_id, run_id)):
            row = dict(stored); feature = features.get(row['company_id'])
            if feature is not None: feature['contacts'].append(row)

        attempt_query = '''SELECT a.company_id,a.attempt_id,a.diagnostics_json FROM discovery_attempts a
          JOIN discovery_run_companies rc ON rc.run_id=a.run_id AND rc.company_id=a.company_id
          WHERE a.run_id=? AND rc.status IN ('PARTIAL','FAILED')
            AND a.attempt_id=COALESCE(rc.selected_attempt_id,
              (SELECT x.attempt_id FROM discovery_attempts x WHERE x.run_id=rc.run_id
               AND x.company_id=rc.company_id ORDER BY x.attempt_number DESC LIMIT 1))'''
        for stored in conn.execute(attempt_query, (run_id,)):
            feature = features.get(stored['company_id'])
            if feature is None: continue
            diagnostics, valid = json_object(stored['diagnostics_json'])
            feature['diagnostics_object'] = diagnostics if valid else {}
            feature['failure_error'] = str(diagnostics.get('error') or '')[:500]
        return features
    finally:
        conn.close()


def _contact_maps(feature):
    attributed = {'EMAIL': set(), 'PHONE': set()}
    for contact in feature['contacts']:
        if contact['attribution_status'] == 'ATTRIBUTED':
            attributed[contact['contact_type']].add(contact['normalized_value'])
    return attributed


def _candidate_details(feature):
    attributed = _contact_maps(feature)
    accepted_domain = normalize_domain(feature['accepted_website'])
    for evidence_id, observations in feature['observations_by_evidence'].items():
        evidence = feature['evidence'].get(evidence_id, {})
        source_kind = evidence.get('source_kind') or 'UNKNOWN'
        identity_types = {row['observation_type'] for row in observations
                          if row['observation_type'] in IDENTITY_TYPES and (
                              row['observation_type'] == 'COMPANY_IDENTITY' or
                              row['value'].get('verification_status') == 'EXACT_MATCH' or
                              row['value'].get('identity_match') is True)}
        exact_types = identity_types & EXACT_IDENTITY_TYPES
        activity_count = sum(row['observation_type'] in (
            'REGISTERED_ACTIVITY','BUSINESS_DESCRIPTION') for row in observations)
        phones = {row['normalized_value'] for row in observations
                  if row['observation_type'] == 'PHONE_CANDIDATE'}
        emails = {row['normalized_value'] for row in observations
                  if row['observation_type'] == 'EMAIL_CANDIDATE'}

        for row in observations:
            kind = row['observation_type']
            if kind == 'EMAIL_CANDIDATE':
                domain = _email_domain(row['normalized_value'])
                if domain:
                    item = feature['email_domains'][domain]
                    item['observation_ids'].add(row['observation_id'])
                    item['evidence_ids'].add(evidence_id)
                    item['source_kinds'].add(source_kind)
                    item['attributed'] |= row['normalized_value'] in attributed['EMAIL']
                    item['public'] = public_email_domain(domain)
                    if source_kind == 'FETCHED_PAGE':
                        (feature['visited']['attributed_emails'] if
                         row['normalized_value'] in attributed['EMAIL'] else
                         feature['visited']['raw_emails']).add(row['normalized_value'])
            elif kind == 'PHONE_CANDIDATE' and source_kind == 'FETCHED_PAGE':
                (feature['visited']['attributed_phones'] if
                 row['normalized_value'] in attributed['PHONE'] else
                 feature['visited']['raw_phones']).add(row['normalized_value'])
            elif kind in IDENTITY_TYPES and source_kind == 'FETCHED_PAGE':
                feature['visited']['identity_observations'].add(row['observation_id'])
            if kind != 'WEBSITE_CANDIDATE':
                continue
            domain = normalize_domain(row['normalized_value'])
            if not domain or domain == accepted_domain:
                continue
            candidate = feature['website_candidates'][domain]
            candidate['urls'].add(row['normalized_value'])
            candidate['observation_ids'].add(row['observation_id'])
            candidate['evidence_ids'].add(evidence_id)
            candidate['source_kinds'].add(source_kind)
            if evidence.get('provider'):
                candidate['providers'].add(evidence['provider'])
            candidate['origins'].add(row['extraction_method'])
            candidate['identity_matched'] |= row['value'].get('identity_match') is True
            candidate['identity_types'].update(identity_types)
            candidate['exact_identity_types'].update(exact_types)
            candidate['phone_agreement'] |= bool(phones & attributed['PHONE'])
            candidate['attributed_email_agreement'] |= any(
                _email_domain(email) == domain for email in emails & attributed['EMAIL'])
            candidate['activity_observations'] += activity_count
            candidate['disallowed_domain'] |= blocks_official('https://' + domain + '/')
            publisher = normalize_domain(evidence.get('result_url') or
                                         evidence.get('result_host') or '')
            if publisher: candidate['publishers'].add(publisher)
            if source_kind == 'SEARCH_RESULT':
                candidate['search_result_urls'].add(evidence.get('result_url') or '')
                candidate['query_occurrences'].add((evidence.get('query_type') or '',
                    evidence.get('query_text') or '', evidence_id))
                if evidence.get('result_rank') is not None:
                    candidate['ranks'].add(evidence['result_rank'])
                policy = policy_for_url(evidence.get('result_url') or '')
                if policy and policy.classification in (DIRECTORY, COMPANY_DATABASE):
                    candidate['directory_publishers'].add(publisher)
            if source_kind == 'FETCHED_PAGE':
                feature['visited']['candidate_domains'].add(domain)

    # Contacts can exist even when a supporting observation was not loaded due to
    # malformed historical provenance; keep the attributed domain visible.
    for email in attributed['EMAIL']:
        domain = _email_domain(email)
        if domain:
            item = feature['email_domains'][domain]
            item['attributed'] = True
            item['public'] = public_email_domain(domain)

    details = []
    for domain, candidate in sorted(feature['website_candidates'].items()):
        strong = (not candidate['disallowed_domain'] and (
            bool(candidate['exact_identity_types'] & {'TAX_NUMBER','REGISTRATION_NUMBER','ADDRESS'}) or
            (candidate['identity_matched'] and len(candidate['evidence_ids']) >= 2
             and len(candidate['publishers']) >= 2) or
            (candidate['identity_matched'] and candidate['attributed_email_agreement'])))
        details.append({
            'domain': domain, 'urls': sorted(candidate['urls']),
            'observation_ids': sorted(candidate['observation_ids']),
            'evidence_ids': sorted(candidate['evidence_ids']),
            'source_kinds': sorted(candidate['source_kinds']),
            'providers': sorted(candidate['providers']),
            'origins': sorted(candidate['origins']),
            'publishers': sorted(candidate['publishers']),
            'independent_evidence_count': len(candidate['evidence_ids']),
            'independent_publisher_count': len(candidate['publishers']),
            'identity_match': candidate['identity_matched'],
            'identity_support': sorted(candidate['identity_types']),
            'exact_identity_support': sorted(candidate['exact_identity_types']),
            'attributed_email_domain_agreement': candidate['attributed_email_agreement'],
            'attributed_phone_agreement': candidate['phone_agreement'],
            'business_activity_observation_count': candidate['activity_observations'],
            'query_occurrence_count': len(candidate['query_occurrences']),
            'minimum_search_rank': min(candidate['ranks']) if candidate['ranks'] else None,
            'search_result_urls': sorted(candidate['search_result_urls']),
            'registered_directory_publishers': sorted(candidate['directory_publishers']),
            'disallowed_official_domain': candidate['disallowed_domain'],
            'strong_diagnostic_signal': strong,
        })
    return details


def _failure_category(feature):
    diagnostics = feature.get('diagnostics_object') or {}
    parts = [feature.get('failure_error') or '']
    for query in diagnostics.get('queries') or []:
        if isinstance(query, dict):
            if query.get('status') == 'FAILED':
                parts.append('search failed')
            parts.extend(str(query.get(key) or '') for key in
                         ('error','reason','search_outcome'))
    parts.extend(str(value) for value in diagnostics.get('fetch_errors') or [])
    completeness = diagnostics.get('completeness')
    if isinstance(completeness, dict):
        parts.extend(str(value) for value in completeness.get('errors') or [])
    text = ' '.join(parts).casefold()
    if any(value in text for value in ('providercircuitopen','circuit breaker','database is locked','disk i/o','sqlite')):
        return 'CIRCUIT_BREAKER_OR_INFRASTRUCTURE'
    if any(value in text for value in ('timeout','timed out','readtimeout','connecttimeout')):
        return 'FETCH_TIMEOUT'
    if any(value in text for value in ('serper','provider','search failed','search_outcome failed')):
        return 'PROVIDER_SEARCH_FAILURE'
    if any(value in text for value in ('http 4','http 5','access denied','rate limit','robots','forbidden')):
        return 'FETCH_HTTP_OR_ACCESS'
    if any(value in text for value in ('dns','failed to resolve','name resolution','connectionerror',
            'connection reset','connection refused','proxyerror','ssl','tls','certificate')):
        return 'FETCH_DNS_OR_NETWORK'
    if any(value in text for value in ('p1a','ownership','verification','candidate provenance','official scope')):
        return 'VERIFICATION_EXCEPTION'
    if any(value in text for value in ('jsondecode','keyerror','typeerror','attributeerror',
            'parse','parser','extract','malformed','invalid literal')):
        return 'PARSER_EXTRACTION_EXCEPTION'
    candidates = [item for item in diagnostics.get('website_candidates') or []
                  if isinstance(item, dict)]
    if not text.strip() and candidates and all(
            item.get('status') not in ('VERIFIED','HIGH','MEDIUM') for item in candidates):
        return 'CANDIDATE_VERIFICATION_EXHAUSTED'
    if not text.strip() and not feature['evidence']:
        return 'NO_USEFUL_CANDIDATE'
    return 'UNKNOWN_UNCLASSIFIED'


def _row(feature):
    company = feature['company']; details = _candidate_details(feature)
    accepted_domain = normalize_domain(feature['accepted_website'])
    plausible = [item for item in details if not item['disallowed_official_domain']]
    strong = [item for item in plausible if item['strong_diagnostic_signal']]
    email_domains = feature['email_domains']
    attributed_domains = sorted(domain for domain, item in email_domains.items()
                                if item['attributed'])
    public_domains = sorted(domain for domain, item in email_domains.items() if item['public'])
    raw_domains = sorted(email_domains)
    raw_unattributed = sorted(domain for domain, item in email_domains.items()
                              if not item['attributed'] and not item['public'])
    multi = sorted(domain for domain, item in email_domains.items()
                   if not item['public'] and len(item['evidence_ids']) >= 2)
    email_candidates = sorted(domain for domain, item in email_domains.items()
        if item['attributed'] and not item['public'] and domain != accepted_domain)
    strong_email = sorted(domain for domain in email_candidates if
        len(email_domains[domain]['evidence_ids']) >= 2 or
        any(item['domain'] == domain and item['identity_match'] for item in plausible))
    accepted_email = feature['accepted_email']; accepted_phone = feature['accepted_phone']
    visited = feature['visited']
    stored_contact = bool(
        {value for value in visited['attributed_emails'] | visited['raw_emails']
         if value != accepted_email} or
        {value for value in visited['attributed_phones'] | visited['raw_phones']
         if value != accepted_phone})
    if len(plausible) > 1:
        bucket = 'MULTIPLE_AMBIGUOUS_CANDIDATES'
    elif len(strong) == 1:
        bucket = 'STRONG_STORED_WEBSITE_CANDIDATE'
    elif len(strong_email) == 1:
        bucket = 'STRONG_EMAIL_DOMAIN_CANDIDATE'
    elif stored_contact:
        bucket = 'CONTACT_RECOVERY_ONLY'
    elif plausible or raw_unattributed or feature['evidence']:
        bucket = 'STORED_EVIDENCE_LOW_SIGNAL'
    else:
        bucket = 'LIKELY_NEEDS_NEW_SEARCH'

    source_counts = Counter(evidence.get('source_kind') or 'UNKNOWN'
                            for evidence in feature['evidence'].values())
    base = {
        'company_id': company['id'], 'company_name': company['company_name'] or '',
        'tax_number': company['tax_number'] or '',
        'registration_number': company['registration_number'] or '',
        'address': company['address'] or '', 'municipality': company['municipality'] or '',
        'attempt_id': feature['attempt_id'], 'recovery_bucket': bucket,
        'accepted_website': feature['accepted_website'],
        'accepted_email': accepted_email, 'accepted_phone': accepted_phone,
        'attributed_email_domains': '|'.join(attributed_domains),
        'attributed_email_domain_candidate_count': len(email_candidates),
        'raw_email_domains': '|'.join(raw_domains),
        'raw_unattributed_email_domains': '|'.join(raw_unattributed),
        'public_email_domains': '|'.join(public_domains),
        'multi_evidence_email_domains': '|'.join(multi),
        'website_candidate_domains': '|'.join(item['domain'] for item in plausible),
        'strong_website_candidate_domains': '|'.join(item['domain'] for item in strong),
        'strong_email_domain_candidates': '|'.join(strong_email),
        'candidate_count': len(plausible),
        'ambiguous_candidate_count': len(plausible) if len(plausible) > 1 else 0,
        'search_result_count': sum(e.get('source_kind') == 'SEARCH_RESULT'
                                   for e in feature['evidence'].values()),
        'visited_page_candidate_count': len(visited['candidate_domains']),
        'visited_attributable_email_count': len(visited['attributed_emails']),
        'visited_raw_email_count': len(visited['raw_emails']),
        'visited_attributable_phone_count': len(visited['attributed_phones']),
        'visited_raw_phone_count': len(visited['raw_phones']),
        'visited_identity_evidence_count': len(visited['identity_observations']),
        'stored_contact_recovery': 'YES' if stored_contact else 'NO',
        'candidate_details_json': json.dumps(details, ensure_ascii=False,
                                             sort_keys=True, separators=(',', ':')),
        'evidence_class_counts_json': json.dumps(dict(sorted(source_counts.items())),
                                                 sort_keys=True, separators=(',', ':')),
    }
    if feature['status'] == 'FAILED':
        category = _failure_category(feature)
        useful_search = bool(base['search_result_count'])
        useful_candidate = bool(plausible or email_candidates or stored_contact)
        if useful_candidate:
            stage = 'AFTER_USEFUL_CANDIDATE_EVIDENCE'
        elif useful_search:
            stage = 'AFTER_SEARCH_EVIDENCE'
        else:
            stage = 'BEFORE_USEFUL_SEARCH_EVIDENCE'
        transient = category in {'CIRCUIT_BREAKER_OR_INFRASTRUCTURE',
            'PROVIDER_SEARCH_FAILURE','FETCH_TIMEOUT','FETCH_HTTP_OR_ACCESS',
            'FETCH_DNS_OR_NETWORK'}
        base.update(failure_category=category,
            failure_error=feature['failure_error'], failure_stage=stage,
            stored_reinterpretation_first='YES' if useful_candidate else 'NO',
            failed_only_retry_candidate='YES' if transient else 'NO',
            retry_likely_requires_search='NO' if useful_candidate else 'YES')
    return base


def _storage_value(features, rows):
    company_bucket = {row['company_id']: row['recovery_bucket'] for row in rows}
    kinds = defaultdict(set); observations = defaultdict(set)
    contributing = {'strong_candidate_companies': set(), 'contact_recovery_companies': set()}
    opportunity_kinds = defaultdict(set)
    opportunity_observations = defaultdict(set)
    opportunity_buckets = {'STRONG_STORED_WEBSITE_CANDIDATE',
        'STRONG_EMAIL_DOMAIN_CANDIDATE', 'CONTACT_RECOVERY_ONLY'}
    for company_id, feature in features.items():
        opportunity = company_bucket.get(company_id, '') in opportunity_buckets
        for evidence in feature['evidence'].values():
            kind = evidence.get('source_kind') or 'UNKNOWN'
            kinds[kind].add(company_id)
            if opportunity:
                opportunity_kinds[kind].add(company_id)
        for rows_for_evidence in feature['observations_by_evidence'].values():
            for observation in rows_for_evidence:
                kind = observation['observation_type']
                observations[kind].add(company_id)
                if opportunity:
                    opportunity_observations[kind].add(company_id)
        bucket = company_bucket.get(company_id, '')
        if bucket in ('STRONG_STORED_WEBSITE_CANDIDATE','STRONG_EMAIL_DOMAIN_CANDIDATE'):
            contributing['strong_candidate_companies'].add(company_id)
        if bucket == 'CONTACT_RECOVERY_ONLY':
            contributing['contact_recovery_companies'].add(company_id)
    return {'companies_by_evidence_source_kind': {key:len(value) for key,value in sorted(kinds.items())},
        'companies_by_observation_type': {key:len(value) for key,value in sorted(observations.items())},
        'opportunity_companies_by_evidence_source_kind': {
            key:len(value) for key,value in sorted(opportunity_kinds.items())},
        'opportunity_companies_by_observation_type': {
            key:len(value) for key,value in sorted(opportunity_observations.items())},
        **{key:len(value) for key,value in contributing.items()},
        'interpretation': ('Counts show which persisted evidence classes participate in the '
            'PARTIAL/FAILED diagnostic corpus; they do not imply accepted facts.')}


def build_recovery_audit(master, results, run_id):
    master_path, companies, activity_column = load_master(master)
    current = load_v2(results, run_id, 'CURRENT_V2', 'CURRENT')
    validate_master_identities(companies, [current])
    features = _load_detailed_features(results, run_id, companies, current['records'])
    rows = [_row(feature) for _, feature in sorted(features.items())]
    partial = [row for row in rows if features[row['company_id']]['status'] == 'PARTIAL']
    failed = [row for row in rows if features[row['company_id']]['status'] == 'FAILED']
    partial_buckets = Counter(row['recovery_bucket'] for row in partial)
    failed_buckets = Counter(row['recovery_bucket'] for row in failed)
    summary = {
        'audit_version': 1,
        'inputs': {'master': str(master_path), 'results': current['path'], 'run_id': run_id},
        'source_statuses': current['company_statuses'],
        'master_boundary': {
            'outside_master_result_company_count': sum(
                company_id not in companies for company_id in current['records']),
            'outside_master_result_company_ids': sorted(
                company_id for company_id in current['records'] if company_id not in companies),
        },
        'partial': {
            'companies': len(partial), 'recovery_buckets': dict(sorted(partial_buckets.items())),
            'potentially_recoverable_stored_only': sum(row['recovery_bucket'] in (
                'STRONG_STORED_WEBSITE_CANDIDATE','STRONG_EMAIL_DOMAIN_CANDIDATE',
                'CONTACT_RECOVERY_ONLY') for row in partial),
            'strong_website_candidate_signal': sum(row['recovery_bucket'] in (
                'STRONG_STORED_WEBSITE_CANDIDATE','STRONG_EMAIL_DOMAIN_CANDIDATE') for row in partial),
            'contact_only_recovery_opportunity': partial_buckets['CONTACT_RECOVERY_ONLY'],
            'likely_requires_new_search': partial_buckets['LIKELY_NEEDS_NEW_SEARCH'],
            'genuinely_ambiguous': partial_buckets['MULTIPLE_AMBIGUOUS_CANDIDATES'],
            'with_attributed_email_domain_candidate': sum(
                bool(row['attributed_email_domain_candidate_count']) for row in partial),
            'with_raw_unattributed_email_domain': sum(
                bool(row['raw_unattributed_email_domains']) for row in partial),
            'with_public_email_domain_observation': sum(
                bool(row['public_email_domains']) for row in partial),
            'with_multi_evidence_email_domain': sum(
                bool(row['multi_evidence_email_domains']) for row in partial),
        },
        'failed': {
            'companies': len(failed), 'recovery_buckets': dict(sorted(failed_buckets.items())),
            'failure_categories': dict(sorted(Counter(
                row['failure_category'] for row in failed).items())),
            'failure_stages': dict(sorted(Counter(row['failure_stage'] for row in failed).items())),
            'potentially_recoverable_stored_only': sum(
                row['stored_reinterpretation_first'] == 'YES' for row in failed),
            'failed_only_retry_candidates': sum(
                row['failed_only_retry_candidate'] == 'YES' for row in failed),
            'retry_likely_requires_search': sum(
                row['retry_likely_requires_search'] == 'YES' for row in failed),
        },
        'storage_value': _storage_value(features, rows),
        'definitions': {'recovery_buckets': RECOVERY_BUCKETS,
                        'failure_categories': FAILURE_CATEGORIES,
                        'strong_signal_is_acceptance': False,
                        'candidate_is_official_website': False},
        'availability': {'registered_activity_column': activity_column,
            'business_activity_consistency': ('Exact semantic consistency is not inferred; '
                'only persisted activity-observation presence is counted.'),
            'raw_html_exported': False},
    }
    return partial, failed, summary


def _atomic_text(path, value, overwrite):
    if path.exists() and not overwrite:
        raise ValueError(f'Output already exists (use --overwrite): {path}')
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(value)
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except OSError: pass
        raise


def human_report(summary):
    partial, failed = summary['partial'], summary['failed']
    lines = ['# Discovery stored-evidence recovery opportunity audit', '',
        'All values are diagnostic opportunities, not accepted facts or guaranteed recoveries.', '',
        '## PARTIAL', '', f"- Companies: {partial['companies']}",
        f"- Potentially recoverable from stored evidence: {partial['potentially_recoverable_stored_only']}",
        f"- Strong website/email-domain signal: {partial['strong_website_candidate_signal']}",
        f"- Contact-only opportunity: {partial['contact_only_recovery_opportunity']}",
        f"- Ambiguous stored candidates: {partial['genuinely_ambiguous']}",
        f"- Likely needs new search: {partial['likely_requires_new_search']}", '',
        '## FAILED', '', f"- Companies: {failed['companies']}",
        f"- Stored reinterpretation first: {failed['potentially_recoverable_stored_only']}",
        f"- Bounded FAILED-only retry candidates: {failed['failed_only_retry_candidates']}",
        f"- Retry likely requires search: {failed['retry_likely_requires_search']}", '',
        '### Failure categories', '']
    lines.extend(f'- {key}: {value}' for key, value in failed['failure_categories'].items())
    lines.extend(['', '## Safety', '',
        '- Search results, email domains, and observations remain candidate evidence only.',
        '- No candidate is promoted and no acceptance rule is changed.',
        '- Input databases are opened read-only; reports contain no raw HTML.', ''])
    return '\n'.join(lines)


def write_recovery_reports(output_dir, partial, failed, summary, inputs, overwrite=False):
    output = Path(output_dir).expanduser().absolute()
    resolved_inputs = {Path(value).expanduser().resolve() for value in inputs}
    if output.resolve() in resolved_inputs:
        raise ValueError('Output directory cannot be a SQLite input')
    output.mkdir(parents=True, exist_ok=True)
    paths = {name:output/name for name in OUTPUT_NAMES}
    if not overwrite and any(path.exists() for path in paths.values()):
        raise ValueError('Recovery output already exists; choose a new directory or use --overwrite')
    if any(path.resolve() in resolved_inputs for path in paths.values()):
        raise ValueError('Output report cannot overwrite a SQLite input')
    _atomic_json(paths['recovery_summary.json'], summary, overwrite)
    _atomic_csv(paths['partial_recovery.csv'], partial, PARTIAL_HEADERS, overwrite)
    _atomic_csv(paths['failed_analysis.csv'], failed, FAILED_HEADERS, overwrite)
    _atomic_text(paths['RECOVERY_REPORT.md'], human_report(summary), overwrite)
    return {name:str(path) for name,path in paths.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--master', required=True)
    parser.add_argument('--results', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    try:
        partial, failed, summary = build_recovery_audit(
            args.master, args.results, args.run_id)
        paths = write_recovery_reports(args.output_dir, partial, failed, summary,
                                       (args.master,args.results), args.overwrite)
    except (ValueError,OSError,sqlite3.Error) as exc:
        parser.exit(2, f'error: {exc}\n')
    print('PARTIAL_RECOVERY='+json.dumps(summary['partial'],sort_keys=True,separators=(',',':')))
    print('FAILED_ANALYSIS='+json.dumps(summary['failed'],sort_keys=True,separators=(',',':')))
    for name,path in paths.items():
        print(f'{name.upper().replace(".","_")}={path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
