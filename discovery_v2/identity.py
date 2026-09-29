"""Conservative, evidence-local identity claims for Discovery v2."""
import re
import unicodedata

CLAIM_VERSION = 2
LEGAL_FORM_RE = re.compile(r'\b(?:d\s*[. ]\s*o\s*[. ]\s*o\.?|doo)\b', re.I)
LEGAL_NAME_RE = re.compile(r'([A-ZČŠŽ0-9][A-ZČŠŽ0-9&+\-., ]{1,100}?\s+(?:d\s*[. ]\s*o\s*[. ]\s*o\.?|doo))', re.I)
IDENTIFIER_RE = re.compile(
    r'(?<!\w)(?P<label>mati[čc]na(?:\s+[šs]tevilka)?|registration(?:\s+number)?|company\s+id|'
    r'dav[čc]na(?:\s+[šs]tevilka)?|vat|id\s+za\s+ddv)(?!\w)\s*[:.]?\s*(?P<si>SI\s*)?(?P<value>\d(?:[ .-]?\d){5,})(?![\w.-]|[ ]+\d)', re.I)
ADDRESS_ABBREVIATIONS = ((r'\bul\.?\b', 'ulica'), (r'\bc\.?\b', 'cesta'), (r'\btrg\b', 'trg'))


def fold(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', str(value or '').lower())
                    if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', value).split())


def normalize_legal_name(value, remove_form=False):
    value = fold(value)
    value = re.sub(r'\bd\s*o\s*o\b', 'doo', value)
    if remove_form:
        value = re.sub(r'\bdoo\b', ' ', value)
    return ' '.join(value.split())


def name_forms(value):
    return {'full': normalize_legal_name(value),
            'distinctive': normalize_legal_name(value, remove_form=True),
            'raw': value}


def normalize_address(value):
    value = fold(value)
    for pattern, replacement in ADDRESS_ABBREVIATIONS:
        value = re.sub(pattern, replacement, value)
    return ' '.join(value.split())


def address_parts(company):
    raw = str(company.get('address') or '')
    normalized = normalize_address(raw)
    street_part = normalize_address(raw.split(',')[0])
    match = re.match(r'(.+?)\s+(\d+[a-z]?(?:\s*[-/]\s*\d+[a-z]?)?)$', street_part)
    street = match.group(1) if match else street_part
    house = re.sub(r'\s+', '', match.group(2)) if match else ''
    postal_match = re.search(r'(?<!\d)(\d{4})(?!\d)', raw)
    postal = postal_match.group(1) if postal_match else ''
    municipality = normalize_address(company.get('municipality') or '')
    locality = re.sub(r'^\d{4}\s+', '', municipality)
    return {'raw': raw, 'full': normalized, 'street_full': street_part, 'street': street,
            'house_number': house, 'postal_code': postal, 'municipality': municipality,
            'locality': locality, 'country': 'SI'}


def normalize_identifier(value):
    raw = str(value or '').strip()
    return {'digits': re.sub(r'\D', '', raw), 'si_prefix': bool(re.match(r'^\s*SI', raw, re.I)), 'raw': raw}


def plausible_identifier(kind, digits):
    return (kind == 'TAX_NUMBER' and len(digits) == 8) or (kind == 'REGISTRATION_NUMBER' and 7 <= len(digits) <= 10)


def identifiers_equal(kind, stored, observed):
    """Compare complete identifiers, including Slovenia's narrow MŠ +000 form."""
    stored_digits = normalize_identifier(stored)['digits']
    observed_digits = normalize_identifier(observed)['digits']
    if not stored_digits or not observed_digits:
        return False
    if stored_digits == observed_digits:
        return True
    return (kind == 'REGISTRATION_NUMBER' and len(stored_digits) == 7
            and observed_digits == stored_digits + '000')


def claim(kind, raw, normalized, status, confidence, **qualifiers):
    return dict(kind=kind, raw=raw, normalized=normalized,
                value=dict(claim_version=CLAIM_VERSION, confidence=confidence,
                           verification_status=status, qualifiers=qualifiers))


def extract_claims(company, source_text):
    """Extract only exact/bounded claims; no fuzzy claim can verify ownership."""
    source_text = str(source_text or '')
    normalized_text = normalize_legal_name(source_text)
    target_names = name_forms(company.get('company_name', ''))
    target_address = address_parts(company)
    claims = []

    if target_names['full'] and re.search(r'(?<!\w)' + re.escape(target_names['full']) + r'(?!\w)', normalized_text):
        claims.append(claim('LEGAL_NAME', company['company_name'], target_names['full'],
                            'EXACT_MATCH', 'HIGH', distinctive_name=target_names['distinctive']))

    observed_names = []
    for match in LEGAL_NAME_RE.finditer(source_text):
        raw = match.group(1).strip(' ,.;')
        forms = name_forms(raw)
        if forms['full'] == target_names['full'] or forms['full'] in observed_names:
            continue
        observed_names.append(forms['full'])
        target_words = target_names['distinctive'].split()
        observed_words = forms['distinctive'].split()
        expanded = bool(target_words and len(''.join(target_words)) >= 4
                        and all(word in observed_words for word in target_words)
                        and len(observed_words) > len(target_words))
        claims.append(claim('ALIAS', raw, forms['full'], 'UNVERIFIED', 'LOW',
                            alias_kind='EXPANDED_LEGAL_NAME' if expanded else 'NAME_VARIANT',
                            canonical_name=company.get('company_name', ''), observed_name=raw,
                            distinctive_name=forms['distinctive']))

    target_ids = {kind: normalize_identifier(company.get(field)) for kind, field in
                  (('TAX_NUMBER', 'tax_number'), ('REGISTRATION_NUMBER', 'registration_number'))}
    for match in IDENTIFIER_RE.finditer(source_text):
        label = fold(match.group('label'))
        kind = 'TAX_NUMBER' if any(x in label for x in ('davcna', 'vat', 'ddv')) else 'REGISTRATION_NUMBER'
        observed = normalize_identifier(('SI' if match.group('si') else '') + match.group('value'))
        if not plausible_identifier(kind, observed['digits']) or (observed['si_prefix'] and kind != 'TAX_NUMBER'):
            claims.append(claim('NUMERIC_IDENTIFIER', match.group(0), observed['digits'],
                                'UNVERIFIED', 'LOW', label=match.group('label'), exclusion_only=True))
            continue
        target = target_ids[kind]
        status = ('EXACT_MATCH' if identifiers_equal(kind, target['digits'], observed['digits'])
                  else 'CONFLICT')
        claims.append(claim(kind, match.group(0), observed['digits'], status, 'HIGH',
                            si_prefix=observed['si_prefix'], label=match.group('label')))

    if target_address['street_full'] and re.search(r'(?<!\w)' + re.escape(target_address['street_full']) + r'(?!\w)', normalize_address(source_text)):
        claims.append(claim('STREET', target_address['street_full'], target_address['street_full'],
                            'EXACT_MATCH', 'HIGH', house_number=target_address['house_number']))
    # A full address is one contiguous street/house/postal/locality sequence.
    # Newlines mark publication boundaries and must not complete an address.
    locality = target_address['locality']
    postal = target_address['postal_code']
    suffix = ((re.escape(postal) + r'\s+') if postal else '') + re.escape(locality)
    pattern = r'(?<!\w)' + re.escape(target_address['street_full']) + r'\s+' + suffix + r'(?!\w)'
    ambiguous_house = bool(re.search(r'(?<!\w)' + re.escape(target_address['house_number']) + r'\s*[/\-]\s*\d', source_text)) if target_address['house_number'] else True
    address_context = bool(not ambiguous_house and target_address['house_number'] and locality and any(
        re.search(pattern, normalize_address(line)) for line in source_text.splitlines()))
    if address_context:
        claims.append(claim('ADDRESS', target_address['raw'], target_address['full'], 'EXACT_MATCH', 'HIGH',
                            **{k: v for k, v in target_address.items() if k not in ('raw', 'full')}))
    if target_address['postal_code'] and re.search(r'(?<!\d)' + target_address['postal_code'] + r'(?!\d)', source_text):
        claims.append(claim('POSTAL_CODE', target_address['postal_code'], target_address['postal_code'],
                            'EXACT_MATCH', 'MEDIUM'))
    if target_address['municipality'] and target_address['municipality'] in normalize_address(source_text):
        claims.append(claim('MUNICIPALITY', company.get('municipality', ''), target_address['municipality'],
                            'EXACT_MATCH', 'LOW'))
    return claims


def support_aliases(claims):
    """Only explicitly extracted, identifier-corroborated mappings are supported."""
    return claims


def relationship_claims(company, publication):
    """Narrow affirmative statements; incidental target mentions are insufficient."""
    value = normalize_legal_name(publication)
    target = re.escape(normalize_legal_name(company.get('company_name', '')))
    if not target:
        return []
    claims = []
    # Start of the bounded statement prevents quoted/negated operator language.
    operator = re.search(r'^(?:website (?:is )?(?:operated|owned) by|upravljavec spletne(?:ga mesta| strani))\s+' + target + r'(?!\w)', value)
    if operator:
        claims.append(claim('SITE_OPERATOR', publication, company['company_name'], 'EXACT_MATCH', 'HIGH',
                            basis='explicit_operator_statement'))
    address = re.escape(normalize_legal_name(address_parts(company)['raw']))
    group = re.search(r'^(?:website (?:is )?(?:operated|owned) by )?' + target + r'(?:\s+' + address + r')?\s+(?:is a |is |je )?'
                      r'(?:member of group|part of group|subsidiary of|clan skupine|del skupine)\b', value)
    if group:
        claims.append(claim('ENTITY_RELATIONSHIP', publication, 'SUBSIDIARY_ON_GROUP_DOMAIN', 'EXACT_MATCH', 'HIGH',
                            basis='explicit_group_membership'))
    identifier = any(c['kind'] in ('TAX_NUMBER', 'REGISTRATION_NUMBER') and
                     c['value']['verification_status'] == 'EXACT_MATCH' for c in extract_claims(company, publication))
    mapping = re.search(r'\b' + target + r'\s+(?:trading as|formerly known as)\s+([a-z][a-z0-9 ]{1,70}?)(?=\s+(?:vat|id za ddv|registration)|$)', value)
    if operator and mapping and identifier:
        claims.append(claim('ALIAS', mapping.group(1), mapping.group(1), 'ALIAS_SUPPORTED', 'HIGH',
                            alias_kind='EXPLICIT_MAPPING', basis='explicit_legal_mapping'))
        claims.append(claim('ENTITY_RELATIONSHIP', mapping.group(0), 'BRAND_OF_ENTITY', 'EXACT_MATCH', 'HIGH',
                            basis='explicit_legal_mapping'))
    return claims
