"""Local-first, provider-independent registration identity resolution."""
import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from discovery.domain_generator import normalize_domain
from discovery.domain_policy import blocks_official
from discovery_v2.evidence import public_email_domain

from control_room.matching import (has_explicit_legal_form, normalize_email,
    normalize_name, normalize_phone, normalize_registration, normalize_tax,
    normalize_text, submitted_name_aliases)


RESOLVER_VERSION = 'import-identity-v1'
QUERY_VERSION = 'import-identity-query-v1'
PLACEHOLDER = re.compile(r'^[\W_]+$', re.UNICODE)
GENERIC_DESCRIPTIONS = {
    'potencialni podjetnik', 'zaposlen v solstvu', 'zaposlena v solstvu',
    'brez podjetja', 'nimam podjetja', 'ni podjetja',
}
IDENTIFIER = re.compile(
    r'\b(?:dav[cč]na(?:\s+številka)?|tax(?:\s+number)?|vat|'
    r'mati[cč]na(?:\s+številka)?|registration(?:\s+number)?)\s*[:#-]?\s*'
    r'(?:SI\s*)?(\d[\d .-]{5,10}\d)(?!\d)', re.I)


@dataclass(frozen=True)
class IdentityDecision:
    status: str
    canonical_company_id: int | None = None
    resolution_rule: str | None = None
    decisive_evidence: tuple = ()
    alternatives: tuple[int, ...] = ()
    conflicts: tuple[str, ...] = ()
    identity_status: str = 'UNRESOLVED'
    persistence_status: str = 'NOT_APPLICABLE'
    proposed_identity: dict | None = None
    enrichment_scope: str = 'NOT_EVALUATED'
    research_mode: str = 'ON_DEMAND'


def research_allowed(enrichment_scope, research_mode):
    """Bulk scope and an explicit on-demand request are separate decisions."""
    return research_mode == 'ON_DEMAND' or enrichment_scope == 'BULK_IN_SCOPE'


def _decision(status, company_id=None, rule=None, evidence=(), alternatives=(),
              conflicts=(), **values):
    if status in ('LOCAL_RESOLVED', 'SEARCH_RESOLVED'):
        values.setdefault('identity_status', 'RESOLVED_EXISTING')
        values.setdefault('persistence_status', 'EXISTING')
    elif status == 'EXTERNAL_ENTITY_IDENTIFIED':
        values.setdefault('identity_status', 'RESOLVED_NEW_ENTITY')
        values.setdefault('persistence_status', 'PENDING_CREATE')
    elif status == 'AMBIGUOUS':
        values.setdefault('identity_status', 'AMBIGUOUS')
    elif status == 'NOT_ELIGIBLE':
        values.setdefault('identity_status', 'NOT_ELIGIBLE')
    return IdentityDecision(status, company_id, rule, tuple(evidence),
                            tuple(alternatives), tuple(conflicts), **values)


def registration_input(normalized):
    email = normalize_email(normalized.get('email'))
    domain = email.rsplit('@', 1)[1] if email else ''
    if domain and public_email_domain(domain):
        domain = ''
    return {
        'company_name': str(normalized.get('company_name') or '').strip(),
        'normalized_name': normalize_name(normalized.get('company_name')),
        'person_name': normalize_text(normalized.get('person_name')),
        'email_domain': domain,
        'phone': normalize_phone(normalized.get('phone')),
        'tax_number': normalize_tax(normalized.get('tax_number')),
        'registration_number': normalize_registration(normalized.get('registration_number')),
        'municipality': normalize_text(normalized.get('municipality')),
        'address': normalize_text(normalized.get('address')),
    }


def fingerprint(inputs, origin_status, candidate_ids, conflicts):
    identity_input = dict(inputs)
    # Different people registering for the same named/domain entity should
    # share one task. Person and phone become identity keys only when no
    # company-bearing name/domain exists.
    if inputs.get('normalized_name') or inputs.get('email_domain'):
        identity_input['person_name'] = ''
        identity_input['phone'] = ''
    payload = {'input': identity_input, 'origin_status': origin_status,
               'candidate_ids': sorted(set(candidate_ids)),
               'conflicts': sorted(set(conflicts))}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _name_candidates(index, raw_name):
    ids = set(index.names.get(normalize_name(raw_name), ()))
    for alias, _, _ in submitted_name_aliases(raw_name):
        ids.update(index.aliases.get(alias, ()))
    return ids


