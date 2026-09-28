"""Lossless source records and conservative derived candidate observations."""
import hashlib
import re
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from discovery.domain_generator import normalize_url
from discovery.domain_policy import blocks_official
from discovery.email_discovery import extract_emails, mailto_emails
from discovery_v2.context import contact_context, visible_soup, entity_specific
from discovery.ownership import entity_context
from discovery_v2 import RULE_VERSION
from discovery_v2.store import encode, now

URL_RE = re.compile(r'https?://[^\s<>"\)]+|(?<![@\w.-])(?:www\.)?[\w-]+\.(?:si|com|eu|net|org)(?:/[^\s<>"\)]*)?', re.I)
PHONE_RE = re.compile(r'(?:tel(?:efon)?|phone|mobil(?:ni)?)\s*[:.]?\s*(\+?\d[\d ()/.-]{5,}\d(?:\s*(?:ext\.?|int\.?|x)\s*\d+)?)', re.I)
IDENTIFIER_RE = re.compile(
    r'(?<!\w)(?P<label>mati[čc]na(?:\s+[šs]tevilka)?|registration(?:\s+number)?|company\s+id|'
    r'dav[čc]na(?:\s+[šs]tevilka)?|vat|id\s+za\s+ddv)(?!\w)\s*[:.]?\s*(?:SI\s*)?(?P<value>\d[\d .-]{5,}\d)', re.I)


def phone_value(raw):
    value = re.sub(r'^tel:', '', unquote(raw), flags=re.I).strip()
    value = re.sub(r'^(\+\d{1,3})\s*\(0\)', r'\1', value)
    parts = re.split(r'(?:;ext=|\bext\.?\s*|\bint\.?\s*|\bx\s*)(?=\d)', value, maxsplit=1, flags=re.I)
    digits = re.sub(r'\D', '', parts[0])
    if not 7 <= len(digits) <= 15:
        return None
    # Preserve national format; the company location is not numbering evidence.
    international = parts[0].lstrip().startswith('+') or digits.startswith('00')
    if digits.startswith('00'):
        digits = digits[2:]
    number = ('+' if international else '') + digits
    return number + (';ext=' + re.sub(r'\D', '', parts[1]) if len(parts) == 2 else '')


