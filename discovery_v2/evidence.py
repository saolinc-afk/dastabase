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
from discovery_v2.identity import IDENTIFIER_RE, extract_claims, support_aliases, relationship_claims
from discovery_v2.store import encode, now

URL_RE = re.compile(r'https?://[^\s<>"\)]+|(?<![@\w.-])(?:www\.)?[\w-]+\.(?:si|com|eu|net|org)(?:/[^\s<>"\)]*)?', re.I)
PHONE_RE = re.compile(r'(?:tel(?:efon)?|phone|mobil(?:ni)?)\s*[:.]?\s*(\+?\d[\d ()/.-]{5,}\d(?:\s*(?:ext\.?|int\.?|x)\s*\d+)?)', re.I)
OBFUSCATED_EMAIL_RE = re.compile(
    r'(?<![\w.-])([A-Z0-9._%+-]+)\s*(?:\[at\]|\(at\))\s*'
    r'([A-Z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\.)\s*([A-Z]{2,63})(?![\w.-])', re.I)
CONTACT_NUMBER_LABEL_RE = re.compile(
    r'\b(telefaks|telefax|facsimile|faks|fax|telefon|tel|phone|mobil(?:ni)?)\b', re.I)
PUBLIC_EMAIL_DOMAINS = frozenset({
    'gmail.com', 'hotmail.com', 'outlook.com', 'live.com', 'yahoo.com',
    'icloud.com', 'me.com', 'siol.net', 't-2.net', 'amis.net', 'aol.com',
})


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


def local_contact_label(node):
    """Text label immediately preceding a linked contact within its line."""
    parts = []
    current = node
    while current is not None:
        for sibling in current.previous_siblings:
            if getattr(sibling, 'name', None) == 'br':
                return ' '.join(reversed(parts))[-240:]
            value = sibling.get_text(' ', strip=True) if hasattr(sibling, 'get_text') else str(sibling)
            if value.strip():
                parts.append(value.strip())
        parent = getattr(current, 'parent', None)
        if getattr(parent, 'name', None) not in ('span', 'strong', 'b', 'em', 'i'):
            break
        current = parent
    return ' '.join(reversed(parts))[-240:]


def obfuscated_emails(value):
    return {f'{m.group(1)}@{m.group(2)}.{m.group(3)}'.lower()
            for m in OBFUSCATED_EMAIL_RE.finditer(value or '')}


def is_fax_label(value):
    """True only when the nearest bounded number label identifies a fax."""
    labels = CONTACT_NUMBER_LABEL_RE.findall(value or '')
    return bool(labels and labels[-1].lower() in
                ('telefaks', 'telefax', 'facsimile', 'faks', 'fax'))


