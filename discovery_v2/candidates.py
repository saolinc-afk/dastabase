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
    return not blocks_official(url) and page_type(url, title, body) not in ('THIRD_PARTY', 'PROFILE', 'GROUP') and not any(w in label for w in excluded)


def rank(company, observation):
    url = observation['normalized_value']
    value = observation.get('value', {})
    return (not brand_match(company, url),
            observation['extraction_method'] == 'domain_guess',
            not any(w in urlsplit(url).path.lower() for w in ('kontakt', 'contact')),
            not value.get('identity_match', False),
            {'si': 0, 'com': 1, 'eu': 2}.get(normalize_domain(url).split('.')[-1], 3), len(url))