class EvidenceWriter:
    def __init__(self, store, context, company):
        self.store, self.context, self.company = store, context, company
        self.observations = []
        self.pages = {}

    def evidence(self, kind, source_class, **fields):
        payload = fields.pop('payload', {})
        return self.store.record('discovery_evidence', self.context, source_kind=kind,
            source_class=source_class, observed_at=now(), evidence_payload_json=encode(payload), **fields)

    def observe(self, evidence_id, kind, raw, normalized=None, method='text', locator='body', **value):
        fields = dict(evidence_id=evidence_id, observation_type=kind, raw_value=raw,
            normalized_value=normalized if normalized is not None else raw,
            value_json=encode(value), extraction_method=method, extractor_version=RULE_VERSION,
            source_locator=locator, observed_at=now())
        observation_id = self.store.record('discovery_observations', self.context, **fields)
        row = dict(observation_id=observation_id, **fields, value=value)
        self.observations.append(row)
        return row

    def incidental(self, evidence_id, text, locator):
        if entity_context(self.company, text):
            self.observe(evidence_id, 'COMPANY_IDENTITY', text, locator=locator,
                         candidate=True, identity_match=True)
        for match in re.finditer(r'\bSKD\s*(?:(20\d{2})\s*[:/-]?\s*)?[: ]*([A-U]?\s*\d{2}\.\d{2,3})\b', text, re.I):
            self.observe(evidence_id, 'REGISTERED_ACTIVITY', match.group(), method='explicit_skd', locator=locator,
                         code=match.group(2), classification='SKD', classification_version=match.group(1),
                         activity_name=None, candidate=True, authoritative=False)
        if text.strip():
            self.observe(evidence_id, 'BUSINESS_DESCRIPTION', text, locator=locator,
                         candidate=True, interpretation='Unclassified source wording; not an actual-business assertion')

    def identity_identifiers(self, evidence_id, publication, locator):
        """Record only explicitly labelled numeric identity claims needed by P0."""
        for match in IDENTIFIER_RE.finditer(publication):
            label = text_value(match.group('label'))
            digits = re.sub(r'\D', '', match.group('value'))
            kind = 'TAX_NUMBER' if any(word in label for word in ('davcna', 'vat', 'ddv')) else 'REGISTRATION_NUMBER'
            self.observe(evidence_id, kind, match.group('value'), digits,
                         method='labelled_identifier', locator=locator,
                         label=match.group('label'), candidate=True)

    def phone(self, evidence_id, raw, normalized, locator, publication, method='text', **value):
        digits = re.sub(r'\D', '', normalized.split(';', 1)[0])
        raw_digits = re.sub(r'\D', '', re.split(r'(?:;ext=|\bext\.?\s*|\bint\.?\s*|\bx\s*)(?=\d)',
                                                    unquote(raw), maxsplit=1, flags=re.I)[0])
        known = {}
        for kind, field in (('TAX_NUMBER', 'tax_number'), ('REGISTRATION_NUMBER', 'registration_number')):
            number = re.sub(r'\D', '', str(self.company.get(field) or ''))
            if number:
                known[number] = (kind, {'kind': 'IDENTITY_SNAPSHOT', 'field': field})
        for observation in self.observations:
            if observation['observation_type'] in ('TAX_NUMBER', 'REGISTRATION_NUMBER'):
                known.setdefault(re.sub(r'\D', '', observation['normalized_value']), (
                    observation['observation_type'],
                    {'kind': 'OBSERVATION', 'observation_id': observation['observation_id']}))
        labelled = None
        for match in IDENTIFIER_RE.finditer(publication):
            if re.sub(r'\D', '', match.group('value')) in (digits, raw_digits):
                labelled = match.group('label')
                break
        matched = digits if digits in known else raw_digits if raw_digits in known else None
        if matched or labelled:
            kind, source = known.get(matched, ('NUMERIC_IDENTIFIER', {'kind': 'LOCAL_LABEL'}))
            return self.observe(evidence_id, 'PHONE_EXCLUSION', raw, normalized,
                method='numeric_identity_exclusion', locator=locator,
                exclusion_reason='MATCHES_' + kind,
                matched_identity_source=source, identifier_label=labelled,
                publication=publication, **value)
        return self.observe(evidence_id, 'PHONE_CANDIDATE', raw, normalized,
                            method=method, locator=locator, publication=publication, **value)

    def search_result(self, item, query_type, query_text, provider, rank):
        url = item.get('url') or item.get('href') or ''
        title, body = item.get('title') or '', item.get('body') or item.get('snippet') or ''
        source_class = 'THIRD_PARTY' if blocks_official(url) else 'UNASSESSED'
        evidence_id = self.evidence('SEARCH_RESULT', source_class, provider=provider,
            query_type=query_type, query_text=query_text, result_rank=rank, result_url=url,
            result_host=urlsplit(normalize_url(url) or '').hostname or '', title=title,
            snippet_body=body, payload=item)
        text = title + '\n' + body
        self.identity_identifiers(evidence_id, text, 'title/snippet')
        for candidate in dict.fromkeys([url] + URL_RE.findall(text)):
            normalized = normalize_url(candidate.rstrip('.,;'))
            if normalized:
                self.observe(evidence_id, 'WEBSITE_CANDIDATE', candidate, normalized,
                             method='search_url' if candidate == url else 'snippet_url', locator='result_url' if candidate == url else 'title/snippet', title=title, body=body, identity_match=entity_specific(self.company, text))
        for email in sorted(extract_emails(text)):
            if entity_specific(self.company, text):
                self.observe(evidence_id, 'WEBSITE_CANDIDATE', email, 'https://' + email.rsplit('@', 1)[1] + '/', method='email_domain', locator='title/snippet', identity_match=True)
            self.observe(evidence_id, 'EMAIL_CANDIDATE', email, email, method='search_snippet', locator='title/snippet',
                         source_url=url, publication=text, source_kind='SEARCH_RESULT', identity_match=entity_specific(self.company, text))
        for match in PHONE_RE.finditer(text):
            raw = match.group(1)
            normalized = phone_value(raw)
            if normalized:
                self.phone(evidence_id, raw, normalized, 'title/snippet', text,
                           method='search_snippet', source_url=url, source_kind='SEARCH_RESULT',
                           identity_match=entity_specific(self.company, text))
        self.incidental(evidence_id, text, 'title/snippet')
        return evidence_id

    def generated(self, url):
        evidence_id = self.evidence('GENERATED_CANDIDATE', 'HYPOTHESIS', requested_url=url,
                                    payload={'strategy': 'legal_name_domain_guess', 'company_name': self.company['company_name']})
        return self.observe(evidence_id, 'WEBSITE_CANDIDATE', url, method='domain_guess', locator='requested_url')

    def page(self, requested_url, response):
        key = (response.url, hashlib.sha256(response.text.encode()).hexdigest())
        if key in self.pages:
            return self.pages[key]
        original = BeautifulSoup(response.text, 'html.parser')
        title = original.title.get_text(' ', strip=True) if original.title else ''
        soup = visible_soup(response.text)
        visible = soup.get_text(' ', strip=True)
        evidence_id = self.evidence('FETCHED_PAGE', 'UNASSESSED', requested_url=requested_url,
            final_url=response.url, http_status=response.status_code, title=title,
            content_hash=key[1], snippet_body=visible, payload={'html': response.text})
        self.pages[key] = evidence_id
        self.identity_identifiers(evidence_id, visible, 'visible_text')
        self.incidental(evidence_id, visible, 'visible_text')
        for a in soup.find_all('a', href=True):
            publication, block = contact_context(a, self.company, soup)
            publication += ' ' + a['href']
            displayed = extract_emails(a.get_text(' ', strip=True))
            for email in sorted(mailto_emails(a['href'])):
                self.observe(evidence_id, 'EMAIL_CANDIDATE', a['href'], email, method='mailto', locator='a[href=' + a['href'] + ']',
                    source_url=response.url, publication=publication, block=block, title=title,
                    visible_email=next(iter(displayed)) if len(displayed) == 1 else '', source_kind='FETCHED_PAGE')
            if a['href'].lower().startswith('tel:'):
                normalized = phone_value(a['href'])
                if normalized:
                    shown = phone_value(a.get_text(' ', strip=True))
                    self.phone(evidence_id, a['href'], normalized, 'a[href=' + a['href'] + ']', publication,
                        method='tel', source_url=response.url, block=block, title=title,
                        display_conflict=bool(shown and shown != normalized), source_kind='FETCHED_PAGE')
        for index, node in enumerate(soup.find_all(string=True)):
            if node.find_parent('a') and node.find_parent('a').get('href', '').lower().startswith(('mailto:', 'tel:')):
                continue
            publication, block = contact_context(node, self.company, soup)
            common = dict(source_url=response.url, block=block, title=title, source_kind='FETCHED_PAGE')
            for email in sorted(extract_emails(str(node))):
                self.observe(evidence_id, 'EMAIL_CANDIDATE', email, email, locator=f'text_node:{index}',
                             publication=publication, **common)
            phone_text = str(node)
            if any(w in (publication + ' ' + ' '.join(block.get('headings', []))).lower() for w in ('pokličite', 'telefon', 'phone', 'call us')) and re.fullmatch(r'[+\d ()/.-]+', phone_text.strip()):
                phone_text = 'Tel: ' + phone_text
            for match in PHONE_RE.finditer(phone_text):
                raw = match.group(1)
                normalized = phone_value(raw)
                if normalized:
                    self.phone(evidence_id, raw, normalized, f'text_node:{index}', publication, **common)
        return evidence_id


def text_value(value):
    import unicodedata
    value = ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', value).split())
