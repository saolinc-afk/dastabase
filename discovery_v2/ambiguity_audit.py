"""Read-only ambiguity breakdown for stored Discovery v2 candidate evidence."""
import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from discovery.domain_generator import normalize_domain
from discovery.domain_policy import (
    BOOKING_AGGREGATOR, COMPANY_DATABASE, DEALER_PORTAL, DIRECTORY,
    JOB_PORTAL, MARKETPLACE, MEDIA_NEWS, OTHER_THIRD_PARTY,
    SOCIAL_PLATFORM, TOURISM_PROFILE, blocks_official, policy_for_url,
)
from discovery_v2.candidates import brand_match
from discovery_v2.coverage_audit import (
    _atomic_csv, _atomic_json, load_master, load_v2,
    validate_master_identities,
)
from discovery_v2.evidence import public_email_domain
from discovery_v2.recovery_audit import (
    _contact_maps, _email_domain, _load_detailed_features, _row,
)


OUTPUT_NAMES = ('ambiguity_summary.json', 'ambiguity_companies.csv',
                'ambiguity_sample_100.csv', 'AMBIGUITY_REPORT.md')
COMPANY_HEADERS = (
    'company_id', 'company_name', 'tax_number', 'registration_number',
    'address', 'municipality', 'attempt_id', 'accepted_website',
    'selected_result_id', 'selected_result_attempt_id',
    'selected_website_status', 'selected_contact_outcome',
    'plausible_host_count', 'plausible_registrable_domain_count',
    'all_candidate_host_count', 'all_candidate_url_count',
    'same_host_url_variant_domain_count', 'candidate_cardinality_band',
    'diagnostic_category', 'leader_domain', 'leader_score',
    'runner_up_score', 'leader_margin', 'same_domain_variants',
    'foreign_country_candidate_count', 'known_third_party_candidate_count',
    'meaningful_identity_candidate_count', 'candidate_type_counts_json',
    'negative_signal_counts_json', 'candidate_details_json',
)

DIAGNOSTIC_CATEGORIES = {
    'ONE_CLEAR_EVIDENCE_LEADER': (
        'One non-blocked candidate has exact tax/registration support, or an '
        'equivalent high-grade identity/contact combination, with a score margin '
        'of at least 50. This remains diagnostic, not acceptance.'),
    'ONE_MODERATE_LEADER': (
        'One non-blocked candidate has legal/address/contact support and a score '
        'of at least 45 with a margin of at least 20, without persisted conflict.'),
    'TWO_CLOSE_CANDIDATES': (
        'Exactly two genuinely different registrable domains remain and neither '
        'has a qualifying evidence lead.'),
    'MANY_LOW_SIGNAL_CANDIDATES': (
        'Three or more genuinely different domains remain without a qualifying '
        'leader; most support is weak search/name/rank evidence.'),
    'THIRD_PARTY_NOISE_DOMINATES': (
        'Known blocked directory/social/marketplace/media/other operators make up '
        'at least half of all stored candidate hosts and no leader qualifies.'),
    'SAME_DOMAIN_VARIANTS': (
        'The apparent ambiguity collapses to one registrable domain after grouping '
        'root/subdomain variants.'),
    'CROSS_COUNTRY_COLLISION': (
        'Supported candidates span Slovenian and foreign country-code domains, or '
        'multiple foreign country-code domains, without a qualifying leader.'),
    'NO_MEANINGFUL_IDENTITY_SUPPORT': (
        'No plausible candidate has exact identity, attributed contact agreement, '
        'phone agreement, or independent multi-publisher support.'),
}

