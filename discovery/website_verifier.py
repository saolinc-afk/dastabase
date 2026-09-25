"""Conservative identity verification for candidate official company websites.

Scores express rule strength, not calibrated probabilities. Search snippets and
name/title similarity alone never establish ownership. No mailbox claims.
"""
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit
import requests
from bs4 import BeautifulSoup
from discovery.domain_generator import normalize_domain, normalize_url, same_site
from discovery.ownership import page_facts, evaluate_ownership, in_scope, scope_url
from discovery.domain_policy import blocks_official, is_group_parent, policy_for_url

RULE_VERSION = 'phase2-ownership-1'
TIMEOUT = 5
MAX_BYTES = 2_000_000
CONTACT_PATHS = ['/kontakt', '/contact', '/o-nas', '/about', '/podjetje', '/impressum']
LINK_WORDS = ('kontakt', 'contact', 'o-nas', 'about', 'podjetj', 'company',
              'impress', 'legal', 'privacy', 'zasebnost', 'terms', 'pogoji', 'podatki')


def blocked_url(url):
    return not normalize_url(url) or blocks_official(url)


def group_domain(url):
    return normalize_domain(url) if is_group_parent(url) else ''


def homepage(url):
    p = urlsplit(url)
    return f'{p.scheme}://{p.netloc}/'


def normalize_text(value):
    text = ''.join(c for c in unicodedata.normalize('NFKD', str(value or '').lower()) if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text).split())


def normalize_company_name(value):
    text = re.sub(r'\b(?:d\s*\.\s*o\s*\.\s*o\.?|d\s*\.\s*d\.?|s\s*\.\s*p\.?)', ' ', value or '', flags=re.I)
    return normalize_text(text)


def phrase_in(needle, haystack):
    return bool(needle) and f' {needle} ' in f' {haystack} '


def domain_matches_company(url, company_name):
    """A matching domain is supporting ownership evidence, never sufficient alone."""
    host = normalize_domain(url).split('.')[0]
    company = normalize_company_name(company_name).replace(' ', '')
    return len(company) >= 4 and (company in host or host in company)


def marketplace_profile(company, url, title, visible):
    """Require marketplace AND seller-profile context, never products alone.

    Seller Organization JSON-LD is still only evidence about the listed seller.
    A dealer's own branded domain remains eligible under the existing rules.
    Known marketplace operators are blocked independently of this heuristic.
    """
    label = normalize_text(title + ' ' + url)
    text = normalize_text(visible)
    marketplace = any(p in text for p in (
        'machinery marketplace', 'equipment marketplace', 'online marketplace'))
    profile = any(p in label for p in (
        'seller profile', 'dealer profile', 'seller listing', 'dealer listing',
        'company profile', 'searchdealer')) or any(p in text for p in (
        'verified seller', 'other sellers on', 'sellers on this marketplace'))
    return marketplace and profile and not domain_matches_company(url, company.get('company_name', ''))


def priority(url):
    text = url.lower()
    if any(x in text for x in ('kontakt', 'contact')):
        return 0
    if any(x in text for x in ('impress', 'legal', 'podatki')):
        return 1
    if any(x in text for x in ('o-nas', 'about', 'podjetj', 'company')):
        return 2
    return 3


def internal_identity_links(base_url, soup):
    links = set()
    for a in soup.find_all('a', href=True):
        url = normalize_url(urljoin(base_url, a['href']))
        if url and same_site(url, base_url) and any(w in (url + ' ' + a.get_text(' ', strip=True)).lower() for w in LINK_WORDS):
            links.add(url)
    return sorted(links, key=lambda u: (priority(u), u))


