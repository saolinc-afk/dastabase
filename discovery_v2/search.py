"""Search providers and scheduled evidence acquisition."""
from dataclasses import dataclass
import os
import time

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


class ProviderCircuitOpen(RuntimeError):
    """Stop a run after sustained transient or permanent provider failure."""


class SerperTransientError(SerperAPIError):
    """A bounded-retry Serper failure with no credentials in its message."""


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
    pacing_seconds = 0.5
    retry_delays = (1.0, 2.0)
    circuit_failure_threshold = 5

    def __init__(self, api_key, session=None, *, sleeper=time.sleep, clock=time.monotonic):
        if not api_key:
            raise ValueError('SERPER_API_KEY is required for Serper')
        self._api_key = api_key
        self._session = session or requests.Session()
        self._sleep = sleeper
        self._clock = clock
        self._last_request_at = None
        self._consecutive_failures = 0
        self.request_count = 0

    def _pace(self):
        if self._last_request_at is not None:
            remaining = self.pacing_seconds - (self._clock() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()

    def _failed(self, message, *, permanent=False):
        self._consecutive_failures += 1
        if permanent or self._consecutive_failures >= self.circuit_failure_threshold:
            reason = 'permanent provider error' if permanent else (
                f'{self._consecutive_failures} consecutive exhausted transient queries')
            raise ProviderCircuitOpen(f'Serper circuit breaker opened: {reason}; last error: {message}')
        raise SerperTransientError(message)

    def search(self, query, max_results):
        response = None
        for attempt in range(len(self.retry_delays) + 1):
            self._pace()
            self.request_count += 1
            try:
                response = self._session.post(self.endpoint,
                    headers={'X-API-KEY': self._api_key, 'Content-Type': 'application/json'},
                    json={'q': query, 'gl': 'si', 'hl': 'sl', 'num': 10}, timeout=15)
            except requests.RequestException as exc:
                if attempt < len(self.retry_delays):
                    self._sleep(self.retry_delays[attempt])
                    continue
                return self._failed(f'transport failure after {attempt + 1} attempts ({type(exc).__name__})')
            status = response.status_code
            if status in (408, 425, 429) or 500 <= status <= 599:
                if attempt < len(self.retry_delays):
                    self._sleep(self.retry_delays[attempt])
                    continue
                return self._failed(f'HTTP {status} after {attempt + 1} attempts')
            if status >= 400:
                return self._failed(f'HTTP {status}', permanent=True)
            break
        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError):
            return self._failed('invalid JSON response')
        if not isinstance(payload, dict):
            return self._failed('invalid response object')
        if 'error' in payload or ('message' in payload and 'organic' not in payload):
            return self._failed('provider returned an error payload', permanent=True)
        organic = payload.get('organic') or []
        if not isinstance(organic, list):
            return self._failed('invalid organic results payload')
        self._consecutive_failures = 0
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
