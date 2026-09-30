"""Categorical resolution of official domains from persisted search evidence.

This resolver establishes a target-to-domain association.  It deliberately
does not classify fetched HTML ownership and does not treat historical status,
generated guesses, or publisher repetition as authorization.
"""
from discovery.domain_generator import normalize_domain, normalize_url
from discovery.domain_policy import blocks_official
from discovery_v2.candidates import brand_match, eligible, fused_candidates
from discovery_v2.ownership import publisher_id

RESOLVED = 'RESOLVED'
REVIEW = 'REVIEW'
NO_SUPPORTED_WEBSITE = 'NO_SUPPORTED_WEBSITE'
RULE_DIRECT = 'SER-01_DIRECT_FIRST_PARTY_IDENTITY'
RULE_PUBLISHERS = 'SER-02_INDEPENDENT_PUBLISHER_CONVERGENCE'
RULE_EMAIL = 'SER-03_WEBSITE_EMAIL_DOMAIN_CONVERGENCE'


def _current_candidates(company, observations, evidence_rows):
    candidates = []
    for bundle in fused_candidates(company, observations, evidence_rows):
        domain = bundle['domain']
        evidence = bundle['evidence']
        source_only = blocks_official('https://' + domain + '/')
        bundle_evidence_ids = {o['evidence_id'] for o in bundle['observations']}
        entity_conflict = any(
            o.get('evidence_id') in bundle_evidence_ids
            and o.get('observation_type') in ('LEGAL_NAME', 'SITE_OPERATOR')
            and o.get('value', {}).get('verification_status') == 'CONFLICT'
            for o in observations)
        exact_name_evidence = {o['evidence_id'] for o in observations
                               if ((o.get('observation_type') == 'LEGAL_NAME'
                                    and o.get('value', {}).get('verification_status') == 'EXACT_MATCH')
                                   or (o.get('observation_type') == 'COMPANY_IDENTITY'
                                       and o.get('value', {}).get('identity_match')))}
        identity = [o for o in bundle['observations']
                    if (o.get('value', {}).get('identity_match')
                        or o.get('evidence_id') in exact_name_evidence)]
        assertions = []
        for observation in identity:
            row = evidence_rows.get(observation['evidence_id'], {})
            if not eligible(company, observation['normalized_value'],
                            observation.get('value', {}).get('title', ''),
                            observation.get('value', {}).get('body', '')):
                continue
            method = observation.get('extraction_method')
            publisher = publisher_id(row.get('result_host') or row.get('result_url'))
            kind = ('DIRECT_SEARCH_DOMAIN' if method == 'search_url' and
                    normalize_domain(row.get('result_url')) == domain else
                    'SNIPPET_URL' if method == 'snippet_url' else
                    'SNIPPET_EMAIL_DOMAIN' if method == 'email_domain' else None)
            if kind:
                assertions.append((kind, publisher, observation['evidence_id'],
                                   observation['observation_id']))
        explicit_publishers = {p for k, p, _, _ in assertions
                               if k in ('DIRECT_SEARCH_DOMAIN', 'SNIPPET_URL') and p}
        email_publishers = {p for k, p, _, _ in assertions
                            if k == 'SNIPPET_EMAIL_DOMAIN' and p}
        direct = (brand_match(company, 'https://' + domain + '/') and
                  any(k == 'DIRECT_SEARCH_DOMAIN' and p == domain
                      for k, p, _, _ in assertions))
        independent_external = {p for p in explicit_publishers if p != domain}
        independent_email = {p for p in email_publishers
                             if p != domain and p not in explicit_publishers}
        if source_only or evidence.get('identifier_conflict') or entity_conflict:
            rule = None
        elif direct:
            rule = RULE_DIRECT
        elif len(independent_external) >= 2:
            rule = RULE_PUBLISHERS
        elif independent_external and independent_email:
            rule = RULE_EMAIL
        else:
            rule = None
        decisive = []
        decisive_observations = []
        if rule == RULE_DIRECT:
            chosen = [a for a in assertions if a[0] == 'DIRECT_SEARCH_DOMAIN' and a[1] == domain]
        elif rule == RULE_PUBLISHERS:
            chosen = [a for a in assertions if a[0] == 'SNIPPET_URL' and a[1] in independent_external]
        elif rule == RULE_EMAIL:
            chosen = [a for a in assertions if (a[0] == 'SNIPPET_URL' and a[1] in independent_external)
                      or (a[0] == 'SNIPPET_EMAIL_DOMAIN' and a[1] in independent_email)]
        else:
            chosen = []
        seen_publishers = set()
        for kind, publisher, evidence_id, observation_id in sorted(chosen):
            key = (kind, publisher)
            if key in seen_publishers:
                continue
            seen_publishers.add(key)
            decisive.append(evidence_id)
            decisive_observations.append(observation_id)
        candidates.append({
            'candidate_domain': domain,
            'candidate_url': normalize_url(bundle['observation']['normalized_value']),
            'observation_id': bundle['observation']['observation_id'],
            'candidate_evidence_id': bundle['observation']['evidence_id'],
            'resolution_rule': rule,
            'decisive_evidence_ids': sorted(set(decisive)),
            'decisive_observation_ids': sorted(set(decisive_observations)),
            'independent_publishers': sorted(set(explicit_publishers | email_publishers)),
            'evidence_types': sorted({a[0] for a in assertions}),
            'conflicts': (['EXACT_IDENTIFIER_CONFLICT'] if evidence.get('identifier_conflict') else []) +
                         (['ENTITY_OPERATOR_CONFLICT'] if entity_conflict else []) +
                         (['SOURCE_ONLY_HOST'] if source_only else []),
            'current_evidence': bool(assertions),
        })
    return candidates