class Fetcher:
    """Per-company bounded HTTP session/cache shared by verification and email."""
    def __init__(self, max_requests=36):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'Dastabase contact discovery/1.0'
        self.cache = {}
        self.requests = 0
        self.max_requests = max_requests
        self.errors = []
        self.blocked = False

    def close(self):
        self.session.close()

    def fetch(self, url, allowed_site=None):
        if self.blocked:
            return None
        url = normalize_url(url)
        if blocked_url(url) or (allowed_site and not in_scope(url, allowed_site)):
            return None
        if url in self.cache:
            response = self.cache[url]
            return response if response is None or not allowed_site or in_scope(response.url, allowed_site) else None
        original = url
        for _ in range(5):
            if blocked_url(url) or (allowed_site and not in_scope(url, allowed_site)):
                self.errors.append(f'Redirect outside accepted site: {url}')
                break
            if self.requests >= self.max_requests:
                self.errors.append('Per-company HTTP request budget exhausted')
                break
            self.requests += 1
            time.sleep(0.15)
            try:
                r = self.session.get(url, timeout=TIMEOUT, allow_redirects=False, stream=True)
                if r.status_code in (301, 302, 303, 307, 308):
                    url = normalize_url(urljoin(url, r.headers.get('Location', '')))
                    r.close()
                    continue
                if r.status_code in (403, 429):
                    self.blocked = True
                    self.errors.append(f'Access denied/rate limited ({r.status_code}): {url}')
                if r.status_code >= 400 or 'html' not in r.headers.get('Content-Type', '').lower():
                    self.errors.append(f'HTTP {r.status_code} or non-HTML: {url}')
                    r.close()
                    break
                data = bytearray()
                for part in r.iter_content(32768):
                    data.extend(part)
                    if len(data) > MAX_BYTES:
                        raise ValueError('HTML exceeds 2 MB limit')
                r._content = bytes(data)
                r._content_consumed = True
                r.close()
                if 'charset=' not in r.headers.get('Content-Type', '').lower():
                    r.encoding = r.apparent_encoding or 'utf-8'
                # Do not solve access-control challenges or retry them.
                lower = r.text.lower()
                if any(x in lower for x in ('cf-chl-', 'verify you are human', 'checking your browser')):
                    self.blocked = True
                    self.errors.append(f'Access challenge: {url}')
                    break
                self.cache[original] = r
                self.cache[url] = r
                return r
            except (requests.RequestException, ValueError) as exc:
                self.errors.append(f'{url}: {type(exc).__name__}: {exc}')
                break
        self.cache[original] = None
        return None


def structured_organizations(soup):
    def walk(value):
        if isinstance(value, dict):
            types = value.get('@type', [])
            if isinstance(types, str): types = [types]
            if any(t in ('Organization', 'Corporation', 'LocalBusiness', 'ProfessionalService', 'Store') for t in types):
                yield value
            for child in value.values(): yield from walk(child)
        elif isinstance(value, list):
            for child in value: yield from walk(child)
    for script in soup.find_all('script', type='application/ld+json'):
        try: yield from walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError): continue


def score_page(company, response):
    soup = BeautifulSoup(response.text, 'html.parser')
    organizations = list(structured_organizations(soup))
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    ownership = page_facts(company, response.url, title, soup.get_text(' ', strip=True), soup)
    for node in soup(['script', 'style', 'noscript']): node.decompose()
    visible = soup.get_text(' ', strip=True)
    text = normalize_text(visible)
    name = normalize_company_name(company['company_name'])
    # Full normalized name, no broad fuzzy/substring acceptance of short brands.
    name_match = len(name.replace(' ', '')) >= 4 and phrase_in(name, text)
    org_match = any(normalize_company_name(o.get('legalName') or o.get('name', '')) == name for o in organizations)
    org_text = json.dumps(organizations, ensure_ascii=False)
    searchable = visible + ' ' + org_text
    evidence = []
    for kind, key in (('tax_exact', 'tax_number'), ('registration_exact', 'registration_number')):
        value = re.sub(r'\D', '', str(company.get(key) or ''))
        if len(value) >= 7 and re.search(r'(?<!\d)(?:SI\s*)?' + re.escape(value) + r'(?!\d)', searchable, re.I):
            evidence.append(kind)
    if name_match: evidence.append('company_name_exact')
    if org_match: evidence.append('organization_name_exact')
    address = str(company.get('address') or '')
    street = normalize_text(address.split(',')[0])
    address_match = bool(re.search(r'\d', street)) and len(street) > 5 and phrase_in(street, text)
    locality = normalize_text(company.get('municipality') or '')
    locality_match = bool(locality) and phrase_in(locality, text)
    if address_match: evidence.append('street_address_exact')
    if locality_match: evidence.append('postal_locality_exact')
    parked = any(p in text for p in ('domain for sale', 'domain is for sale', 'buy this domain', 'domena je naprodaj', 'sedo domain parking'))
    listing = any(p in text for p in ('business directory', 'poslovni imenik', 'company directory',
                                      'company profile', 'supplier report', 'company listing', 'profil podjetja'))
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    profile_markers = ('prosta delovna mesta', 'zaposlitev', 'job opening', 'job portal',
                       'career portal', 'company profile', 'supplier report', 'business directory',
                       'company directory', 'company listing', 'profile page')
    news_markers = ('novice', 'news article', 'press release')
    page_label = normalize_text(title + ' ' + response.url)
    profile_page = any(marker in page_label for marker in profile_markers)
    news_page = any(marker in page_label for marker in news_markers)
    domain_match = domain_matches_company(response.url, company['company_name'])
    if parked: evidence.append('parked_domain')
    if listing: evidence.append('directory_page')
    if profile_page: evidence.append('third_party_profile_page')
    if news_page: evidence.append('news_or_media_page')
    if domain_match: evidence.append('domain_matches_company_name')
    # A real company career/news page can carry one of these labels. It remains
    # eligible only when the page also exposes independent first-party identity.
    marketplace = marketplace_profile(company, response.url, title, visible)
    if marketplace: evidence.append('third_party_marketplace_page')
    third_party = ownership['page_type'] == 'THIRD_PARTY' or marketplace
    if third_party: ownership['page_type'] = 'THIRD_PARTY'
    return {'page_url': response.url, 'page_title': title,
            'signals': evidence, 'name_match': name_match or org_match,
            'address_match': address_match, 'locality_match': locality_match,
            'organization_match': org_match, 'domain_match': domain_match,
            'third_party': third_party, 'rejected': parked or third_party,
            'entity_signals': [s for s in evidence if s != 'domain_matches_company_name'],
            'ownership': ownership}