# Offline, deliberately bounded approximation. Most production candidates use
# .si/.com/.eu; these common compound public suffixes prevent obvious bad grouping
# without downloading or vendoring a mutable public-suffix list.
COMPOUND_SUFFIXES = frozenset({
    'co.uk', 'org.uk', 'ac.uk', 'com.au', 'net.au', 'org.au', 'co.nz',
    'com.tr', 'com.tw', 'com.ua', 'co.za', 'co.kr', 'co.jp', 'com.br',
})
GENERIC_TLDS = frozenset({
    'com', 'org', 'net', 'eu', 'info', 'biz', 'io', 'co', 'online', 'site',
    'shop', 'store', 'agency', 'digital', 'tech', 'app', 'dev', 'pro',
})
KNOWN_THIRD_PARTY_TYPES = frozenset({
    'DIRECTORY_OR_COMPANY_DATABASE', 'SOCIAL_NETWORK',
    'MARKETPLACE_OR_PLATFORM', 'NEWS_OR_MEDIA', 'GOVERNMENT_OR_PUBLIC_REGISTRY',
    'OTHER_REGISTERED_THIRD_PARTY',
})
CANDIDATE_TYPES = (
    'LIKELY_OFFICIAL_OR_BUSINESS_DOMAIN', 'DIRECTORY_OR_COMPANY_DATABASE',
    'SOCIAL_NETWORK', 'MARKETPLACE_OR_PLATFORM', 'NEWS_OR_MEDIA',
    'GOVERNMENT_OR_PUBLIC_REGISTRY', 'OTHER_REGISTERED_THIRD_PARTY',
    'UNCLASSIFIED_DOMAIN',
)
CARDINALITY_BANDS = ('EXACTLY_2', '3_TO_5', '6_TO_10', 'OVER_10')
MEANINGFUL_SUPPORT_KEYS = frozenset({
    'exact_tax', 'exact_registration', 'exact_address', 'exact_street',
    'exact_postal', 'exact_municipality', 'attributed_email_domain_agreement',
    'attributed_phone_agreement',
})


def registrable_domain(host):
    host = normalize_domain(host)
    labels = host.split('.')
    if len(labels) <= 2:
        return host
    suffix = '.'.join(labels[-2:])
    return '.'.join(labels[-3:]) if suffix in COMPOUND_SUFFIXES else suffix


def country_code(host):
    host = normalize_domain(host)
    label = host.rsplit('.', 1)[-1] if '.' in host else ''
    return label if len(label) == 2 and label not in GENERIC_TLDS else ''


def cardinality_band(count):
    if count == 2:
        return 'EXACTLY_2'
    if count <= 5:
        return '3_TO_5'
    if count <= 10:
        return '6_TO_10'
    return 'OVER_10'


def candidate_type(domain, support):
    policy = policy_for_url('https://' + domain + '/')
    classification = policy.classification if policy else ''
    if classification in (DIRECTORY, COMPANY_DATABASE):
        return 'DIRECTORY_OR_COMPANY_DATABASE', classification
    if classification == SOCIAL_PLATFORM:
        return 'SOCIAL_NETWORK', classification
    if classification in (MARKETPLACE, BOOKING_AGGREGATOR, TOURISM_PROFILE,
                          DEALER_PORTAL, JOB_PORTAL):
        return 'MARKETPLACE_OR_PLATFORM', classification
    if classification == MEDIA_NEWS:
        return 'NEWS_OR_MEDIA', classification
    if classification == OTHER_THIRD_PARTY:
        return 'OTHER_REGISTERED_THIRD_PARTY', classification
    if (domain == 'gov.si' or domain.endswith('.gov.si') or
            domain == 'europa.eu' or domain.endswith('.europa.eu')):
        return 'GOVERNMENT_OR_PUBLIC_REGISTRY', 'STRUCTURAL_PUBLIC_SUFFIX'
    strong = (support['exact_tax'] or support['exact_registration'] or
              support['attributed_email_domain_agreement'] or
              (support['exact_legal_name'] and (support['exact_address'] or
               support['exact_street'] or support['independent_publisher_count'] >= 2)))
    if strong:
        return 'LIKELY_OFFICIAL_OR_BUSINESS_DOMAIN', ''
    return 'UNCLASSIFIED_DOMAIN', ''