def resolve_search_evidence(company, observations, evidence_rows, historical=()):
    """Resolve persisted evidence without fetching or using historical status."""
    candidates = _current_candidates(company, observations, evidence_rows)
    by_domain = {item['candidate_domain']: item for item in candidates}
    for item in historical:
        domain = normalize_domain(item.get('url') or item.get('website') or '')
        if not domain or blocks_official('https://' + domain + '/'):
            continue
        candidate = by_domain.get(domain)
        if candidate is None:
            candidate = {
                'candidate_domain': domain,
                'candidate_url': normalize_url(item.get('url') or item.get('website')),
                'observation_id': None, 'candidate_evidence_id': None,
                'resolution_rule': None, 'decisive_evidence_ids': [],
                'decisive_observation_ids': [], 'independent_publishers': [],
                'evidence_types': [], 'conflicts': [], 'current_evidence': False}
            by_domain[domain] = candidate
            candidates.append(candidate)
        candidate['evidence_types'] = sorted(set(candidate['evidence_types']) |
                                             {'HISTORICAL_WEBSITE'})

    authorized = [item for item in candidates if item['resolution_rule']]
    priority = {RULE_DIRECT: 3, RULE_PUBLISHERS: 2, RULE_EMAIL: 1}
    best_priority = max((priority[item['resolution_rule']] for item in authorized),
                        default=0)
    strongest = [item for item in authorized
                 if priority[item['resolution_rule']] == best_priority]
    # Rules are categorical. Multiple independently authorized domains cannot
    # be broken by ranks, spelling similarity, or historical status.
    if len(strongest) == 1:
        winner = strongest[0]
        status = RESOLVED
    elif len(strongest) > 1:
        winner = None
        status = REVIEW
    elif any(item['current_evidence'] for item in candidates):
        winner = None
        status = REVIEW
    else:
        winner = None
        status = NO_SUPPORTED_WEBSITE
    alternatives = sorted(item['candidate_domain'] for item in candidates
                          if winner is None or item['candidate_domain'] != winner['candidate_domain'])
    return {
        'resolver_version': 'search-evidence-resolver-v2',
        'status': status,
        'candidate_domain': winner['candidate_domain'] if winner else None,
        'candidate_url': winner['candidate_url'] if winner else None,
        'observation_id': winner['observation_id'] if winner else None,
        'candidate_evidence_id': winner['candidate_evidence_id'] if winner else None,
        'resolution_rule': winner['resolution_rule'] if winner else None,
        'decisive_evidence_ids': winner['decisive_evidence_ids'] if winner else [],
        'decisive_observation_ids': winner['decisive_observation_ids'] if winner else [],
        'independent_publishers': winner['independent_publishers'] if winner else [],
        'evidence_types': winner['evidence_types'] if winner else [],
        'conflicts': sorted({conflict for item in candidates for conflict in item['conflicts']}),
        'alternative_candidate_domains': alternatives,
        'candidates': sorted(candidates, key=lambda item: item['candidate_domain']),
    }


def assessment_from_resolution(resolution):
    """Create a separately identified HIGH authorization for runner/storage."""
    scope = resolution['candidate_url']
    decision = {
        'status': 'HIGH', 'verified': False, 'usable': True,
        'rule_id': None, 'confidence_rule_id': None,
        'authorization_basis': 'SEARCH_EVIDENCE_RESOLVER_V2',
        'verification_scope': scope, 'relationship': 'STANDALONE',
        'supporting_evidence_ids': resolution['decisive_evidence_ids'],
        'supporting_observation_ids': resolution['decisive_observation_ids'],
        'blockers': [], 'conflicts': [], 'search_resolution': resolution,
    }
    return {
        'verified': False, 'usable': True, 'status': 'HIGH',
        'relationship': 'STANDALONE', 'verified_scope': scope, 'final_url': scope,
        'p1a': decision,
        'ownership': {'status': 'HIGH', 'scope': scope, 'relationship': 'STANDALONE',
                      'reasons': [resolution['resolution_rule']],
                      'ownership_evidence': resolution['decisive_evidence_ids']},
        'search_resolution': resolution,
    }


def validate_resolution_assessment(company, candidate_url, assessment, writer):
    decision = assessment.get('p1a') or {}
    original = decision.get('search_resolution') or assessment.get('search_resolution')
    if decision.get('authorization_basis') != 'SEARCH_EVIDENCE_RESOLVER_V2' or not original:
        return False
    current = resolve_search_evidence(company, writer.observations, writer.evidence_rows)
    return bool(current['status'] == RESOLVED
                and current['candidate_domain'] == normalize_domain(candidate_url)
                and current['candidate_domain'] == original.get('candidate_domain')
                and current['resolution_rule'] == original.get('resolution_rule')
                and current['decisive_evidence_ids'] == original.get('decisive_evidence_ids')
                and current['decisive_observation_ids'] == original.get('decisive_observation_ids')
                and assessment.get('status') == 'HIGH'
                and assessment.get('verified_scope') == decision.get('verification_scope'))
