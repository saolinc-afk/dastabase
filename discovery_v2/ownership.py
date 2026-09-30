"""Named, conservative Discovery v2 website ownership rules."""
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from discovery.domain_generator import normalize_domain, normalize_url
from discovery.domain_policy import blocks_official
from discovery.ownership import scope_url, canonical_path, in_scope, page_type, name
from discovery_v2.identity import fold, normalize_legal_name

RELATIONSHIPS = ('STANDALONE', 'BRAND_OF_ENTITY', 'SUBSIDIARY_ON_GROUP_DOMAIN',
                 'ENTITY_PAGE_ON_GROUP_DOMAIN',
                 'GROUP_PARENT', 'RELATED_ENTITY', 'AMBIGUOUS', 'UNRELATED')
TRUSTED_ENTITY_HOSTS = ('bizi.si', 'companywall.si', 'companywall.eu', 'ebonitete.si')
GROUP_WORDS = ('member of group', 'part of group', 'član skupine', 'clan skupine',
               'subsidiary of', 'hčerinska družba', 'hcerinska druzba', 'del skupine')
CONTACT_PATHS = ('kontakt', 'contact', 'impress', 'legal', 'podjetj', 'company', 'about', 'o-nas')


def trusted_host(host):
    host = (host or '').lower().removeprefix('www.')
    return any(host == item or host.endswith('.' + item) for item in TRUSTED_ENTITY_HOSTS)


def fetch_state(fetcher, candidate_url, writer=None):
    domain = normalize_domain(candidate_url)
    errors = [error for url, values in fetcher.failures.items()
              if normalize_domain(url) == domain for error in values]
    if writer is not None:
        errors.extend(error for row in writer.evidence_rows.values()
            if row.get('source_kind') == 'FETCH_FAILURE'
            and normalize_domain(row.get('requested_url') or '') == domain
            for error in row.get('evidence_payload', {}).get('errors', []))
    errors = list(dict.fromkeys(errors))
    if any(normalize_domain(url) == domain for url in fetcher.responses):
        return {'status': 'FETCHED', 'errors': errors}
    label = 'NOT_FETCHED'
    joined = ' '.join(errors).lower()
    if 'certificate has expired' in joined or 'certificate_expired' in joined:
        label = 'TLS_EXPIRED'
    elif 'hostname' in joined and ('mismatch' in joined or 'certificate' in joined):
        label = 'TLS_HOSTNAME_MISMATCH'
    elif ('401' in joined or '403' in joined or 'access denied' in joined
          or 'access challenge' in joined or 'unauthorized' in joined):
        label = 'HTTP_FORBIDDEN'
    elif 'eof' in joined:
        label = 'CONNECTION_EOF'
    elif 'timed out' in joined or 'timeout' in joined:
        label = 'TIMEOUT'
    elif 'redirect' in joined:
        label = 'REDIRECT_UNRESOLVED'
    elif errors:
        label = 'FETCH_UNCERTAIN'
    return {'status': label, 'errors': errors}


RULES = {
    'OWN-01_EXACT_IDENTIFIER_ON_SITE': 'STANDALONE',
    'OWN-02_EXACT_ENTITY_CONTACT_PAGE': 'STANDALONE',
    'OWN-03_TRUSTED_LINK_PLUS_SITE_IDENTITY': 'STANDALONE',
    'OWN-04_MULTISOURCE_ENTITY_DOMAIN': 'STANDALONE',
    'OWN-05_SUBSIDIARY_SCOPE': 'SUBSIDIARY_ON_GROUP_DOMAIN',
    'OWN-06_SUPPORTED_BRAND': 'BRAND_OF_ENTITY',
    'OWN-07_SCOPED_ENTITY_PAGE': 'ENTITY_PAGE_ON_GROUP_DOMAIN',
}

CONFIDENCE_RULES = {
    'CONF-01_LEGAL_ADDRESS_CONVERGENCE': 'HIGH',
    'CONF-02_LEGAL_CONTACT_CONVERGENCE': 'HIGH',
    'CONF-03_LEGAL_PAGE_DOMAIN_CONTACT': 'HIGH',
    'CONF-04_LEGAL_PAGE_COHERENCE': 'MEDIUM',
    'CONF-05_SEARCH_SUPPORTED_DOMAIN': 'HIGH',
    'CONF-06_SUPPORTED_EXPANDED_LEGAL_NAME': 'HIGH',
}

USABLE_STATUSES = ('VERIFIED', 'HIGH', 'MEDIUM')
UNSELECTABLE_PAGE_CLASSES = ('NEWS_PAGE', 'THIRD_PARTY_PAGE')


def usable(assessment):
    return assessment.get('status') in USABLE_STATUSES and bool(assessment.get('verified_scope'))


def _domain_name_match(company, url):
    target = ''.join(name(company.get('company_name', '')).split())
    labels = normalize_domain(url).split('.')[:-1]
    return len(target) >= 3 and any(re.sub(r'[^a-z0-9]', '', label) == target for label in labels)


def _company_specific_subdomain(company, url):
    """A target-named tenant below a differently named registrable host."""
    labels = normalize_domain(url).split('.')
    target = ''.join(name(company.get('company_name', '')).split())
    return (len(labels) >= 3 and len(target) >= 3
            and any(re.sub(r'[^a-z0-9]', '', label) == target for label in labels[:-2]))