def _candidate_score(item):
    """Evidence diagnostic only; never consumed by Discovery selection."""
    score = 0
    score += 120 * bool(item['exact_tax'])
    score += 120 * bool(item['exact_registration'])
    score += 60 * bool(item['exact_address'])
    score += 35 * bool(item['exact_street'])
    score += 15 * bool(item['exact_postal'])
    score += 15 * bool(item['exact_municipality'])
    score += 70 * bool(item['attributed_email_domain_agreement'])
    score += 35 * bool(item['attributed_phone_agreement'])
    score += 30 * bool(item['exact_legal_name'])
    score += 10 * bool(item['raw_email_domain_agreement'])
    score += min(item['independent_publisher_count'], 3) * 8
    score += min(item['independent_evidence_count'], 3) * 3
    score += min(item['query_occurrence_count'], 3) * 2
    score += 10 * bool(item['direct_brand_match'])
    score += 5 * bool(item['broad_identity_match'])
    if item['minimum_search_rank']:
        score += max(0, 6 - min(item['minimum_search_rank'], 6))
    score -= 200 * bool(item['identifier_conflict'])
    score -= 80 * bool(item['address_conflict'])
    score -= 80 * bool(item['entity_conflict'])
    score -= 250 * bool(item['blocked_as_official'])
    return score


def _candidate_items(feature):
    attributed = _contact_maps(feature)
    attributed_email_domains = {_email_domain(value) for value in attributed['EMAIL']}
    attributed_email_domains.discard('')
    attributed_phones = set(attributed['PHONE'])
    raw_email_domains = set()
    by_domain = defaultdict(lambda: {
        'urls': set(), 'observation_ids': set(), 'evidence_ids': set(),
        'source_kinds': set(), 'providers': set(), 'publishers': set(),
        'query_occurrences': set(), 'ranks': set(), 'methods': set(),
        'broad_identity_match': False, 'direct_search_domain': False,
    })

    for evidence_id, observations in feature['observations_by_evidence'].items():
        for observation in observations:
            if observation['observation_type'] == 'EMAIL_CANDIDATE':
                domain = _email_domain(observation['normalized_value'])
                if (domain and not public_email_domain(domain) and
                        observation['normalized_value'] not in attributed['EMAIL']):
                    raw_email_domains.add(domain)
            if observation['observation_type'] != 'WEBSITE_CANDIDATE':
                continue
            domain = normalize_domain(observation['normalized_value'])
            if not domain:
                continue
            evidence = feature['evidence'].get(evidence_id, {})
            item = by_domain[domain]
            item['urls'].add(observation['normalized_value'])
            item['observation_ids'].add(observation['observation_id'])
            item['evidence_ids'].add(evidence_id)
            item['source_kinds'].add(evidence.get('source_kind') or 'UNKNOWN')
            item['methods'].add(observation['extraction_method'])
            item['broad_identity_match'] |= observation['value'].get('identity_match') is True
            if evidence.get('provider'):
                item['providers'].add(evidence['provider'])
            publisher = normalize_domain(evidence.get('result_host') or
                                         evidence.get('result_url') or '')
            if publisher:
                item['publishers'].add(publisher)
            if evidence.get('source_kind') == 'SEARCH_RESULT':
                item['query_occurrences'].add((evidence.get('query_type') or '',
                    evidence.get('query_text') or '', evidence_id))
                rank = evidence.get('result_rank')
                if isinstance(rank, int) and rank > 0:
                    item['ranks'].add(rank)
                item['direct_search_domain'] |= (
                    observation['extraction_method'] == 'search_url' and
                    publisher == domain)

    accepted_domain = normalize_domain(feature['accepted_website'])
    items = []
    for domain, gathered in sorted(by_domain.items()):
        local = [observation
                 for evidence_id in gathered['evidence_ids']
                 for observation in feature['observations_by_evidence'][evidence_id]]
        exact = {observation['observation_type'] for observation in local
                 if observation['value'].get('verification_status') == 'EXACT_MATCH'}
        conflicts = {observation['observation_type'] for observation in local
                     if observation['value'].get('verification_status') == 'CONFLICT'}
        local_phones = {observation['normalized_value'] for observation in local
                        if observation['observation_type'] == 'PHONE_CANDIDATE'}
        root = registrable_domain(domain)
        item = {
            'domain': domain, 'registrable_domain': root,
            'urls': sorted(gathered['urls'])[:8],
            'url_count': len(gathered['urls']),
            'observation_ids': sorted(gathered['observation_ids']),
            'evidence_ids': sorted(gathered['evidence_ids']),
            'source_kinds': sorted(gathered['source_kinds']),
            'providers': sorted(gathered['providers']),
            'publishers': sorted(gathered['publishers']),
            'methods': sorted(gathered['methods']),
            'independent_evidence_count': len(gathered['evidence_ids']),
            'independent_publisher_count': len(gathered['publishers']),
            'query_occurrence_count': len(gathered['query_occurrences']),
            'minimum_search_rank': min(gathered['ranks']) if gathered['ranks'] else None,
            'exact_tax': 'TAX_NUMBER' in exact,
            'exact_registration': 'REGISTRATION_NUMBER' in exact,
            'exact_legal_name': 'LEGAL_NAME' in exact,
            'exact_address': 'ADDRESS' in exact,
            'exact_street': 'STREET' in exact,
            'exact_postal': 'POSTAL_CODE' in exact,
            'exact_municipality': 'MUNICIPALITY' in exact,
            'identifier_conflict': bool(conflicts & {'TAX_NUMBER','REGISTRATION_NUMBER'}),
            'address_conflict': bool(conflicts & {'ADDRESS','STREET','POSTAL_CODE','MUNICIPALITY'}),
            'entity_conflict': bool(conflicts & {'LEGAL_NAME','SITE_OPERATOR'}),
            'attributed_email_domain_agreement': any(
                registrable_domain(value) == root for value in attributed_email_domains),
            'raw_email_domain_agreement': any(
                registrable_domain(value) == root for value in raw_email_domains),
            'attributed_phone_agreement': bool(local_phones & attributed_phones),
            'broad_identity_match': gathered['broad_identity_match'],
            'direct_search_domain': gathered['direct_search_domain'],
            'direct_brand_match': (gathered['direct_search_domain'] and
                                   brand_match(feature['company'], 'https://'+domain+'/')),
            'blocked_as_official': blocks_official('https://' + domain + '/'),
            'is_accepted_domain': domain == accepted_domain,
            'country_code': country_code(domain),
        }
        kind, policy = candidate_type(domain, item)
        item['candidate_type'] = kind
        item['policy_classification'] = policy
        item['foreign_country_domain'] = bool(item['country_code'] and
                                              item['country_code'] != 'si')
        item['name_or_rank_only'] = not any(item[key] for key in MEANINGFUL_SUPPORT_KEYS) and (
            item['exact_legal_name'] or item['broad_identity_match'] or
            item['direct_brand_match'] or item['minimum_search_rank'] is not None)
        item['meaningful_identity_support'] = bool(
            any(item[key] for key in MEANINGFUL_SUPPORT_KEYS) or
            (item['exact_legal_name'] and item['independent_publisher_count'] >= 2))
        item['score'] = _candidate_score(item)
        items.append(item)
    return items


