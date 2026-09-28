"""Attempt-local contact attribution, role hints and deterministic defaults."""
import re
import json
from collections import defaultdict

from discovery.ownership import in_scope, legal_conflict, text
from discovery.website_verifier import score_page
from discovery_v2 import RULE_VERSION
from discovery_v2.store import encode, uid

ROLE_PREFIXES = {
    'GENERAL': ('info', 'office', 'kontakt', 'contact', 'hello', 'tajnistvo'),
    'SALES': ('sales', 'prodaja', 'komerciala'),
    'MANAGEMENT': ('uprava', 'management', 'direktor', 'direkcija'),
    'PURCHASING': ('nabava', 'purchasing', 'procurement'),
    'ACCOUNTING': ('racunovodstvo', 'accounting', 'finance'),
    'HR': ('hr', 'kadrovska', 'zaposlitev'),
    'SUPPORT': ('support', 'podpora', 'servis'),
    'MARKETING': ('marketing', 'trzenje'),
}


def role(value, kind, company=None):
    if kind == 'PHONE':
        return 'UNKNOWN'
    local = value.split('@')[0].lower()
    for name, prefixes in ROLE_PREFIXES.items():
        if any(local == p or local.startswith(p + '.') or local.startswith(p + '_') or (p in ('prodaja', 'sales', 'servis') and local.startswith(p)) for p in prefixes):
            return name
    if local.startswith(('rezervni.deli', 'spare.parts')):
        return 'SUPPORT'
    if company:
        from discovery.ownership import name
        if local.split('.')[0] == name(company['company_name']).replace(' ', ''):
            return 'GENERAL'
    if re.fullmatch(r'[^\W\d_]{2,}[._-][^\W\d_]{2,}', local):
        return 'PERSON'
    return 'UNKNOWN'


def attribution(company, observation, owner, responses, corroborated=False):
    value = observation['value']
    if value.get('source_kind') != 'FETCHED_PAGE':
        return 'CANDIDATE', 'Search observation requires independent entity/contact corroboration'
    source_url = value['source_url']
    if not owner or owner.get('status') != 'VERIFIED' or not in_scope(source_url, owner.get('scope', '')):
        return 'UNCERTAIN', 'No verified first-party scope for this publication'
    response = responses[source_url]
    signals = score_page(company, response)
    if signals.get('third_party') or signals.get('rejected') or signals.get('ownership', {}).get('legal_conflict'):
        return 'REJECTED', 'Third-party or conflicting page context'
    block = value.get('block', {})
    publication = value['publication']
    if value.get('visible_email') and value['visible_email'].lower() != observation['normalized_value'].lower():
        return 'REJECTED', 'Displayed email and link target disagree'
    if value.get('display_conflict') or block.get('other_entity') or legal_conflict(company, publication):
        return 'REJECTED', 'Conflicting contact publication/entity context'
    if any(word in text(publication) for word in ('website by', 'developed by', 'designed by', 'izdelava splet', 'powered by')):
        return 'REJECTED', 'Agency/vendor publication context'
    if block.get('target_entity'):
        return 'ATTRIBUTED', 'Explicit target-entity contact on verified scope'
    if block.get('single_entity_page') and block.get('contact_section'):
        return 'ATTRIBUTED', 'First-party company contact section on verified scope'
    if block.get('single_entity_page') and corroborated:
        return 'ATTRIBUTED', 'First-party publication corroborated by independent entity-specific search evidence'
    return 'UNCERTAIN', 'Contact lacks sufficient target-entity context'


def resolve_contacts(company, observations, owner, responses):
    groups = defaultdict(list)
    for observation in observations:
        if observation['observation_type'] in ('EMAIL_CANDIDATE', 'PHONE_CANDIDATE'):
            groups[(observation['observation_type'].split('_')[0], observation['normalized_value'])].append(observation)
    contacts = []
    order = {'ATTRIBUTED': 0, 'CANDIDATE': 1, 'UNCERTAIN': 2, 'REJECTED': 3}
    for (kind, normalized), items in sorted(groups.items()):
        from discovery.domain_generator import normalize_domain
        corroborated = any(o['value'].get('source_kind') == 'SEARCH_RESULT' and o['value'].get('identity_match') and (not owner or normalize_domain(o['value'].get('source_url', '')) != normalize_domain(owner.get('scope', ''))) for o in items)
        decisions = [(observation, *attribution(company, observation, owner, responses, corroborated)) for observation in items]
        primary, status, reason = min(decisions, key=lambda d: (order[d[1]], not d[0]['value'].get('block', {}).get('explicit_company'), not d[0]['value'].get('block', {}).get('contact_section')))
        contact_role = role(normalized, kind, company)
        contacts.append(dict(contact_id=uid(), contact_type=kind, raw_value=primary['raw_value'],
            normalized_value=normalized, primary_observation_id=primary['observation_id'],
            supporting_observation_ids_json=encode([o['observation_id'] for o in items]),
            attribution_status=status, attribution_reason=reason, rule_version=RULE_VERSION,
            roles_json=encode([contact_role]), role_basis_json=encode({
                'basis': 'email local-part hint' if kind == 'EMAIL' else 'unclassified phone',
                'not_person_identity': True,
                'explicit_company': bool(primary['value'].get('block', {}).get('explicit_company')),
                'contact_section': bool(primary['value'].get('block', {}).get('contact_section')),
                'observation_assessments': [{'observation_id': o['observation_id'], 'status': s, 'reason': r} for o, s, r in decisions]}),
            person_name=None, person_title=None, department=None, person_context_evidence_json='{}'))
    return contacts


def select_default(contacts, kind):
    candidates = [c for c in contacts if c['contact_type'] == kind and c['attribution_status'] == 'ATTRIBUTED'
                  and c['roles_json'] != encode(['PERSON'])]
    if not candidates:
        return None
    # Stable across result ordering and UUIDs. General mailboxes precede departments.
    def rank(c):
        roles = json.loads(c['roles_json'])
        basis = json.loads(c.get('role_basis_json', '{}'))
        category = 0 if 'GENERAL' in roles else (2 if 'UNKNOWN' in roles else 1)
        return category, not basis.get('explicit_company'), not basis.get('contact_section'), c['normalized_value']
    selected = min(candidates, key=rank)
    return selected['contact_id']
