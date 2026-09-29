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

GENERAL = ('glavni', 'main', 'centrala', 'switchboard', 'centralni', 'splošni', 'splosni')
GENERAL_EMAIL = ('general company contact', 'general contact', 'splošni kontakt', 'splosni kontakt',
                 'glavni kontakt', 'centralni kontakt')
OFFICE = ('pisarna', 'office', 'recepcija', 'reception', 'tajništvo', 'tajnistvo')
SALES = ('prodaja', 'sales', 'komerciala')
DEPARTMENT = ('oddelek', 'department', 'servis', 'support', 'računovodstvo', 'racunovodstvo', 'nabava', 'marketing')
PERSON_MOBILE = ('mobilni', 'mobile', 'gsm', 'direktor', 'vodja', 'manager')
TRANSACTIONAL = ('vračil', 'vracil', 'returns', 'reklamacij', 'orders', 'naročil', 'narocil', 'transaction')
LEGAL = ('zasebnost', 'privacy', 'legal', 'pravna', 'gdpr', 'pooblaščen', 'pooblascen')
FOREIGN_OFFICE = ('foreign office', 'foreign branch', 'podružnica', 'podruznica', 'subsidiary',
                  'office in', 'pisarna v', 'foreign market', 'tuji trg', 'mednarodn', 'international')


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
    if not owner or owner.get('status') not in ('VERIFIED', 'HIGH', 'MEDIUM') or not in_scope(source_url, owner.get('scope', '')):
        return 'UNCERTAIN', 'No usable first-party scope for this publication'
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
        # Default ranking may use only contexts that independently passed
        # attribution. Search candidates and rejected publications cannot lend
        # a stronger role to an otherwise attributable contact.
        ranking_items = [o for o, s, _ in decisions if s == 'ATTRIBUTED'] or [primary]
        ranking = ranking_context(company, kind, normalized, ranking_items, contact_role)
        contacts.append(dict(contact_id=uid(), contact_type=kind, raw_value=primary['raw_value'],
            normalized_value=normalized, primary_observation_id=primary['observation_id'],
            supporting_observation_ids_json=encode([o['observation_id'] for o in items]),
            attribution_status=status, attribution_reason=reason, rule_version=RULE_VERSION,
            roles_json=encode([contact_role]), role_basis_json=encode({
                'basis': 'email local-part and publication context' if kind == 'EMAIL' else 'phone publication context',
                'default_rank': ranking['rank'], 'ranking_reasons': ranking['reasons'],
                'ranking_observation_id': ranking['ranking_observation_id'],
                'country_context': ranking.get('country_context'),
                'not_person_identity': True,
                'explicit_company': bool(primary['value'].get('block', {}).get('explicit_company')),
                'contact_section': bool(primary['value'].get('block', {}).get('contact_section')),
                'observation_assessments': [{'observation_id': o['observation_id'], 'status': s, 'reason': r} for o, s, r in decisions]}),
            person_name=None, person_title=None, department=None, person_context_evidence_json='{}'))
    return contacts


def ranking_context(company, kind, normalized, observations, contact_role):
    contexts = []
    for observation in observations:
        value = observation.get('value') or {}
        headings = ' '.join(value.get('block', {}).get('headings', []))
        contexts.append(text(value.get('publication', '') + ' ' + headings))
    if kind == 'EMAIL':
        choices = []
        for index, (observation, context) in enumerate(zip(observations, contexts)):
            if any(w in context for w in TRANSACTIONAL): choice = 60, 'transactional/returns context'
            elif any(w in context for w in LEGAL): choice = 50, 'privacy/legal context'
            elif contact_role == 'PERSON': choice = 40, 'named-person mailbox'
            elif any(w in context for w in GENERAL_EMAIL): choice = 0, 'explicit general company contact'
            elif any(w in context for w in OFFICE): choice = 10, 'office/reception context'
            elif contact_role == 'GENERAL': choice = 15, 'general mailbox role'
            elif contact_role == 'SALES' or any(w in context for w in SALES): choice = 20, 'sales context'
            elif contact_role not in ('UNKNOWN', 'GENERAL'): choice = 30, 'department context'
            else: choice = 25, 'unclassified company mailbox'
            choices.append((*choice, index, observation['observation_id']))
        rank, reason, _, observation_id = min(choices, key=lambda item: (item[0], item[2]))
        return {'rank': rank, 'reasons': [reason], 'ranking_observation_id': observation_id}

    number = normalized.split(';', 1)[0]
    international = number.startswith('+')
    target_country = '386'  # Discovery v2 currently operates on Slovenian source entities.
    foreign = international and not number.startswith('+' + target_country)
    target_country_match = (not foreign) if international else None
    choices = []
    for index, (observation, context) in enumerate(zip(observations, contexts)):
        explicit_general = any(w in context for w in GENERAL)
        if explicit_general: rank, reason = 0, 'labelled main telephone/switchboard'
        elif any(w in context for w in OFFICE): rank, reason = 10, 'office/reception telephone'
        elif any(w in context for w in SALES): rank, reason = 30, 'sales telephone'
        elif any(w in context for w in DEPARTMENT): rank, reason = 40, 'department telephone'
        elif any(w in context for w in PERSON_MOBILE): rank, reason = 50, 'employee/mobile context'
        else: rank, reason = 20, 'general unlabeled company telephone'
        target = bool((observation.get('value') or {}).get('block', {}).get('target_entity'))
        foreign_context = any(w in context for w in FOREIGN_OFFICE)
        reasons = [reason]
        if foreign or (target_country_match is None and foreign_context):
            if explicit_general and target:
                reasons.append('foreign number explicitly labelled as target entity primary/general contact')
            else:
                rank += 100
                reasons.append('foreign or unknown-country number lacks explicit target-entity primary/general context')
            if foreign_context and not (explicit_general and target):
                rank += 50
                reasons.append('foreign office/market context')
        elif target_country_match is True:
            rank -= 2
            reasons.append('target-country telephone context')
        choices.append((rank, reasons, foreign_context, index, observation['observation_id']))
    rank, reasons, foreign_context, _, observation_id = min(choices, key=lambda item: (item[0], item[3]))
    return {'rank': rank, 'reasons': reasons, 'ranking_observation_id': observation_id,
            'country_context': {'normalized_international_number': number if international else None,
                                'target_country_match': target_country_match,
                                'foreign_context': foreign_context}}


def select_default(contacts, kind):
    candidates = [c for c in contacts if c['contact_type'] == kind and c['attribution_status'] == 'ATTRIBUTED'
                  and c['roles_json'] != encode(['PERSON'])]
    if not candidates:
        return None
    # Stable across result ordering and UUIDs. General mailboxes precede departments.
    def rank(c):
        roles = json.loads(c['roles_json'])
        basis = json.loads(c.get('role_basis_json', '{}'))
        category = basis.get('default_rank', 0 if 'GENERAL' in roles else (2 if 'UNKNOWN' in roles else 1))
        return category, not basis.get('explicit_company'), not basis.get('contact_section'), c['normalized_value']
    selected = min(candidates, key=rank)
    return selected['contact_id']