def _company_specific_path(company, url):
    target = ''.join(name(company.get('company_name', '')).split())
    path = re.sub(r'[^a-z0-9]', '', fold(urlsplit(url).path))
    return len(target) >= 3 and target in path


def publisher_id(host):
    host = normalize_domain(host)
    for suffix, publisher in (('bizi.si', 'bizi'), ('companywall.si', 'companywall'),
                              ('companywall.eu', 'companywall'), ('ebonitete.si', 'ebonitete')):
        if host == suffix or host.endswith('.' + suffix):
            return publisher
    # Unknown publishers remain separate evidence, never presumed independent.
    return host


def page_classification(row, claims=()):
    """Content role never changes merely because identity claims were extracted."""
    url = row.get('final_url') or row.get('requested_url') or ''
    html = (row.get('evidence_payload') or {}).get('html') or ''
    soup = BeautifulSoup(html, 'html.parser')
    title = row.get('title') or ''
    label = fold(title + ' ' + urlsplit(url).path)
    if (re.search(r'\b(?:news|novice|press release|blog)\b', label)
            or re.search(r'"@type"\s*:\s*"(?:NewsArticle|Article|BlogPosting)"', html, re.I)):
        return 'NEWS_PAGE'
    if any(marker in urlsplit(url).path.lower() for marker in
           ('katalog_clanov', 'katalog-clanov', 'poslovni-imenik', 'company-directory')):
        return 'THIRD_PARTY_PAGE'
    if page_type(url, title, row.get('snippet_body') or '') in ('THIRD_PARTY', 'PROFILE'):
        return 'THIRD_PARTY_PAGE'
    if any(word in label for word in ('legal', 'impress', 'pravna')):
        return 'LEGAL_PAGE'
    if any(word in label for word in CONTACT_PATHS):
        return 'CONTACT_PAGE'
    return 'UNKNOWN_PAGE'


def _exact(observations, *kinds):
    return [o for o in observations if o['observation_type'] in kinds and
            o['value'].get('verification_status') == 'EXACT_MATCH']


def _explicit_link(row, candidate):
    # Snippets remain corroboration only. Arbitrary links, ads and email domains
    # do not establish an entity-to-domain association.
    body = row.get('snippet_body') or ''
    links = re.findall(r'(?:^|[;\n])\s*(?:website|spletna stran)\s*:?\s*(https?://[^\s<>]+)', body, re.I)
    return any(normalize_url(link.rstrip('.,;')) == normalize_url(candidate) for link in links)


def _search_domain_support(company, domain, links, claims, rows):
    """Summarize deterministic target-to-domain evidence without authorizing it."""
    direct, third_party, evidence_types, publishers, witnesses = [], [], set(), set(), []
    conflict = False
    for link in links:
        row = rows.get(link['evidence_id'], {})
        if row.get('source_kind') != 'SEARCH_RESULT' or not link.get('value', {}).get('identity_match'):
            continue
        local = [o for o in claims if o['evidence_id'] == link['evidence_id']]
        if any(o['observation_type'] in ('TAX_NUMBER', 'REGISTRATION_NUMBER')
               and o['value'].get('verification_status') == 'CONFLICT' for o in local):
            conflict = True
        other_names = [o for o in local if o['observation_type'] == 'ALIAS'
                       and o['value'].get('verification_status') == 'UNVERIFIED'
                       and not _target_name_in_prose(company, o)]
        if other_names:
            conflict = True
        publisher = publisher_id(row.get('result_host') or row.get('result_url'))
        method = link.get('extraction_method')
        is_direct = (method == 'search_url'
                     and normalize_domain(row.get('result_host') or row.get('result_url')) == domain)
        kind = ('DIRECT_SEARCH_DOMAIN' if is_direct else
                'SNIPPET_URL' if method == 'snippet_url' else
                'SNIPPET_EMAIL_DOMAIN' if method == 'email_domain' else None)
        if not kind:
            continue
        publishers.add(publisher)
        evidence_types.add(kind)
        witnesses.append(link)
        strong_identity = bool(_exact(local, 'LEGAL_NAME') and (
            _exact(local, 'ADDRESS', 'TAX_NUMBER', 'REGISTRATION_NUMBER')
            or (_exact(local, 'STREET') and _exact(local, 'POSTAL_CODE', 'MUNICIPALITY'))))
        if is_direct:
            direct.append(link)
        elif publisher != domain and strong_identity:
            third_party.append(link)
            witnesses.extend(_exact(local, 'LEGAL_NAME', 'ADDRESS', 'TAX_NUMBER',
                                    'REGISTRATION_NUMBER', 'STREET', 'POSTAL_CODE',
                                    'MUNICIPALITY'))
    return {'direct': direct, 'third_party': third_party,
            'evidence_types': evidence_types, 'publishers': publishers,
            'witnesses': witnesses, 'conflict': conflict}


