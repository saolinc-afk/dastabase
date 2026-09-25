"""Search fallback for the existing guess-first website discovery workflow."""
import re
from ddgs import DDGS
from discovery.domain_generator import normalize_domain, normalize_url


class SearchInfrastructureStopped(RuntimeError):
    """Abort this invocation without completing the current company."""


def infrastructure_failure(exc):
    # DDGS wraps transport errors in DDGSException; inspect the wrapped message.
    text = f'{type(exc).__name__}: {exc}'.lower()
    return isinstance(exc, (ConnectionError, TimeoutError)) or any(token in text for token in (
        'dns error', 'nameresolutionerror', 'failed to resolve', 'nodename nor servname',
        'name or service not known', 'temporary failure in name resolution',
        'connecterror', 'connectionerror', 'connection refused', 'connection reset',
        'no connections available', 'network is unreachable', 'transporterror',
        'proxyerror', 'sslerror', 'tls handshake', 'timeout', 'timed out',
    ))


class SearchCircuitBreaker:
    def __init__(self, threshold=10):
        if threshold < 1:
            raise ValueError('Search failure threshold must be positive')
        self.threshold = threshold
        self.consecutive_failures = 0

    def success(self):
        self.consecutive_failures = 0

    def failure(self, exc):
        if not infrastructure_failure(exc):
            self.consecutive_failures = 0
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            raise SearchInfrastructureStopped(
                f'{self.consecutive_failures} consecutive search infrastructure failures: {exc}'
            ) from exc


def base_company_name(name):
    return re.split(r'\b(?:d\s*\.\s*o\s*\.\s*o\.?|d\s*\.\s*d\.?|s\s*\.\s*p\.?)', name or '', maxsplit=1, flags=re.I)[0].strip(' ,')


def build_queries(company):
    base = base_company_name(company['company_name'])
    location = company.get('municipality') or company.get('address') or 'Slovenija'
    queries = [f'"{base}" {location}']
    tax = str(company.get('tax_number') or '').removeprefix('SI')
    queries.append(f'"{tax}" "{base}"' if tax else f'"{base}" Slovenija kontakt')
    return queries


def deduplicate(results):
    seen, unique = set(), []
    for item in results:
        url = normalize_url(item.get('url', ''))
        if url and url not in seen:
            seen.add(url)
            unique.append({**item, 'url': url})
    return unique


def prioritize(results):
    return sorted(results, key=lambda x: not normalize_domain(x['url']).endswith('.si'))


def search_ddg(query, max_results=6):
    with DDGS(timeout=8) as ddgs:
        results = ddgs.text(query, max_results=max_results)
    return [{'url': item.get('href') or item.get('url', ''),
             'title': item.get('title', ''), 'body': item.get('body', ''),
             'query': query, 'provider': 'ddgs'} for item in results]


def discover_urls(company, stats=None, errors=None, breaker=None):
    results = []
    for query in build_queries(company):
        if stats is not None:
            stats['search_calls'] = stats.get('search_calls', 0) + 1
        try:
            results.extend(search_ddg(query))
            if breaker is not None:
                breaker.success()
        except Exception as exc:
            if errors is not None:
                errors.append(f'Search {query}: {type(exc).__name__}: {exc}')
            if breaker is not None:
                breaker.failure(exc)
    return prioritize(deduplicate(results))