def local_resolve(index, inputs, origin_status, origin_candidates=(), conflicts=()):
    tax_ids = set(index.tax.get(inputs['tax_number'], ())) if inputs['tax_number'] else set()
    registration_ids = (set(index.registration.get(inputs['registration_number'], ()))
                        if inputs['registration_number'] else set())
    identifier_sets = [value for value in (tax_ids, registration_ids) if value]
    if any('IDENTIFIER' in value for value in conflicts):
        alternatives = set().union(*identifier_sets) if identifier_sets else set(origin_candidates)
        return _decision('AMBIGUOUS', alternatives=sorted(alternatives), conflicts=conflicts)
    if len(identifier_sets) > 1 and set.intersection(*identifier_sets) == set():
        return _decision('AMBIGUOUS', alternatives=sorted(set.union(*identifier_sets)),
                         conflicts=('CONFLICTING_IDENTIFIERS',))
    identifiers = set.intersection(*identifier_sets) if identifier_sets else set()
    if len(identifiers) == 1:
        company_id = next(iter(identifiers))
        return _decision('LOCAL_RESOLVED', company_id, 'IDR-01_EXACT_IDENTIFIER',
            ({'kind': 'EXACT_IDENTIFIER', 'company_id': company_id},))
    if len(identifiers) > 1:
        return _decision('AMBIGUOUS', alternatives=sorted(identifiers),
                         conflicts=('NON_UNIQUE_IDENTIFIER',))

    names = _name_candidates(index, inputs['company_name'])
    domains = set(index.domains.get(inputs['email_domain'], ())) if inputs['email_domain'] else set()
    converged = names & domains
    if len(converged) == 1 and len(domains) == 1:
        company_id = next(iter(converged))
        return _decision('LOCAL_RESOLVED', company_id,
            'IDR-02_OFFICIAL_DOMAIN_IDENTITY', (
                {'kind': 'CONTROLLED_NAME', 'value': inputs['normalized_name']},
                {'kind': 'ACCEPTED_OFFICIAL_DOMAIN', 'value': inputs['email_domain']},))
    historical = (set(index.historical_domain_candidates.get(inputs['email_domain'], ())) |
                  set(index.historical_email_candidates.get(inputs['email_domain'], ()))
                  if inputs['email_domain'] else set())
    alternatives = set(origin_candidates) | names | domains | historical
    if len(converged) > 1:
        return _decision('SEARCH_ELIGIBLE', alternatives=sorted(converged),
                         conflicts=('MULTIPLE_DOMAIN_IDENTITIES',))
    if eligible_for_search(inputs):
        return _decision('SEARCH_ELIGIBLE', alternatives=sorted(alternatives), conflicts=conflicts)
    return _decision('NOT_ELIGIBLE', alternatives=sorted(alternatives), conflicts=conflicts)


def eligible_for_search(inputs):
    raw = inputs['company_name'].strip()
    name = inputs['normalized_name']
    if not raw or PLACEHOLDER.fullmatch(raw) or name in GENERIC_DESCRIPTIONS:
        return False
    company_bearing = (inputs['email_domain'] or has_explicit_legal_form(raw)
                       or len(name) >= 4 and len(name.split()) >= 1)
    return bool(company_bearing)