_INCIDENTAL_ENTITY_CONTEXT = (
    'website by', 'developed by', 'designed by', 'powered by', 'izdelava spletne',
    'privacy provider', 'cookie provider', 'software provider', 'ponudnik programske',
    'software provided by', 'provided by',
    'customer', 'client', 'reference', 'partner', 'stranka', 'referenca',
    'certification', 'certificate', 'booking provider', 'payment provider',
    'formerly known as', 'renamed to', 'preimenovan', 'nekdanj',
    'joint controller', 'joint controllers', 'skupni upravljav', 'contitolari',
    )
_IDENTITY_ENTITY_CONTEXT = (
    'company details', 'company represented',
    'kontaktni podatki', 'podatki podjetja', 'polni naziv', 'kratko ime',
    'registration number', 'maticna stevilka', 'davcna stevilka', 'id za ddv',
)
_OPERATOR_ENTITY_CONTEXT = (
    'website operated by', 'website owned by', 'site operator', 'legal publisher',
    'upravljavec spletnega mesta', 'upravljavec spletne strani', 'lastnik spletne',
    'legal owner', 'foreign operator',
)


def _target_name_in_prose(company, observation):
    raw = normalize_legal_name(observation.get('raw_value'))
    target = normalize_legal_name(company.get('company_name'))
    if not raw or not target or raw == target:
        return bool(raw and target and raw == target)
    # The legal-name regex can begin at a capitalized prose prefix. Do not turn
    # that capture into a second entity when its complete legal-name suffix is
    # the target itself.
    if raw.endswith(' ' + target):
        return True
    observed_words = normalize_legal_name(observation.get('raw_value'), remove_form=True).split()
    target_words = normalize_legal_name(company.get('company_name'), remove_form=True).split()
    # Registered names commonly add an activity description or omit a trailing
    # locality. Preserve that as a target-name variant; it is not another entity.
    return bool(observed_words and target_words and
                (_contains_words(observed_words, target_words)
                 or _contains_words(target_words, observed_words)))


def _contains_words(haystack, needle):
    if not needle or len(needle) > len(haystack):
        return False
    return any(haystack[i:i + len(needle)] == needle
               for i in range(len(haystack) - len(needle) + 1))


def _incidental_entity_context(observation):
    context = fold(observation.get('value', {}).get('qualifiers', {}).get('context', ''))
    return any(marker in context for marker in _INCIDENTAL_ENTITY_CONTEXT)


def _material_conflicting_entity(company, observation):
    """Whether a bounded other-name claim materially defines site identity."""
    if _target_name_in_prose(company, observation):
        return False
    context = fold(observation.get('value', {}).get('qualifiers', {}).get('context', ''))
    raw = fold(observation.get('raw_value'))
    if any(marker in context for marker in _OPERATOR_ENTITY_CONTEXT):
        return True
    if _incidental_entity_context(observation):
        return False
    if any(marker in context for marker in _IDENTITY_ENTITY_CONTEXT):
        return True
    # A bare or heading-like legal entity is identity-bearing. Longer prose
    # with an unexplained second entity stays ambiguous and blocks as before.
    block_tag = observation.get('value', {}).get('qualifiers', {}).get('block_tag')
    return bool(raw and block_tag in ('h1', 'h2', 'h3', 'address', 'footer'))


def _attributed_identifier_conflicts(company, claims, blocks):
    """Return only conflicts structurally attributable to candidate identity."""
    def target_presentation(local):
        return any(o['observation_type'] == 'ALIAS'
                   and o['value'].get('qualifiers', {}).get('alias_kind') ==
                   'EXPANDED_LEGAL_NAME'
                   and _target_name_in_prose(company, o) for o in local)

    page_target_identity = {}
    for (eid, _), local in blocks.items():
        if _exact(local, 'LEGAL_NAME', 'SITE_OPERATOR') or target_presentation(local):
            page_target_identity[eid] = True
    conflicts = []
    for (eid, _), local in blocks.items():
        local_conflicts = [o for o in local
            if o['observation_type'] in ('TAX_NUMBER', 'REGISTRATION_NUMBER')
            and o['value'].get('verification_status') == 'CONFLICT']
        for conflict in local_conflicts:
            if _incidental_entity_context(conflict):
                continue
            aliases = [o for o in local if o['observation_type'] == 'ALIAS'
                       and o['value'].get('verification_status') == 'UNVERIFIED'
                       and not _target_name_in_prose(company, o)]
            if aliases and not any(_material_conflicting_entity(company, o) for o in aliases):
                continue
            if (_exact(local, 'LEGAL_NAME', 'SITE_OPERATOR') or target_presentation(local)
                    or any(_material_conflicting_entity(company, o) for o in aliases)
                    or page_target_identity.get(eid)):
                conflicts.append(conflict)
    return conflicts