def find_contact_page(base_url, soup):
    return next((u for u in internal_identity_links(base_url, soup) if priority(u) == 0), '')


def verify(company, url, fetcher=None):
    owned = fetcher is None
    fetcher = fetcher or Fetcher()
    result = {'verified': False, 'status': 'NOT_FOUND', 'confidence': 0,
              'final_url': '', 'contact_page': '', 'evidence': [], 'rule_version': RULE_VERSION}
    try:
        if blocked_url(url):
            result.update(status='REVIEW', evidence=[{'reason': 'directory/social/disallowed URL', 'url': url}])
            return result
        response = fetcher.fetch(url)
        if response is None:
            return result
        root = homepage(response.url)
        if blocked_url(root):
            result.update(status='REVIEW', final_url=root,
                          evidence=[{'reason': 'directory/social/disallowed final URL', 'url': root}])
            return result
        result['final_url'] = root
        queue = [response.url]
        responses = {response.url: response}
        visited = set()
        scope = root
        while queue and len(visited) < 5:
            current = queue.pop(0)
            if current in visited or not in_scope(current, scope): continue
            visited.add(current)
            r = responses.get(current) or fetcher.fetch(current, allowed_site=scope)
            if r is None or not in_scope(r.url, scope): continue
            signals = score_page(company, r)
            result['evidence'].append(signals)
            owner = evaluate_ownership(company, result['evidence'], url, root,
                                       known_group=bool(group_domain(root)))
            result['ownership'] = owner
            result['relationship'] = owner['relationship']
            if not owner['scope']:
                handoffs = [x['scope'] for x in owner['ownership_evidence'] if x['basis']=='company_domain_handoff']
                if handoffs: scope = handoffs[0]
            if owner['scope']:
                scope = owner['scope']
                result['verified_scope'] = scope
                result['final_url'] = scope
            soup = BeautifulSoup(r.text, 'html.parser')
            links = [u for u in internal_identity_links(r.url, soup) if in_scope(u, scope)]
            result['contact_page'] = result['contact_page'] or next((u for u in links if priority(u)==0), '')
            if owner['status'] == 'VERIFIED':
                exact = any(set(p.get('entity_signals', [])) & {'tax_exact','registration_exact'} for p in result['evidence'])
                result.update(verified=True, status='VERIFIED', confidence=95 if exact else 85)
                return result
            if owner['relationship']=='THIRD_PARTY' or signals['rejected']:
                result.update(status='REVIEW', confidence=0)
                return result
            if owner['status']=='GROUP_REVIEW':
                result.update(status='GROUP_REVIEW', confidence=40)
                return result
            queue.extend(u for u in links if u not in visited and u not in queue)
            if len(visited)==1:
                if in_scope(root, scope) and root not in visited and root not in queue: queue.append(root)
                if not queue: queue.extend(scope_url(scope, path) for path in CONTACT_PATHS[:3])
        result.update(status='REVIEW', confidence=min(60, 20*len({s for p in result['evidence'] for s in p.get('signals', [])})),
                      relationship='UNRESOLVED')
        return result
    finally:
        if owned: fetcher.close()
