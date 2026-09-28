"""Explicit boundary to stateless SPARROW fetch/verification logic.

Never imports its runner, database helpers or website-result reuse path.
"""
from discovery.website_verifier import Fetcher, RULE_VERSION, verify

VERIFIER_VERSION = RULE_VERSION + "+v2-brand-2"


def new_fetcher(config):
    return ScopedFetcher(max_requests=config.max_http_requests)


def evaluate_website(company, url, fetcher):
    result = verify(company, url, fetcher=fetcher)
    if result.get('verified'):
        return result
    from discovery_v2.candidates import brand_match, eligible
    from discovery.ownership import scope_url, name, text
    from discovery.domain_generator import normalize_domain
    for page in result.get('evidence', []):
        page_url = page['page_url']
        signals = set(page.get('signals', []))
        facts = page.get('ownership', {})
        labels = normalize_domain(page_url).split('.')
        branded_parent = brand_match(company, 'https://' + '.'.join(labels[-2:]) + '/')
        strong = bool(signals & {'tax_exact', 'registration_exact'}) and bool(signals & {'street_address_exact', 'postal_locality_exact'})
        strong |= {'company_name_exact', 'street_address_exact', 'postal_locality_exact'} <= signals
        if (strong and branded_parent and brand_match(company, page_url) and eligible(company, page_url, page.get('page_title', ''))
                and facts.get('page_type') == 'COMPANY_PAGE' and (facts.get('site_brand_match') or set(name(company['company_name']).split()) <= set(text(page.get('page_title', '')).split()))
                and not facts.get('legal_conflict') and not page.get('third_party')
                and result.get('relationship') not in ('THIRD_PARTY', 'GROUP_PARENT')):
            scope = scope_url(page_url)
            owner = dict(status='VERIFIED', scope=scope, relationship='LEGAL_ENTITY',
                         reasons=['V2 brand tokens/subdomain with strong independent identity'],
                         ownership_evidence=[dict(page_url=page_url, basis='v2_corroborated_brand', scope=scope)])
            result.update(verified=True, status='VERIFIED', verified_scope=scope, final_url=scope, ownership=owner)
            return result
    return result


class ScopedFetcher(Fetcher):
    """Keep request budget global, access-denial blocking local to a host."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.blocked_hosts = set()

    def fetch(self, url, allowed_site=None):
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname
        if host in self.blocked_hosts:
            return None
        self.blocked = False
        before = len(self.errors)
        response = super().fetch(url, allowed_site)
        if self.blocked:
            for error in self.errors[before:]:
                if error.startswith(('Access denied', 'Access challenge')):
                    failed_url = error.split(': ', 1)[-1]
                    self.blocked_hosts.add(urlsplit(failed_url).hostname)
        self.blocked = False
        return response


class RecordingFetcher:
    def __init__(self, fetcher, writer):
        self.inner, self.writer = fetcher, writer
        self.responses = {}
        self.failures = {}

    @property
    def requests(self):
        return self.inner.requests

    @property
    def errors(self):
        return self.inner.errors

    def fetch(self, url, allowed_site=None):
        before = len(self.errors)
        response = self.inner.fetch(url, allowed_site=allowed_site)
        if response is not None:
            self.writer.page(url, response)
            self.responses[response.url] = response
            self.failures.pop(url, None)
        elif len(self.errors) > before:
            # Keep diagnostics when a later contact crawl sees the cached failure.
            self.failures[url] = list(self.errors[before:])
        return response

    def close(self):
        self.inner.close()