def plan_queries(inputs, alternatives=(), index=None):
    """Return at most two deterministic query dictionaries."""
    name, municipality = inputs['company_name'].strip(), inputs['municipality']
    domain, person, phone, address = (inputs['email_domain'], inputs['person_name'],
                                      inputs['phone'], inputs['address'])
    plans = []

    def add(strategy, text):
        text = ' '.join(text.split())
        if text and text not in {item['query_text'] for item in plans} and len(plans) < 2:
            plans.append({'query_strategy': strategy, 'strategy_version': QUERY_VERSION,
                          'provider': 'UNCONFIGURED', 'query_text': text,
                          'locale': {'gl': 'si', 'hl': 'sl', 'num': 10}})

    if name and domain:
        add('COMPANY_DOMAIN', f'"{name}" site:{domain}')
    elif name and municipality:
        add('COMPANY_MUNICIPALITY', f'Podjetje "{name}" {municipality}')
    elif name and address:
        add('COMPANY_ADDRESS', f'"{name}" "{address}"')
    elif name:
        add('COMPANY_NAME', f'Podjetje "{name}" kontakt')
    if person and domain:
        add('PERSON_DOMAIN', f'"{person}" site:{domain}')
    elif person and name:
        add('PERSON_COMPANY', f'"{person}" "{name}"')
    elif phone and name:
        add('PHONE_COMPANY', f'"{phone}" "{name}"')
    if len(plans) < 2 and len(set(alternatives)) <= 3 and alternatives and index is not None:
        labels = ' OR '.join(f'"{index.by_id[value]["company_name"]}"'
                             for value in sorted(set(alternatives)) if value in index.by_id)
        if labels:
            add('CANONICAL_DISCRIMINATOR', f'"{name}" ({labels})')
    return tuple(plans)


