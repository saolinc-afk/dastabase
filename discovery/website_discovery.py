"""Existing domain-guess-first discovery, with structured search fallback."""
from discovery.domain_generator import generate_candidates, normalize_domain
from discovery.search_engine import discover_urls
from discovery.website_verifier import verify, blocked_url, Fetcher


def best_candidate(company, urls, fetcher=None, attempts=None, method='guess'):
    best = None
    for candidate in urls:
        if fetcher and (fetcher.requests >= 28 or fetcher.blocked): break
        metadata = candidate if isinstance(candidate, dict) else {'url': candidate}
        url = metadata.get('url', '')
        if not url or blocked_url(url): continue
        result = verify(company, url, fetcher=fetcher)
        result['method'] = method
        result['candidate'] = metadata
        if attempts is not None: attempts.append(result)
        if result['verified']: return result
        if best is None or result['confidence'] > best['confidence']:
            best = result
    return best


def discover(company, fetcher=None, stats=None, breaker=None):
    owned = fetcher is None
    fetcher = fetcher or Fetcher()
    attempts, errors = [], []
    try:
        best = best_candidate(company, generate_candidates(company['company_name']), fetcher, attempts)
        if not best or not best['verified']:
            results = discover_urls(company, stats=stats, errors=errors, breaker=breaker) if not fetcher.blocked else []
            # One search result per host; keep exact result URL and search metadata.
            hosts, candidates = set(), []
            for item in results:
                host = normalize_domain(item['url'])
                if host not in hosts and not blocked_url(item['url']):
                    candidates.append(item); hosts.add(host)
            searched = best_candidate(company, candidates[:6], fetcher, attempts, method='search')
            if searched and (not best or searched['verified'] or searched['confidence'] > best['confidence']):
                best = searched
        best = best or {'verified': False, 'status': 'NOT_FOUND', 'confidence': 0,
                        'final_url': '', 'contact_page': '', 'evidence': [], 'method': 'guess+search'}
        if not best['verified'] and best.get('status') != 'GROUP_REVIEW' and (errors or fetcher.blocked or fetcher.requests >= 28):
            best = {**best, 'status': 'ERROR' if not best['final_url'] else 'REVIEW'}
        best['attempts'] = [{k: v for k, v in a.items() if k != 'attempts'} for a in attempts]
        best['errors'] = errors + fetcher.errors
        return best
    finally:
        if owned: fetcher.close()


def main():
    raise SystemExit('Use the bounded runner: python -m discovery.runner --limit 20')


if __name__ == '__main__':
    main()