def _diagnostic_category(items):
    plausible = [item for item in items
                 if not item['blocked_as_official'] and not item['is_accepted_domain']]
    roots = {item['registrable_domain'] for item in plausible}
    ordered = sorted(plausible, key=lambda item: (-item['score'], item['domain']))
    leader = ordered[0]
    runner_score = ordered[1]['score'] if len(ordered) > 1 else 0
    margin = leader['score'] - runner_score
    conflict = (leader['identifier_conflict'] or leader['address_conflict'] or
                leader['entity_conflict'])
    clear_basis = (leader['exact_tax'] or leader['exact_registration'] or
        (leader['attributed_email_domain_agreement'] and
         (leader['exact_legal_name'] or leader['exact_address'] or
          leader['independent_publisher_count'] >= 2)))
    moderate_basis = (leader['attributed_email_domain_agreement'] or
        leader['exact_address'] or leader['exact_street'] or
        (leader['exact_legal_name'] and leader['independent_publisher_count'] >= 2))
    known_third_party = sum(item['candidate_type'] in KNOWN_THIRD_PARTY_TYPES
                            for item in items)
    country_codes = {item['country_code'] for item in plausible
                     if item['country_code'] and
                     (item['exact_legal_name'] or item['broad_identity_match'])}

    if len(roots) == 1:
        category = 'SAME_DOMAIN_VARIANTS'
    elif clear_basis and not conflict and margin >= 50:
        category = 'ONE_CLEAR_EVIDENCE_LEADER'
    elif moderate_basis and not conflict and leader['score'] >= 45 and margin >= 20:
        category = 'ONE_MODERATE_LEADER'
    elif len(country_codes) >= 2 and ('si' in country_codes or len(country_codes) > 1):
        category = 'CROSS_COUNTRY_COLLISION'
    elif items and known_third_party * 2 >= len(items):
        category = 'THIRD_PARTY_NOISE_DOMINATES'
    elif not any(item['meaningful_identity_support'] for item in plausible):
        category = 'NO_MEANINGFUL_IDENTITY_SUPPORT'
    elif len(roots) == 2:
        category = 'TWO_CLOSE_CANDIDATES'
    else:
        category = 'MANY_LOW_SIGNAL_CANDIDATES'
    return category, leader, runner_score, margin, plausible, roots


