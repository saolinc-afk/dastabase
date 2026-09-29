"""Explicit boundary to stateless SPARROW fetch/verification logic.

Never imports its runner, database helpers or website-result reuse path.
"""
from discovery.website_verifier import Fetcher, RULE_VERSION, verify
from discovery_v2.ownership import evaluate

VERIFIER_VERSION = RULE_VERSION + "+v2-p1a-1"


def new_fetcher(config):
    return ScopedFetcher(max_requests=config.max_http_requests)


def evaluate_website(company, url, fetcher):
    legacy = verify(company, url, fetcher=fetcher)
    writer = getattr(fetcher, 'writer', None)
    if writer is None:
        return {**legacy, 'verified': False, 'status': 'REVIEW', 'relationship': 'AMBIGUOUS',
                'verified_scope': None,
                'ownership': {'status': 'REVIEW', 'scope': None, 'relationship': 'AMBIGUOUS'},
                'p1a': {'rule_id': None, 'blockers': ['NO_PERSISTED_EVIDENCE_CONTEXT']}}
    decision = evaluate(company, url, writer, fetcher, legacy)
    scope = decision['verification_scope']
    result = {**legacy, 'verified': decision['verified'], 'status': decision['status'],
              'usable': decision['usable'],
              'relationship': decision['relationship'], 'verified_scope': scope,
              'final_url': scope or legacy.get('final_url') or url, 'p1a': decision,
              'ownership': {'status': decision['status'] if decision['usable'] else 'REVIEW',
                            'scope': scope, 'relationship': decision['relationship'],
                            'reasons': ([decision['rule_id']] if decision['rule_id'] else
                                        [decision['confidence_rule_id']] if decision['confidence_rule_id'] else
                                        decision['blockers']),
                            'ownership_evidence': decision['supporting_evidence_ids']}}
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
