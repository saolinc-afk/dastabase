"""Search providers and scheduled evidence acquisition."""
from dataclasses import dataclass
import os

import requests

from discovery.domain_generator import normalize_domain

LEGAL_COMPANY_CONTACT = 'LEGAL_COMPANY_CONTACT'
LEGAL_NAME_CONTACT = 'LEGAL_NAME_CONTACT'
LEGAL_COMPANY_DATABASE = 'LEGAL_COMPANY_DATABASE'
DOMAIN_CONTACT = 'DOMAIN_CONTACT'
SUCCESS_WITH_RESULTS = 'SUCCESS_WITH_RESULTS'
SUCCESS_ZERO_RESULTS = 'SUCCESS_ZERO_RESULTS'
FAILED = 'FAILED'


class SerperAPIError(RuntimeError):
    """A sanitized Serper response error safe for attempt diagnostics."""


@dataclass(frozen=True)
class SearchOutcome:
    status: str
    results: tuple[dict, ...] = ()

    def __post_init__(self):
        if self.status not in (SUCCESS_WITH_RESULTS, SUCCESS_ZERO_RESULTS):
            raise ValueError('SearchOutcome must represent a successful provider response')
        if (self.status == SUCCESS_WITH_RESULTS) != bool(self.results):
            raise ValueError('Search outcome status and results disagree')


def successful(results):
    results = tuple(results)
    return SearchOutcome(SUCCESS_WITH_RESULTS if results else SUCCESS_ZERO_RESULTS, results)


def queries(company, use_municipality=False):
    name = company['company_name'].strip()
    location = ' ' + company['municipality'].strip() if use_municipality and company.get('municipality') else ''
    return [(LEGAL_COMPANY_CONTACT, f'Podjetje "{name}" kontakt'),
            (LEGAL_NAME_CONTACT, f'{name}{location} kontakt'),
            (LEGAL_COMPANY_DATABASE, f'Podjetje {name}{location} bizi.si')]


def domain_query(url):
    return DOMAIN_CONTACT, f'{normalize_domain(url)} kontakt'


class DDGSearch:
    name = 'ddgs'
    staged = False

    def search(self, query, max_results):
        # Only the stateless provider call is reused, not legacy search strategy.
        from discovery.search_engine import search_ddg
        try:
            return successful(search_ddg(query, max_results=max_results))
        except Exception as exc:
            # DDGS represents a legitimate aggregate zero-result response as an
            # exception. Preserve other provider/transport failures as failures.
            if type(exc).__name__ == 'DDGSException' and str(exc).strip() == 'No results found.':
                return successful([])
            raise


class SerperSearch:
    name = 'serper'
    staged = True
    endpoint = 'https://google.serper.dev/search'

    def __init__(self, api_key, session=None):
        if not api_key:
            raise ValueError('SERPER_API_KEY is required for Serper')
        self._api_key = api_key
        self._session = session or requests.Session()

    def search(self, query, max_results):
        response = self._session.post(self.endpoint,
            headers={'X-API-KEY': self._api_key, 'Content-Type': 'application/json'},
            json={'q': query, 'gl': 'si', 'hl': 'sl', 'num': 10}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise SerperAPIError('Invalid Serper response')
        if 'error' in payload or ('message' in payload and 'organic' not in payload):
            raise SerperAPIError('Serper API returned an error')
        organic = payload.get('organic') or []
        if not isinstance(organic, list):
            raise ValueError('Invalid Serper response: organic must be a list')
        results = []
        for sequence, item in enumerate(organic[:max_results], 1):
            if not isinstance(item, dict):
                continue
            url = item.get('link') or item.get('url') or ''
            if not url:
                continue
            position = item.get('position')
            results.append({'url': url, 'title': item.get('title') or '',
                'body': item.get('snippet') or '',
                'position': position if isinstance(position, int) and position > 0 else sequence,
                'query': query, 'provider': self.name,
                'serper_result': dict(item)})
        return successful(results)


def default_provider(environ=None):
    environ = os.environ if environ is None else environ
    api_key = environ.get('SERPER_API_KEY')
    return SerperSearch(api_key) if api_key else DDGSearch()
