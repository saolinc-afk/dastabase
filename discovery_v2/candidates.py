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