class EvidenceWriter:
    def __init__(self, store, context, company):
        self.store, self.context, self.company = store, context, company
        self.observations = []
        self.evidence_rows = {}
        self.pages = {}

    def evidence(self, kind, source_class, **fields):
        payload = fields.pop('payload', {})
        evidence_id = self.store.record('discovery_evidence', self.context, source_kind=kind,
            source_class=source_class, observed_at=now(), evidence_payload_json=encode(payload), **fields)
        self.evidence_rows[evidence_id] = dict(evidence_id=evidence_id, source_kind=kind,
            source_class=source_class, evidence_payload=payload, **fields)
        return evidence_id

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
        """Emit the focused P1A identity claims used by ownership and P0."""
        for item in support_aliases(extract_claims(self.company, publication)):
            self.observe(evidence_id, item['kind'], item['raw'], item['normalized'],
                         method='structured_identity', locator=locator, **item['value'])

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
            if observation['observation_type'] in ('TAX_NUMBER', 'REGISTRATION_NUMBER', 'NUMERIC_IDENTIFIER'):
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
                direct = candidate == url
                self.observe(evidence_id, 'WEBSITE_CANDIDATE', candidate, normalized,
                             method='search_url' if direct else 'snippet_url',
                             locator='result_url' if direct else 'title/snippet',
                             title=title, body=body,
                             identity_match=entity_specific(self.company, text),
                             candidate_origin='DIRECT_SEARCH_RESULT' if direct else 'SNIPPET_URL',
                             source_result_url=url, source_result_rank=rank)
        for email in sorted(extract_emails(text)):
            email_domain = email.rsplit('@', 1)[1].lower()
            if (entity_specific(self.company, text)
                    and email_domain not in PUBLIC_EMAIL_DOMAINS
                    and not blocks_official('https://' + email_domain + '/')):
                self.observe(evidence_id, 'WEBSITE_CANDIDATE', email,
                             'https://' + email_domain + '/', method='email_domain',
                             locator='title/snippet', identity_match=True,
                             candidate_origin='SNIPPET_EMAIL_DOMAIN',
                             source_result_url=url, source_result_rank=rank,
                             source_email=email)
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
        # The same final page can be reached through several candidates. Keep
        # each requested-to-final association: ownership evaluation uses it to
        # prove that a cross-domain canonical response belongs to that candidate.
        content_hash = hashlib.sha256(response.text.encode()).hexdigest()
        association_hash = hashlib.sha256(
            normalize_url(requested_url).encode()).hexdigest()
        # Keep the historical two-item ``pages`` key contract: callers use the
        # first item as the final page URL when collecting scoped evidence.
        key = (response.url, content_hash + ':' + association_hash)
        if key in self.pages:
            return self.pages[key]
        original = BeautifulSoup(response.text, 'html.parser')
        title = original.title.get_text(' ', strip=True) if original.title else ''
        soup = visible_soup(response.text)
        visible = soup.get_text(' ', strip=True)
        evidence_id = self.evidence('FETCHED_PAGE', 'UNASSESSED', requested_url=requested_url,
            final_url=response.url, http_status=response.status_code, title=title,
            content_hash=content_hash, snippet_body=visible, payload={'html': response.text})
        self.pages[key] = evidence_id
        self.identity_identifiers(evidence_id, visible, 'visible_text')
        # Preserve page-wide observations, but authorize only bounded contexts.
        block_tags = ['main', 'article', 'section', 'footer', 'address', 'p', 'li',
                      'div', 'h1', 'h2', 'h3']
        units = soup.find_all(block_tags)
        emitted_publications = set()
        for index, unit in enumerate(units):
            # Prefer the smallest semantic publication. A container must not
            # launder identity/operator facts from separate child blocks.
            if unit.find(block_tags):
                continue
            publication = unit.get_text(' ', strip=True)
            if not publication or len(publication) > 1200:
                continue
            emitted_publications.add((unit.name, publication))
            block_id = f'identity_block:{index}'
            items = extract_claims(self.company, publication) + relationship_claims(self.company, publication)
            for item in items:
                item['value']['qualifiers'].update(block_id=block_id, source_url=response.url,
                    context=publication, block_tag=unit.name)
                self.observe(evidence_id, item['kind'], item['raw'], item['normalized'],
                    method='bounded_identity', locator=block_id, **item['value'])

        # Real contact/legal cards commonly put the name and address in separate
        # child elements. Preserve their nearest semantic container as one
        # bounded publication. Never use a container containing a footer, and
        # never promote a container unless it itself has the complete strong
        # identity combination required by ownership rules.
        composite_index = 0
        for unit in units:
            if (unit.name not in ('main', 'article') or not unit.find(block_tags)
                    or unit.find('footer')):
                continue
            publication = unit.get_text(' ', strip=True)
            if (not publication or len(publication) > 1200
                    or (unit.name, publication) in emitted_publications):
                continue
            items = extract_claims(self.company, publication) + relationship_claims(self.company, publication)
            exact = {item['kind'] for item in items
                     if item['value'].get('verification_status') == 'EXACT_MATCH'}
            if 'LEGAL_NAME' not in exact or not exact.intersection(
                    ('ADDRESS', 'TAX_NUMBER', 'REGISTRATION_NUMBER')):
                continue
            # Prefer the smallest complete semantic container so a broad page
            # wrapper cannot combine independent identity sections.
            child_complete = False
            for child in unit.find_all(block_tags):
                if child is unit or child.name == 'footer' or child.find('footer'):
                    continue
                child_items = extract_claims(self.company, child.get_text(' ', strip=True))
                child_exact = {item['kind'] for item in child_items
                               if item['value'].get('verification_status') == 'EXACT_MATCH'}
                if ('LEGAL_NAME' in child_exact and child_exact.intersection(
                        ('ADDRESS', 'TAX_NUMBER', 'REGISTRATION_NUMBER'))):
                    child_complete = True
                    break
            if child_complete:
                continue
            block_id = f'identity_container:{composite_index}'
            composite_index += 1
            for item in items:
                item['value']['qualifiers'].update(block_id=block_id, source_url=response.url,
                    context=publication, block_tag=unit.name)
                self.observe(evidence_id, item['kind'], item['raw'], item['normalized'],
                    method='bounded_identity_container', locator=block_id, **item['value'])

        self.incidental(evidence_id, visible, 'visible_text')
        for a in soup.find_all('a', href=True):
            publication, block = contact_context(a, self.company, soup)
            publication += ' ' + a['href']
            local_label = local_contact_label(a)
            displayed = extract_emails(a.get_text(' ', strip=True))
            for email in sorted(mailto_emails(a['href'])):
                self.observe(evidence_id, 'EMAIL_CANDIDATE', a['href'], email, method='mailto', locator='a[href=' + a['href'] + ']',
                    source_url=response.url, publication=publication, block=block, title=title,
                    visible_email=next(iter(displayed)) if len(displayed) == 1 else '',
                    local_label=local_label, source_kind='FETCHED_PAGE')
            if a['href'].lower().startswith('tel:'):
                normalized = phone_value(a['href'])
                if normalized and not is_fax_label(local_label + ' ' + a.get_text(' ', strip=True)):
                    shown = phone_value(a.get_text(' ', strip=True))
                    self.phone(evidence_id, a['href'], normalized, 'a[href=' + a['href'] + ']', publication,
                        method='tel', source_url=response.url, block=block, title=title,
                        display_conflict=bool(shown and shown != normalized),
                        local_label=local_label, source_kind='FETCHED_PAGE')
        for index, node in enumerate(soup.find_all(string=True)):
            if node.find_parent('a') and node.find_parent('a').get('href', '').lower().startswith(('mailto:', 'tel:')):
                continue
            publication, block = contact_context(node, self.company, soup)
            common = dict(source_url=response.url, block=block, title=title, source_kind='FETCHED_PAGE')
            for email in sorted(extract_emails(str(node))):
                self.observe(evidence_id, 'EMAIL_CANDIDATE', email, email, locator=f'text_node:{index}',
                             publication=publication, **common)
            for email in sorted(obfuscated_emails(str(node))):
                self.observe(evidence_id, 'EMAIL_CANDIDATE', str(node), email,
                             method='visible_obfuscated_text', locator=f'text_node:{index}',
                             publication=publication, **common)
            phone_text = str(node)
            local_label = local_contact_label(node)
            if any(w in (publication + ' ' + ' '.join(block.get('headings', []))).lower() for w in ('pokličite', 'telefon', 'phone', 'call us')) and re.fullmatch(r'[+\d ()/.-]+', phone_text.strip()):
                phone_text = 'Tel: ' + phone_text
            elif (block.get('single_entity_page') and
                  re.fullmatch(r'\s*(?:\+|00)386[\d ()/.-]{5,}\d\s*', phone_text)):
                phone_text = 'Tel: ' + phone_text
            for match in PHONE_RE.finditer(phone_text):
                raw = match.group(1)
                normalized = phone_value(raw)
                if normalized and not is_fax_label(local_label + ' ' + phone_text[:match.start()]):
                    self.phone(evidence_id, raw, normalized, f'text_node:{index}', publication, **common)
        return evidence_id