def evaluate(company, candidate_url, writer, fetcher, legacy_result=None):
    legacy_result = legacy_result or {}
    domain = normalize_domain(candidate_url)
    rows, observations = writer.evidence_rows, writer.observations
    # A recorded response requested from the candidate is the only authority
    # for following a cross-domain canonical redirect. Once established, the
    # verifier's subsequent pages on that final host belong to this assessment.
    redirect_domains = {normalize_domain(row.get('final_url') or '') for row in rows.values()
        if row.get('source_kind') == 'FETCHED_PAGE'
        and normalize_domain(row.get('requested_url') or '') == domain
        and row.get('final_url')
        and normalize_domain(row.get('final_url') or '') != domain}
    accepted_domains = {domain} | redirect_domains
    fetched = {eid: row for eid, row in rows.items() if row.get('source_kind') == 'FETCHED_PAGE'
               and normalize_domain(row.get('final_url') or '') in accepted_domains}
    links = [o for o in observations if o['observation_type'] == 'WEBSITE_CANDIDATE'
             and normalize_domain(o['normalized_value']) == domain]
    relevant = set(fetched) | {o['evidence_id'] for o in links}
    claims = [o for o in observations if o['evidence_id'] in relevant]
    classifications = {eid: page_classification(row) for eid, row in fetched.items()}
    blocks = {}
    for o in claims:
        q = o['value'].get('qualifiers', {})
        if o['evidence_id'] in fetched and q.get('block_id'):
            blocks.setdefault((o['evidence_id'], q['block_id']), []).append(o)
    conflicts = _attributed_identifier_conflicts(company, claims, blocks)
    blockers = []
    candidate_rows = [row for row in fetched.values()
        if normalize_url(row.get('final_url') or row.get('requested_url')) == normalize_url(candidate_url)]
    candidate_is_third_party = (
        page_type(candidate_url) in ('THIRD_PARTY', 'PROFILE')
        or any(page_classification(row) in UNSELECTABLE_PAGE_CLASSES for row in candidate_rows))
    if candidate_is_third_party:
        blockers.append('THIRD_PARTY_PAGE')
    if blocks_official(candidate_url) or any(blocks_official(row.get('final_url') or '')
                                             for row in fetched.values()):
        blockers.append('DISALLOWED_PUBLISHER_DOMAIN')
    if any('parked_domain' in p.get('signals', []) for p in legacy_result.get('evidence', [])) or any(
            any(marker in fold(row.get('snippet_body')) for marker in
                ('domain for sale', 'domain is for sale', 'buy this domain', 'domena je naprodaj', 'sedo domain parking'))
            for row in fetched.values()):
        blockers.append('PARKED_DOMAIN')
    if any(p.get('third_party') for p in legacy_result.get('evidence', [])):
        blockers.append('THIRD_PARTY_PAGE')

    sources = {}
    corroboration = []
    for eid in sorted({o['evidence_id'] for o in links}):
        row = rows[eid]
        if row.get('source_kind') != 'SEARCH_RESULT': continue
        publisher = publisher_id(row.get('result_host') or row.get('result_url'))
        local = [o for o in claims if o['evidence_id'] == eid]
        explicit = _explicit_link(row, candidate_url)
        item = {'evidence_id': eid, 'publisher_id': publisher, 'explicit_link': explicit,
                'trusted': trusted_host(row.get('result_host'))}
        sources.setdefault(publisher, []).append(item)
        # Distinct known publishers alone do not prove independence: OWN-04 is
        # reserved until independently sourced records can be established.
        if explicit and item['trusted'] and _exact(local, 'LEGAL_NAME') and _exact(local, 'TAX_NUMBER', 'REGISTRATION_NUMBER', 'ADDRESS'):
            corroboration.append((item, _exact(local, 'LEGAL_NAME', 'TAX_NUMBER', 'REGISTRATION_NUMBER', 'ADDRESS') +
                                  [o for o in links if o['evidence_id'] == eid and normalize_url(o['normalized_value']) == normalize_url(candidate_url)]))

    search_support = _search_domain_support(company, domain, links, claims, rows)
    if search_support['conflict']:
        blockers.append('SEARCH_IDENTITY_CONFLICT')

    candidates = []
    group_seen = any(any(fold(word) in fold(row.get('snippet_body')) for word in GROUP_WORDS)
                     or page_type(row.get('final_url', ''), row.get('title', ''), row.get('snippet_body', '')) == 'GROUP'
                     for row in fetched.values())
    for (eid, block_id), local in blocks.items():
        row = fetched[eid]; url = row['final_url']
        evaluation_scope = (scope_url(url) if normalize_domain(url) in redirect_domains
                            else candidate_url)
        # Never preserve authority from a pre-redirect path or borrow sibling evidence.
        if not in_scope(url, evaluation_scope): continue
        if row.get('http_status') != 200 or classifications[eid] in UNSELECTABLE_PAGE_CLASSES: continue
        legal = _exact(local, 'LEGAL_NAME')
        identifiers = _exact(local, 'TAX_NUMBER', 'REGISTRATION_NUMBER')
        address = _exact(local, 'ADDRESS')
        operator = _exact(local, 'SITE_OPERATOR')
        relationships = _exact(local, 'ENTITY_RELATIONSHIP')
        group = [o for o in relationships if o['normalized_value'] == 'SUBSIDIARY_ON_GROUP_DOMAIN']
        brand = [o for o in relationships if o['normalized_value'] == 'BRAND_OF_ENTITY']
        aliases = [o for o in local if o['observation_type'] == 'ALIAS' and o['value'].get('verification_status') == 'ALIAS_SUPPORTED']
        # Treat other explicit legal names conservatively even on subsidiary pages.
        scoped_entity_page = (_company_specific_path(company, url)
                              or _company_specific_subdomain(company, url))
        # A parent footer is expected on a bounded entity page and does not
        # redefine that page's subject. Only a block that identifies the target
        # can authorize it or contradict its identity.
        if scoped_entity_page and not legal:
            continue
        conflict_pool = local if scoped_entity_page else claims
        other_names = [o for o in conflict_pool if o['evidence_id'] == eid and o['observation_type'] == 'ALIAS'
                       and o['value'].get('verification_status') == 'UNVERIFIED'
                       and o['value'].get('qualifiers', {}).get('block_id')]
        # Regex captures may include the affirmative operator prefix. An exact
        # suffix preceded solely by that prefix is not another legal entity.
        other_names = [o for o in other_names if normalize_legal_name(o['raw_value']) not in
                       ('website operated by ' + normalize_legal_name(company['company_name']),
                        'website owned by ' + normalize_legal_name(company['company_name']))]
        other_names = [o for o in other_names if _material_conflicting_entity(company, o)]
        if other_names:
            blockers.append('CONFLICTING_LEGAL_ENTITY'); continue
        conflict_ids = {o['observation_id'] for o in conflicts}
        if any(o['observation_id'] in conflict_ids for o in local):
            blockers.append('EXACT_IDENTIFIER_CONFLICT'); continue
        # Keep the fetched URL's encoded path. Decoding reserved characters
        # here can turn an entity tenant into a query and broaden scope to '/'.
        scope = scope_url(url, (urlsplit(url).path or '/').rstrip('/') + '/')
        rule, authorization_basis = None, None
        identity, relation, source_support = [], [], []
        if group and operator and legal and (identifiers or address) and canonical_path(url) != '/':
            rule = 'OWN-05_SUBSIDIARY_SCOPE'; authorization_basis = 'EXPLICIT_OPERATOR'
            identity = legal + (identifiers or address); relation = group
        elif scoped_entity_page and legal and (identifiers or address):
            # This authorizes only the exact target's bounded page/tenant. It
            # deliberately makes no claim that the target operates the parent
            # host, and it never composes identity across blocks or pages.
            rule = 'OWN-07_SCOPED_ENTITY_PAGE'; authorization_basis = 'STRONG_SCOPED_ENTITY_PAGE'
            identity = legal + (identifiers or address)
        elif group_seen and not (operator and canonical_path(url) == '/'):
            continue
        elif operator and legal:
            authorization_basis = 'EXPLICIT_OPERATOR'
            if brand and aliases and identifiers:
                rule = 'OWN-06_SUPPORTED_BRAND'; identity = legal + identifiers + aliases; relation = brand
            elif identifiers:
                rule = 'OWN-01_EXACT_IDENTIFIER_ON_SITE'; identity = legal + identifiers
            elif address:
                rule = 'OWN-02_EXACT_ENTITY_CONTACT_PAGE'; identity = legal + address
            elif corroboration:
                rule = 'OWN-03_TRUSTED_LINK_PLUS_SITE_IDENTITY'; identity = legal
                # Content ordering, never UUID ordering, determines the witness.
                chosen = min(corroboration, key=lambda x: (x[0]['publisher_id'], rows[x[0]['evidence_id']].get('result_url', ''), rows[x[0]['evidence_id']].get('snippet_body', '')))
                source_support = chosen[1]
        elif (not group_seen and legal
              and classifications[eid] in ('CONTACT_PAGE', 'LEGAL_PAGE')):
            authorization_basis = 'STRONG_LEGAL_PAGE'
            if identifiers:
                rule = 'OWN-01_EXACT_IDENTIFIER_ON_SITE'; identity = legal + identifiers
            elif address:
                rule = 'OWN-02_EXACT_ENTITY_CONTACT_PAGE'; identity = legal + address
        if rule:
            witnesses = identity + operator + relation + source_support
            candidates.append(dict(rule_id=rule, authorization_basis=authorization_basis,
                verification_scope=scope, relationship=RULES[rule],
                identity_evidence=[o['observation_id'] for o in identity],
                operator_evidence=[o['observation_id'] for o in operator],
                relationship_evidence=[o['observation_id'] for o in relation],
                supporting_observation_ids=sorted({o['observation_id'] for o in witnesses}),
                supporting_evidence_ids=sorted({o['evidence_id'] for o in witnesses}),
                context={'evidence_id': eid, 'block_id': block_id, 'source_url': url,
                         'source_host': normalize_domain(url),
                         'page_classification': classifications[eid]}))
    if conflicts and not any(c['rule_id'] == 'OWN-07_SCOPED_ENTITY_PAGE' for c in candidates):
        blockers.append('EXACT_IDENTIFIER_CONFLICT')
    blockers = sorted(set(blockers))
    selected = min(candidates, key=lambda c: (c['verification_scope'], c['rule_id'], c['context']['block_id'])) if candidates and not blockers else {}

    confidence = {}
    if not selected and not blockers and not group_seen:
        safe_blocks = {(eid, block_id): local for (eid, block_id), local in blocks.items()
                       if rows[eid].get('http_status') == 200
                       and classifications[eid] not in UNSELECTABLE_PAGE_CLASSES
                       and (in_scope(rows[eid].get('final_url') or '', candidate_url)
                            or (normalize_domain(rows[eid].get('final_url') or '') in redirect_domains
                                and in_scope(rows[eid].get('final_url') or '',
                                             scope_url(rows[eid].get('final_url') or ''))))}
        bounded_legal = [(key, o) for key, local in safe_blocks.items()
                         for o in _exact(local, 'LEGAL_NAME')]
        other_entities = [o for (eid, _), local in safe_blocks.items() for o in local
                          if o['observation_type'] == 'ALIAS'
                          and o['value'].get('verification_status') == 'UNVERIFIED'
                          and _material_conflicting_entity(company, o)]
        if other_entities:
            blockers.append('CONFLICTING_LEGAL_ENTITY')
        foreign_context = [o for o in claims if o['observation_type'] in ('EMAIL_CANDIDATE', 'PHONE_CANDIDATE')
                           and o.get('value', {}).get('source_kind') == 'FETCHED_PAGE'
                           and not o.get('value', {}).get('block', {}).get('target_entity')
                           and any(marker in fold(o.get('value', {}).get('publication', '')) for marker in
                                   ('foreign office', 'foreign branch', 'office in', 'pisarna v',
                                    'podruznica', 'subsidiary'))]
        if foreign_context:
            blockers.append('FOREIGN_OR_RELATED_ENTITY_CONTEXT')
        blockers = sorted(set(blockers))
        onsite_email = [o for o in claims
            if o['observation_type'] == 'EMAIL_CANDIDATE'
            and o.get('value', {}).get('source_kind') == 'FETCHED_PAGE'
            and normalize_domain('https://' + o['normalized_value'].rsplit('@', 1)[-1]) == domain
            and (in_scope(o.get('value', {}).get('source_url', ''), candidate_url)
                 or normalize_domain(o.get('value', {}).get('source_url', '')) in redirect_domains)
            and (o.get('value', {}).get('block', {}).get('target_entity') or
                 (o.get('value', {}).get('block', {}).get('single_entity_page')
                  and o.get('value', {}).get('block', {}).get('contact_section')))]
        search_links = [o for o in links if rows[o['evidence_id']].get('source_kind') == 'SEARCH_RESULT'
                        and o.get('value', {}).get('identity_match')
                        and normalize_domain(o['normalized_value']) == domain]
        email_support = min(onsite_email, key=lambda o: (
            o['normalized_value'], o.get('value', {}).get('source_url', ''), o['source_locator'])) if onsite_email else None
        search_link = min(search_links, key=lambda o: (
            publisher_id(rows[o['evidence_id']].get('result_host')),
            rows[o['evidence_id']].get('result_url', ''),
            rows[o['evidence_id']].get('snippet_body', ''))) if search_links else None
        domain_name = _domain_name_match(company, candidate_url)
        if bounded_legal and not blockers:
            legal_keys = {key for key, _ in bounded_legal}
            full_address = [(key, o) for key, local in safe_blocks.items()
                            for o in _exact(local, 'ADDRESS') if key in legal_keys]
            contact_legal = [(key, o) for key, o in bounded_legal
                             if classifications[key[0]] in ('CONTACT_PAGE', 'LEGAL_PAGE')]
            confidence_rule, reasons = None, []
            if full_address and (domain_name or onsite_email or search_links):
                confidence_rule = 'CONF-01_LEGAL_ADDRESS_CONVERGENCE'
                reasons = ['BOUNDED_EXACT_LEGAL_NAME', 'BOUNDED_EXACT_FULL_ADDRESS']
            elif onsite_email and search_links:
                confidence_rule = 'CONF-02_LEGAL_CONTACT_CONVERGENCE'
                reasons = ['BOUNDED_EXACT_LEGAL_NAME', 'FIRST_PARTY_DOMAIN_EMAIL',
                           'ENTITY_SPECIFIC_SEARCH_DOMAIN_LINK']
            elif contact_legal and domain_name and onsite_email:
                confidence_rule = 'CONF-03_LEGAL_PAGE_DOMAIN_CONTACT'
                reasons = ['BOUNDED_EXACT_LEGAL_NAME', 'CONTACT_OR_LEGAL_PAGE',
                           'LEGAL_NAME_DOMAIN_MATCH', 'FIRST_PARTY_DOMAIN_EMAIL']
            elif contact_legal and (domain_name or onsite_email or search_links):
                confidence_rule = 'CONF-04_LEGAL_PAGE_COHERENCE'
                reasons = ['BOUNDED_EXACT_LEGAL_NAME', 'CONTACT_OR_LEGAL_PAGE']
            if confidence_rule:
                status = CONFIDENCE_RULES[confidence_rule]
                page_key, legal_observation = min(bounded_legal,
                    key=lambda item: (rows[item[0][0]].get('final_url', ''), item[0][1]))
                eid, block_id = page_key
                url = rows[eid]['final_url']
                evidence = [legal_observation]
                evidence.extend(o for key, o in full_address if key == page_key)
                if email_support: evidence.append(email_support)
                if search_link: evidence.append(search_link)
                if domain_name: reasons.append('LEGAL_NAME_DOMAIN_MATCH')
                if onsite_email and 'FIRST_PARTY_DOMAIN_EMAIL' not in reasons: reasons.append('FIRST_PARTY_DOMAIN_EMAIL')
                if search_links and 'ENTITY_SPECIFIC_SEARCH_DOMAIN_LINK' not in reasons: reasons.append('ENTITY_SPECIFIC_SEARCH_DOMAIN_LINK')
                confidence = dict(confidence_rule_id=confidence_rule,
                    confidence_reasons=sorted(set(reasons)),
                    verification_scope=scope_url(url, (urlsplit(url).path or '/').rstrip('/') + '/'),
                    relationship='STANDALONE',
                    supporting_observation_ids=sorted({o['observation_id'] for o in evidence}),
                    supporting_evidence_ids=sorted({o['evidence_id'] for o in evidence}),
                    context={'evidence_id': eid, 'block_id': block_id, 'source_url': url,
                             'source_host': domain, 'page_classification': classifications[eid]},
                    status=status)

        if not confidence and not blockers:
            expanded = [(key, o) for key, local in safe_blocks.items() for o in local
                        if o['observation_type'] == 'ALIAS'
                        and o['value'].get('verification_status') == 'UNVERIFIED'
                        and o['value'].get('qualifiers', {}).get('alias_kind') == 'EXPANDED_LEGAL_NAME'
                        and _target_name_in_prose(company, o)]
            if (expanded and search_support['direct'] and search_support['third_party']
                    and len(search_support['publishers']) >= 2
                    and len(search_support['evidence_types']) >= 2):
                page_key, alias = min(expanded, key=lambda item: (
                    rows[item[0][0]].get('final_url', ''), item[0][1]))
                eid, block_id = page_key
                local = safe_blocks[page_key]
                if search_support['third_party']:
                    # Cite only the stable, mandatory CONF-06 predicate. Later
                    # contact observations are useful but are not decisive.
                    evidence = [alias] + search_support['witnesses']
                    url = rows[eid]['final_url']
                    confidence = dict(
                        confidence_rule_id='CONF-06_SUPPORTED_EXPANDED_LEGAL_NAME',
                        confidence_reasons=['EXPANDED_LEGAL_PRESENTATION_NAME',
                            'DIRECT_ENTITY_SEARCH_DOMAIN', 'INDEPENDENT_DOMAIN_CORROBORATION'],
                        verification_scope=scope_url(url), relationship='STANDALONE',
                        supporting_observation_ids=sorted({o['observation_id'] for o in evidence}),
                        supporting_evidence_ids=sorted({o['evidence_id'] for o in evidence}),
                        context={'evidence_id': eid, 'block_id': block_id,
                                 'source_url': url, 'source_host': normalize_domain(url),
                                 'page_classification': classifications[eid]},
                        status=CONFIDENCE_RULES['CONF-06_SUPPORTED_EXPANDED_LEGAL_NAME'])

        state = fetch_state(fetcher, candidate_url, writer)
        if (not confidence and not blockers and state['status'] == 'HTTP_FORBIDDEN'
                and search_support['direct'] and search_support['third_party']
                and len(search_support['publishers']) >= 2
                and len(search_support['evidence_types']) >= 2
                and domain_name and not blocks_official(candidate_url)
                and page_type(candidate_url) not in ('THIRD_PARTY', 'PROFILE')):
            evidence = search_support['witnesses']
            failure_evidence_ids = sorted(eid for eid, row in rows.items()
                if row.get('source_kind') == 'FETCH_FAILURE'
                and normalize_domain(row.get('requested_url') or '') == domain)
            direct = min(search_support['direct'], key=lambda o: (
                rows[o['evidence_id']].get('result_rank') or 10_000,
                rows[o['evidence_id']].get('result_url', '')))
            confidence = dict(
                confidence_rule_id='CONF-05_SEARCH_SUPPORTED_DOMAIN',
                confidence_reasons=['DIRECT_ENTITY_SEARCH_DOMAIN',
                    'INDEPENDENT_DOMAIN_CORROBORATION', 'MULTIPLE_DOMAIN_EVIDENCE_TYPES',
                    'LEGAL_NAME_DOMAIN_MATCH', 'FETCH_BLOCKED_HTTP_FORBIDDEN'],
                verification_scope=scope_url(candidate_url), relationship='STANDALONE',
                supporting_observation_ids=sorted({o['observation_id'] for o in evidence}),
                supporting_evidence_ids=sorted(
                    {o['evidence_id'] for o in evidence} | set(failure_evidence_ids)),
                context={'evidence_id': direct['evidence_id'], 'block_id': 'search_domain_bundle',
                         'source_url': rows[direct['evidence_id']].get('result_url'),
                         'source_host': domain, 'page_classification': 'SEARCH_EVIDENCE',
                         'fetch_status': state['status']},
                status=CONFIDENCE_RULES['CONF-05_SEARCH_SUPPORTED_DOMAIN'])

    status = 'VERIFIED' if selected else confidence.get('status', 'REVIEW')
    chosen = selected or confidence
    return {'verified': bool(selected), 'usable': status in USABLE_STATUSES, 'status': status,
            'relationship': selected.get('relationship', 'GROUP_PARENT' if group_seen else 'AMBIGUOUS'),
            'rule_id': None, 'authorization_basis': None, 'verification_scope': None,
            'confidence_rule_id': None, 'confidence_reasons': [],
            'verified_without_fetch': False,
            'supporting_evidence_ids': [], 'supporting_observation_ids': [],
            'identity_evidence': [], 'operator_evidence': [], 'relationship_evidence': [],
            **chosen, 'site_operator_anchors': selected.get('operator_evidence', []),
            'conflicts': sorted(o['observation_id'] for o in conflicts), 'blockers': blockers,
            'fetch_state': fetch_state(fetcher, candidate_url, writer), 'page_classifications': classifications,
            'source_publishers': sources}