def _company_row(feature):
    items = _candidate_items(feature)
    category, leader, runner_score, margin, plausible, roots = _diagnostic_category(items)
    company = feature['company']
    type_counts = Counter(item['candidate_type'] for item in items)
    negative_counts = Counter()
    for item in items:
        if item['blocked_as_official']:
            negative_counts['BLOCKED_OPERATOR_DOMAIN'] += 1
        if item['identifier_conflict']:
            negative_counts['IDENTIFIER_CONFLICT'] += 1
        if item['address_conflict']:
            negative_counts['ADDRESS_CONFLICT'] += 1
        if item['entity_conflict']:
            negative_counts['ENTITY_CONFLICT'] += 1
        if item['foreign_country_domain']:
            negative_counts['FOREIGN_COUNTRY_DOMAIN'] += 1
        if item['name_or_rank_only']:
            negative_counts['NAME_OR_SEARCH_HEURISTIC_ONLY'] += 1
        mapping = {'SOCIAL_NETWORK':'GENERIC_SOCIAL_OR_PROFILE',
                   'NEWS_OR_MEDIA':'MEDIA_OR_ARTICLE',
                   'MARKETPLACE_OR_PLATFORM':'MARKETPLACE_OR_PLATFORM'}
        if item['candidate_type'] in mapping:
            negative_counts[mapping[item['candidate_type']]] += 1
    roots_to_hosts = defaultdict(set)
    for item in plausible:
        roots_to_hosts[item['registrable_domain']].add(item['domain'])
    same_variants = sorted(root for root, hosts in roots_to_hosts.items() if len(hosts) > 1)
    return {
        'company_id': company['id'], 'company_name': company['company_name'] or '',
        'tax_number': company['tax_number'] or '',
        'registration_number': company['registration_number'] or '',
        'address': company['address'] or '', 'municipality': company['municipality'] or '',
        'attempt_id': feature['attempt_id'],
        'accepted_website': feature['accepted_website'],
        'selected_result_id': feature['selected_result_id'],
        'selected_result_attempt_id': feature['selected_result_attempt_id'],
        'selected_website_status': feature['selected_website_status'],
        'selected_contact_outcome': feature['selected_contact_outcome'],
        'plausible_host_count': len(plausible),
        'plausible_registrable_domain_count': len(roots),
        'all_candidate_host_count': len(items),
        'all_candidate_url_count': sum(item['url_count'] for item in items),
        'same_host_url_variant_domain_count': sum(
            item['url_count'] > 1 for item in items),
        'candidate_cardinality_band': cardinality_band(len(plausible)),
        'diagnostic_category': category,
        'leader_domain': leader['domain'], 'leader_score': leader['score'],
        'runner_up_score': runner_score, 'leader_margin': margin,
        'same_domain_variants': '|'.join(same_variants),
        'foreign_country_candidate_count': sum(item['foreign_country_domain'] for item in items),
        'known_third_party_candidate_count': sum(
            item['candidate_type'] in KNOWN_THIRD_PARTY_TYPES for item in items),
        'meaningful_identity_candidate_count': sum(
            item['meaningful_identity_support'] for item in plausible),
        'candidate_type_counts_json': json.dumps(dict(sorted(type_counts.items())),
            sort_keys=True, separators=(',', ':')),
        'negative_signal_counts_json': json.dumps(dict(sorted(negative_counts.items())),
            sort_keys=True, separators=(',', ':')),
        'candidate_details_json': json.dumps(items, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')),
    }


def deterministic_sample(rows, sample_size=100, seed='ambiguity-v1'):
    groups = defaultdict(list)
    for row in rows:
        groups[row['diagnostic_category']].append(row)
    for category in groups:
        groups[category].sort(key=lambda row: hashlib.sha256(
            f'{seed}:{row["company_id"]}'.encode()).hexdigest())
    selected = []
    categories = sorted(groups)
    index = 0
    while len(selected) < min(sample_size, len(rows)):
        progressed = False
        for category in categories:
            if index < len(groups[category]):
                selected.append({**groups[category][index],
                    'sample_seed': str(seed), 'sample_stratum': category})
                progressed = True
                if len(selected) == min(sample_size, len(rows)):
                    break
        if not progressed:
            break
        index += 1
    return selected


def build_ambiguity_audit(master, results, run_id, sample_size=100,
                          seed='ambiguity-v1'):
    master_path, companies, _ = load_master(master)
    current = load_v2(results, run_id, 'CURRENT_V2', 'CURRENT')
    validate_master_identities(companies, [current])
    features = _load_detailed_features(results, run_id, companies, current['records'])
    ambiguous = []
    for _, feature in sorted(features.items()):
        recovery = _row(feature)
        if feature['status'] == 'PARTIAL' and recovery['recovery_bucket'] == (
                'MULTIPLE_AMBIGUOUS_CANDIDATES'):
            ambiguous.append(_company_row(feature))
    samples = deterministic_sample(ambiguous, sample_size, seed)
    category_counts = Counter(row['diagnostic_category'] for row in ambiguous)
    cardinality = Counter(row['candidate_cardinality_band'] for row in ambiguous)
    type_counts = Counter()
    type_companies = Counter()
    negative_counts = Counter()
    negative_companies = Counter()
    category_cardinality = defaultdict(Counter)
    category_type_presence = defaultdict(Counter)
    for row in ambiguous:
        row_types = json.loads(row['candidate_type_counts_json'])
        row_negatives = json.loads(row['negative_signal_counts_json'])
        type_counts.update(row_types)
        type_companies.update(row_types.keys())
        negative_counts.update(row_negatives)
        negative_companies.update(row_negatives.keys())
        category = row['diagnostic_category']
        category_cardinality[category][row['candidate_cardinality_band']] += 1
        category_type_presence[category].update(row_types.keys())
    summary = {
        'audit_version': 1,
        'inputs': {'master': str(master_path), 'results': current['path'],
                   'run_id': run_id},
        'ambiguous_partial_companies': len(ambiguous),
        'candidate_cardinality': {key:cardinality[key] for key in CARDINALITY_BANDS},
        'diagnostic_categories': {key:category_counts[key]
                                  for key in DIAGNOSTIC_CATEGORIES},
        'candidate_type_occurrences': {key:type_counts[key] for key in CANDIDATE_TYPES},
        'companies_by_candidate_type': {key:type_companies[key]
                                        for key in CANDIDATE_TYPES},
        'negative_signal_occurrences': dict(sorted(negative_counts.items())),
        'companies_by_negative_signal': dict(sorted(negative_companies.items())),
        'category_by_cardinality': {key:dict(sorted(value.items())) for key,value
            in sorted(category_cardinality.items())},
        'category_by_candidate_type_presence': {
            key:dict(sorted(value.items())) for key,value
            in sorted(category_type_presence.items())},
        'same_registrable_domain_only': sum(
            row['diagnostic_category'] == 'SAME_DOMAIN_VARIANTS' for row in ambiguous),
        'companies_with_same_host_url_variants': sum(
            bool(row['same_host_url_variant_domain_count']) for row in ambiguous),
        'companies_with_known_third_party_candidates': sum(
            bool(row['known_third_party_candidate_count']) for row in ambiguous),
        'companies_with_foreign_country_candidates': sum(
            bool(row['foreign_country_candidate_count']) for row in ambiguous),
        'one_clear_or_moderate_leader': sum(row['diagnostic_category'] in (
            'ONE_CLEAR_EVIDENCE_LEADER','ONE_MODERATE_LEADER') for row in ambiguous),
        'sample': {'requested': sample_size, 'produced': len(samples),
                   'seed': str(seed), 'method': 'deterministic category round-robin'},
        'definitions': {
            'categories': DIAGNOSTIC_CATEGORIES,
            'candidate_score_is_acceptance': False,
            'leader_is_official_website': False,
            'known_policy_classification_is_acceptance': False,
            'registrable_domain_method': ('offline bounded compound-suffix grouping; '
                'not a downloaded public-suffix list'),
        },
    }
    return ambiguous, samples, summary


def human_report(summary):
    lines = ['# Discovery ambiguity breakdown', '',
        'All leaders and scores are diagnostic only. No website is accepted or promoted.', '',
        f"- Ambiguous PARTIAL companies: {summary['ambiguous_partial_companies']}",
        f"- Clear/moderate evidence leaders: {summary['one_clear_or_moderate_leader']}",
        f"- Same-registrable-domain variants: {summary['same_registrable_domain_only']}",
        f"- Companies with same-host URL/path variants: {summary['companies_with_same_host_url_variants']}",
        f"- Companies with known third-party candidates: {summary['companies_with_known_third_party_candidates']}",
        f"- Companies with foreign-country candidates: {summary['companies_with_foreign_country_candidates']}",
        '', '## Diagnostic categories', '']
    lines.extend(f'- {key}: {value}' for key, value in
                 summary['diagnostic_categories'].items())
    lines.extend(['', '## Candidate cardinality', ''])
    lines.extend(f'- {key}: {value}' for key, value in
                 summary['candidate_cardinality'].items())
    lines.extend(['', '## Candidate types', ''])
    lines.extend(f'- {key}: {value}' for key, value in
                 summary['candidate_type_occurrences'].items())
    lines.extend(['', '## Interpretation', '',
        '- Candidate collection is intentionally lossless and broader than selection.',
        '- Registry classifications and negative signals suppress diagnostic confidence only.',
        '- Exact identity and attributable contact evidence dominate weak search heuristics.',
        '- UNKNOWN/PARTIAL remains the safe outcome until a future resolver is validated.', ''])
    return '\n'.join(lines)


def _atomic_text(path, value, overwrite):
    if path.exists() and not overwrite:
        raise ValueError(f'Output already exists (use --overwrite): {path}')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp',
                                     dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(value)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_reports(output_dir, rows, samples, summary, inputs, overwrite=False):
    output = Path(output_dir).expanduser().absolute()
    resolved_inputs = {Path(value).expanduser().resolve() for value in inputs}
    if output.resolve() in resolved_inputs:
        raise ValueError('Output directory cannot be a SQLite input')
    output.mkdir(parents=True, exist_ok=True)
    paths = {name: output/name for name in OUTPUT_NAMES}
    if not overwrite and any(path.exists() for path in paths.values()):
        raise ValueError('Ambiguity output already exists; choose a new directory or use --overwrite')
    if any(path.resolve() in resolved_inputs for path in paths.values()):
        raise ValueError('Output report cannot overwrite a SQLite input')
    _atomic_json(paths['ambiguity_summary.json'], summary, overwrite)
    _atomic_csv(paths['ambiguity_companies.csv'], rows, COMPANY_HEADERS, overwrite)
    _atomic_csv(paths['ambiguity_sample_100.csv'], samples,
                COMPANY_HEADERS + ('sample_seed','sample_stratum'), overwrite)
    _atomic_text(paths['AMBIGUITY_REPORT.md'], human_report(summary), overwrite)
    return {name: str(path) for name, path in paths.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--master', required=True)
    parser.add_argument('--results', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--sample-size', type=int, default=100)
    parser.add_argument('--seed', default='ambiguity-v1')
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    if args.sample_size < 1:
        parser.error('--sample-size must be positive')
    try:
        rows, samples, summary = build_ambiguity_audit(
            args.master, args.results, args.run_id, args.sample_size, args.seed)
        paths = write_reports(args.output_dir, rows, samples, summary,
                              (args.master, args.results), args.overwrite)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f'error: {exc}\n')
    print('AMBIGUITY=' + json.dumps(summary['diagnostic_categories'],
          sort_keys=True, separators=(',', ':')))
    for name, path in paths.items():
        print(f'{name.upper().replace(".", "_")}={path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
