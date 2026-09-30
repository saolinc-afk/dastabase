"""Company-relevant candidate ordering and narrow independent ownership evidence."""
import re
from urllib.parse import urlsplit
from discovery.domain_generator import normalize_domain
from discovery.domain_policy import blocks_official
from discovery.ownership import name, text, page_type


def brand_match(company, url):
    words = name(company['company_name']).split()
    labels = normalize_domain(url).split('.')
    labels = labels[:-1]
    target = ''.join(words)
    return any(label.replace('-', '') == target or
               sorted(re.findall(r'[a-z0-9]+', label)) == sorted(words)
               for label in labels)


def eligible(company, url, title='', body=''):
    label = text(url + ' ' + title + ' ' + body[:400])
    excluded = ('mojedelo', 'bettercareer', 'job portal', 'jobs', 'izkusnje zaposlenih',
                'novice', 'news', 'company profile', 'business directory', 'company directory')
    # Group candidates reach the relationship-aware evaluator; group roots still
    # cannot verify without an exact bounded subsidiary rule.
    return not blocks_official(url) and page_type(url, title, body) not in ('THIRD_PARTY', 'PROFILE') and not any(w in label for w in excluded)


def rank(company, observation):
    url = observation['normalized_value']
    value = observation.get('value', {})
    return (observation['extraction_method'] == 'domain_guess',
            not value.get('identity_match', False),
            not brand_match(company, url),
            not any(w in urlsplit(url).path.lower() for w in ('kontakt', 'contact')),
            not normalize_domain(url).endswith('.si'), len(url))


def _publisher(row):
    return normalize_domain(row.get('result_host') or row.get('result_url') or '')


def _candidate_type(observation, row):
    method = observation.get('extraction_method')
    domain = normalize_domain(observation.get('normalized_value'))
    if method == 'search_url' and domain == _publisher(row):
        return 'DIRECT_SEARCH_DOMAIN'
    if method == 'snippet_url':
        return 'SNIPPET_URL'
    if method == 'email_domain':
        return 'SNIPPET_EMAIL_DOMAIN'
    return 'GENERATED_CANDIDATE' if method == 'domain_guess' else 'OTHER'


def fused_candidates(company, observations, evidence_rows):
    """Return one deterministically ranked evidence bundle per candidate domain.

    Repetition from one publisher is collapsed into sets. The ordering uses
    evidence tiers rather than an additive score, and never authorizes a site.
    """
    all_observations = list(observations)
    candidates = [o for o in all_observations
                  if o.get('observation_type') == 'WEBSITE_CANDIDATE'
                  and normalize_domain(o.get('normalized_value'))]
    grouped = {}
    for observation in candidates:
        domain = normalize_domain(observation['normalized_value'])
        grouped.setdefault(domain, []).append(observation)

    bundles = []
    for domain, items in grouped.items():
        usable_items = [o for o in items if eligible(
            company, o['normalized_value'], o.get('value', {}).get('title', ''),
            o.get('value', {}).get('body', ''))]
        pool = usable_items or items
        evidence_ids = {o['evidence_id'] for o in pool}
        related = [o for o in all_observations if o.get('evidence_id') in evidence_ids]
        conflict = any(o.get('observation_type') in ('TAX_NUMBER', 'REGISTRATION_NUMBER')
                       and o.get('value', {}).get('verification_status') == 'CONFLICT'
                       for o in related)
        identity_items = [o for o in pool if o.get('value', {}).get('identity_match')]
        types = {_candidate_type(o, evidence_rows.get(o['evidence_id'], {}))
                 for o in identity_items}
        publishers = {_publisher(evidence_rows.get(o['evidence_id'], {}))
                      for o in identity_items
                      if _publisher(evidence_rows.get(o['evidence_id'], {}))}
        direct_identity = any(_candidate_type(
            o, evidence_rows.get(o['evidence_id'], {})) == 'DIRECT_SEARCH_DOMAIN'
            for o in identity_items)
        direct = any(_candidate_type(o, evidence_rows.get(o['evidence_id'], {}))
                     == 'DIRECT_SEARCH_DOMAIN' for o in pool)
        website_email_convergence = (
            'SNIPPET_EMAIL_DOMAIN' in types
            and bool(types.intersection(('SNIPPET_URL', 'DIRECT_SEARCH_DOMAIN'))))
        independent = len(publishers) >= 2
        varied = len(types) >= 2

        def representative_key(o):
            row = evidence_rows.get(o['evidence_id'], {})
            kind = _candidate_type(o, row)
            result_rank = row.get('result_rank')
            return (not o.get('value', {}).get('identity_match', False),
                    {'DIRECT_SEARCH_DOMAIN': 0, 'SNIPPET_URL': 1,
                     'SNIPPET_EMAIL_DOMAIN': 2, 'OTHER': 3,
                     'GENERATED_CANDIDATE': 4}.get(kind, 5),
                    result_rank if isinstance(result_rank, int) and result_rank > 0 else 10_000,
                    rank(company, o), o['normalized_value'], o['observation_id'])

        ordered_items = tuple(sorted(pool, key=representative_key))
        representative = ordered_items[0]
        result_ranks = [evidence_rows.get(o['evidence_id'], {}).get('result_rank')
                        for o in identity_items or pool]
        best_rank = min((r for r in result_ranks
                         if isinstance(r, int) and r > 0), default=10_000)
        tier = (
            conflict,
            not (direct_identity and (independent or varied)),
            not direct_identity,
            not website_email_convergence,
            not independent,
            not varied,
            not bool(identity_items),
            not direct,
            best_rank,
            not brand_match(company, representative['normalized_value']),
            not domain.endswith('.si'),
            rank(company, representative),
            domain,
        )
        bundles.append({'domain': domain, 'observation': representative,
            'observations': ordered_items, 'sort_key': tier,
            'evidence': {'identity_match': bool(identity_items),
                         'identifier_conflict': conflict,
                         'independent_publishers': sorted(publishers),
                         'evidence_types': sorted(types),
                         'direct_search_domain': direct,
                         'direct_identity_match': direct_identity,
                         'website_email_convergence': website_email_convergence,
                         'best_result_rank': None if best_rank == 10_000 else best_rank,
                         'supporting_evidence_ids': sorted(evidence_ids)}})
    return sorted(bundles, key=lambda bundle: bundle['sort_key'])


def primary_rank(company, observation, assessment, evidence_row=None):
    """Prefer the best direct entity site after candidates verify independently."""
    relationship = assessment.get('relationship') or 'AMBIGUOUS'
    direct = relationship in ('STANDALONE', 'BRAND_OF_ENTITY')
    result_rank = (evidence_row or {}).get('result_rank')
    scope = assessment.get('verified_scope') or observation.get('normalized_value', '')
    return (
        not direct,
        {'VERIFIED': 0, 'HIGH': 1, 'MEDIUM': 2}.get(assessment.get('status'), 3),
        not brand_match(company, scope),
        observation.get('extraction_method') == 'domain_guess',
        not observation.get('value', {}).get('identity_match', False),
        observation.get('extraction_method') in ('snippet_url', 'email_domain'),
        result_rank if isinstance(result_rank, int) and result_rank > 0 else 10_000,
        not normalize_domain(scope).endswith('.si'),
        len(scope),
    )