def _validate_conf06(company, candidate_url, decision, writer, fetcher):
    """Revalidate recorded CONF-06 witnesses plus all-evidence contradictions."""
    from types import SimpleNamespace

    evidence_ids = set(decision.get('supporting_evidence_ids') or ())
    observation_ids = set(decision.get('supporting_observation_ids') or ())
    if (not evidence_ids or not observation_ids
            or not evidence_ids <= set(writer.evidence_rows)):
        return False
    observations_by_id = {o.get('observation_id'): o for o in writer.observations}
    if not observation_ids <= set(observations_by_id):
        return False

    # The original immutable witnesses must still independently derive the
    # recorded CONF-06 decision. New positive observations cannot alter it.
    witness_rows = {eid: writer.evidence_rows[eid] for eid in evidence_ids}
    witness_observations = [observations_by_id[oid] for oid in observation_ids]
    witness_writer = SimpleNamespace(evidence_rows=witness_rows,
                                     observations=witness_observations)
    witness_fetcher = SimpleNamespace(
        responses={row.get('final_url'): True for row in witness_rows.values()
                   if row.get('source_kind') == 'FETCHED_PAGE' and row.get('final_url')},
        failures={})
    original = evaluate(company, candidate_url, witness_writer, witness_fetcher)
    keys = ('status', 'confidence_rule_id', 'confidence_reasons',
            'verification_scope', 'relationship', 'supporting_evidence_ids',
            'supporting_observation_ids', 'context')
    if (original.get('confidence_rule_id') != 'CONF-06_SUPPORTED_EXPANDED_LEGAL_NAME'
            or any(decision.get(key) != original.get(key) for key in keys)):
        return False

    # Evaluate every current observation as a contradiction gate. A stronger
    # valid rule may win, but conflicts, unsafe publishers, or relationship
    # changes still revoke the original authorization.
    current = evaluate(company, candidate_url, writer, fetcher)
    return bool(current.get('status') in ('HIGH', 'VERIFIED')
                and current.get('usable')
                and not current.get('blockers')
                and not current.get('conflicts')
                and current.get('relationship') == 'STANDALONE'
                and normalize_domain(current.get('verification_scope') or '') ==
                    normalize_domain(decision.get('verification_scope') or ''))