def resolve_results(index, inputs, origin_status, origin_candidates, query_results):
    """Resolve stored provider payloads; never performs I/O."""
    identifier_hits, absent_identifiers = set(), set()
    absent_identity = {}
    domain_hits, name_hits = set(), set()
    publishers = {}
    evidence = []
    candidate_ids = set(origin_candidates) | _name_candidates(index, inputs['company_name'])
    for query in query_results:
        for result in query.get('results', ()):
            url = result.get('url') or result.get('link') or ''
            title = str(result.get('title') or '')
            body = str(result.get('body') or result.get('snippet') or '')
            publication = title + '\n' + body
            publisher = normalize_domain(url)
            domain = publisher
            source_only = bool(url and blocks_official(url))
            for raw in IDENTIFIER.findall(publication):
                digits = re.sub(r'\D', '', raw)
                hits = (set(index.tax.get(normalize_tax(digits), ())) |
                        set(index.registration.get(normalize_registration(digits), ())))
                if hits and not source_only:
                    identifier_hits.update(hits)
                    evidence.append({'kind': 'SEARCH_IDENTIFIER', 'value': digits,
                                     'company_ids': sorted(hits), 'publisher': publisher})
                elif len(digits) in (7, 8) and not source_only:
                    absent_identifiers.add(digits)
                    legal_name = str(result.get('legal_name') or '').strip()
                    if (legal_name and normalize_name(legal_name) in
                            normalize_name(publication)):
                        record = absent_identity.setdefault(digits, {
                            'legal_names': set(), 'publishers': set(), 'results': []})
                        record['legal_names'].add(legal_name)
                        if publisher:
                            record['publishers'].add(publisher)
                        record['results'].append({
                            'url': url, 'publisher': publisher, 'legal_name': legal_name,
                            'title': title, 'snippet': body, 'identifier': digits,
                            'tax_number': str(result.get('tax_number') or '').strip(),
                            'registration_number': str(
                                result.get('registration_number') or '').strip(),
                            'entity_type': str(result.get('entity_type') or '').strip(),
                            'address': str(result.get('address') or '').strip(),
                            'municipality': str(result.get('municipality') or '').strip(),
                            'country': str(result.get('country') or '').strip(),
                            'official_domain': str(result.get('official_domain') or '').strip(),
                            'observed_at': (result.get('observed_at') or
                                            result.get('fetched_at')),
                        })
            accepted = set(index.domains.get(domain, ())) if domain and not source_only else set()
            if accepted:
                domain_hits.update(accepted)
                evidence.append({'kind': 'ACCEPTED_RESULT_DOMAIN', 'value': domain,
                                 'company_ids': sorted(accepted), 'publisher': publisher})
            normalized_publication = normalize_text(publication)
            for company_id in candidate_ids | domain_hits | identifier_hits:
                company = index.by_id.get(company_id)
                if company and normalize_name(company['company_name']) in normalized_publication:
                    name_hits.add(company_id)
                    publishers.setdefault(company_id, set()).add(publisher)
                    evidence.append({'kind': 'SEARCH_LEGAL_NAME', 'company_id': company_id,
                                     'publisher': publisher, 'source_only': source_only})

    if len(identifier_hits) > 1:
        return _decision('AMBIGUOUS', alternatives=sorted(identifier_hits),
                         conflicts=('CONFLICTING_SEARCH_IDENTIFIERS',))
    if len(identifier_hits) == 1:
        company_id = next(iter(identifier_hits))
        return _decision('SEARCH_RESOLVED', company_id, 'IDR-01_EXACT_IDENTIFIER',
                         evidence, sorted(candidate_ids - {company_id}))
    official = domain_hits & name_hits
    if len(official) == 1 and len(domain_hits) == 1:
        company_id = next(iter(official))
        independent = {value for value in publishers.get(company_id, ())
                       if value and value != normalize_domain(index.enrichment[company_id].get('website'))}
        if origin_status != 'AMBIGUOUS' or independent:
            return _decision('SEARCH_RESOLVED', company_id,
                'IDR-02_OFFICIAL_DOMAIN_IDENTITY', evidence,
                sorted(candidate_ids - {company_id}))
    converged = {company_id for company_id, sources in publishers.items()
                 if len({value for value in sources if value}) >= 2}
    if origin_status != 'AMBIGUOUS' and len(converged) == 1:
        company_id = next(iter(converged))
        return _decision('SEARCH_RESOLVED', company_id,
            'IDR-05_INDEPENDENT_PUBLISHER_CONVERGENCE', evidence,
            sorted(candidate_ids - {company_id}))
    alternatives = identifier_hits | domain_hits | name_hits | candidate_ids
    if len(alternatives) > 1 or origin_status == 'AMBIGUOUS':
        return _decision('AMBIGUOUS', alternatives=sorted(alternatives))
    if absent_identifiers and not alternatives:
        # A genuinely new entity needs an explicit legal identity and labeled
        # identifier on a non-third-party result. A domain, brand, person, or
        # plausible search hit alone remains unresolved.
        if len(absent_identifiers) > 1:
            return _decision('AMBIGUOUS', conflicts=('CONFLICTING_EXTERNAL_IDENTIFIERS',),
                evidence=({'kind': 'UNMAPPED_IDENTIFIER', 'value': value}
                          for value in sorted(absent_identifiers)))
        if len(absent_identifiers) == 1:
            identifier = next(iter(absent_identifiers))
            record = absent_identity.get(identifier)
            direct_official = bool(record and any(
                normalize_domain(item['official_domain']) == item['publisher']
                for item in record['results'] if item['official_domain']))
            independently_corroborated = bool(record and len(record['publishers']) >= 2)
            if (record and len(record['legal_names']) == 1 and
                    (direct_official or independently_corroborated)):
                witness = record['results'][0]
                proposed = {
                    'legal_name': next(iter(record['legal_names'])),
                    'tax_number': witness['tax_number'] or None,
                    'registration_number': witness['registration_number'] or None,
                    'entity_type': witness['entity_type'] or None,
                    'address': witness['address'] or None,
                    'municipality': witness['municipality'] or None,
                    'country': witness['country'] or None,
                    'official_domain': witness['official_domain'] or None,
                    'submitted_identity': dict(inputs),
                    'decisive_research_evidence': tuple(record['results']),
                    'provenance_urls': tuple(sorted({item['url'] for item in record['results']})),
                    'confidence': 'HIGH',
                    'authorization_rule': 'IDR-06_VERIFIED_EXTERNAL_LEGAL_IDENTITY',
                    'conflicts': (),
                    'resolver_version': RESOLVER_VERSION,
                    'source_timestamps': tuple(sorted({item['observed_at']
                        for item in record['results'] if item['observed_at']})),
                }
                return _decision('EXTERNAL_ENTITY_IDENTIFIED', None,
                    'IDR-06_VERIFIED_EXTERNAL_LEGAL_IDENTITY', (
                        {'kind': 'UNMAPPED_LABELED_IDENTIFIER', 'value': identifier,
                         'legal_name': proposed['legal_name'],
                         'provenance_urls': proposed['provenance_urls']},),
                    proposed_identity=proposed)
        return _decision('CANONICAL_NOT_FOUND', evidence=(
            {'kind': 'UNMAPPED_IDENTIFIER', 'value': value}
            for value in sorted(absent_identifiers)))
    return _decision('UNRESOLVED', alternatives=sorted(alternatives))