def validate_assessment(company, candidate_url, assessment, writer, fetcher):
    """Re-derive authorization from recorded evidence, including at persistence."""
    decision = assessment.get('p1a') or {}
    if decision.get('authorization_basis') == 'SEARCH_EVIDENCE_RESOLVER_V2':
        from discovery_v2.search_resolver import validate_resolution_assessment
        return validate_resolution_assessment(company, candidate_url, assessment, writer)
    status = decision.get('status')
    rule = decision.get('rule_id')
    confidence_rule = decision.get('confidence_rule_id')
    if (status not in USABLE_STATUSES or decision.get('blockers') or
            decision.get('conflicts') or decision.get('verified_without_fetch')):
        return False
    if status == 'VERIFIED':
        if rule not in RULES or confidence_rule is not None or not assessment.get('verified') or not decision.get('verified'):
            return False
    elif (rule is not None or confidence_rule not in CONFIDENCE_RULES or
          CONFIDENCE_RULES[confidence_rule] != status or assessment.get('verified') or decision.get('verified')):
        return False
    scope = decision.get('verification_scope')
    owner = assessment.get('ownership') or {}
    context_class = decision.get('context', {}).get('page_classification')
    if (not scope or blocks_official(scope) or page_type(scope) in ('THIRD_PARTY', 'PROFILE')
            or context_class in UNSELECTABLE_PAGE_CLASSES
            or assessment.get('verified_scope') != scope or
            assessment.get('status') != status or owner.get('scope') != scope or
            owner.get('status') != status or decision.get('relationship') != 'STANDALONE' and status != 'VERIFIED' or
            owner.get('relationship') != decision.get('relationship') or
            assessment.get('relationship') != decision.get('relationship')):
        return False
    if confidence_rule == 'CONF-06_SUPPORTED_EXPANDED_LEGAL_NAME':
        return _validate_conf06(company, candidate_url, decision, writer, fetcher)
    actual = evaluate(company, candidate_url, writer, fetcher)
    return actual.get('status') == status and all(decision.get(key) == actual.get(key) for key in (
        'status', 'rule_id', 'authorization_basis', 'confidence_rule_id', 'confidence_reasons',
        'verification_scope', 'relationship', 'supporting_evidence_ids',
        'supporting_observation_ids', 'operator_evidence', 'identity_evidence', 'relationship_evidence', 'site_operator_anchors', 'context'))
