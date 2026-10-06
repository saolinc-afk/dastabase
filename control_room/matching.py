"""Conservative read-only matching of upload rows to canonical companies."""
import difflib
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass

from discovery_v2.evidence import public_email_domain
from matching.company_matcher import (extract_email_domain, normalize_phone as
                                      normalize_candidate_phone)


LEGAL_FORMS = re.compile(r'\b(?:d\s*o\s*o|s\s*p|d\s*d|d\s*n\s*o|k\s*d)\b')
RAW_LEGAL_FORM = re.compile(
    r'\b(?:d\s*[.\-]?\s*o\s*[.\-]?\s*o|s\s*[.\-]?\s*p|d\s*[.\-]?\s*d|'
    r'd\s*[.\-]?\s*n\s*[.\-]?\s*o|k\s*[.\-]?\s*d)\b', re.I)
BRANCH_SUFFIX = re.compile(
    r'(?:\s*[,;:/\-]\s*|\s+)(?:p\s*[.\-]?\s*e\s*[.]?|poslovna\s+enota|'
    r'podru[zž]nica)\b.*$', re.I)


def normalize_text(value):
    text = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower()
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text).split())


def normalize_name(value):
    return ' '.join(LEGAL_FORMS.sub(' ', normalize_text(value)).split())


def normalize_name_preserving_diacritics(value):
    text = str(value or '').strip().lower()
    text = ''.join(character if character.isalnum() else ' ' for character in text)
    return ' '.join(LEGAL_FORMS.sub(' ', ' '.join(text.split())).split())


def has_explicit_legal_form(value):
    return bool(RAW_LEGAL_FORM.search(str(value or '')))


def canonical_name_aliases(value):
    """Return deterministic structural aliases, never fuzzy similarity."""
    raw = str(value or '').strip()
    aliases = []

    def add(source, rule):
        alias = normalize_name(source)
        if alias:
            aliases.append((alias, rule, normalize_name_preserving_diacritics(source)))

    legal = RAW_LEGAL_FORM.search(raw)
    if legal:
        add(raw[:legal.start()], 'LEGAL_CORE')
    if ',' in raw:
        add(raw.split(',', 1)[0], 'LEADING_CLAUSE')
    if legal and re.search(r's\s*[.\-]?\s*p', legal.group(0), re.I) and ',' in raw:
        add(raw.rsplit(',', 1)[-1], 'PROPRIETOR')
    for alias, rule, preserved in list(aliases) + [(
            normalize_name(raw), 'FULL_NAME', normalize_name_preserving_diacritics(raw))]:
        compact = alias.replace(' ', '')
        if compact and compact != alias:
            aliases.append((compact, 'COMPACT_'+rule, preserved.replace(' ', '')))
    words = normalize_name(raw).split()
    if len(words) >= 3:
        aliases.append((''.join(word[0] for word in words), 'ACRONYM',
                        ''.join(word[0] for word in words)))
    lexical_words = [word for word in re.findall(
        r'[^\W_]+(?:-[^\W_]+)*', raw.lower(), re.UNICODE)
        if word not in {'in', 'ter', 'za'}]
    if len(lexical_words) >= 3:
        initials = ''.join(word[0] for word in lexical_words)
        aliases.append((normalize_text(initials), 'LEXICAL_ACRONYM', initials))
        # Many organization names end in a locality. This shorter form is only
        # a review candidate; acronym aliases never authorize by themselves.
        if len(lexical_words) >= 4:
            without_last = ''.join(word[0] for word in lexical_words[:-1])
            aliases.append((normalize_text(without_last),
                            'LEXICAL_ACRONYM_WITHOUT_SUFFIX', without_last))
    return tuple(dict.fromkeys(aliases))


def submitted_name_aliases(value):
    raw = str(value or '').strip()
    aliases = []

    def add(source, rule):
        alias = normalize_name(source)
        if alias:
            aliases.append((alias, rule, normalize_name_preserving_diacritics(source)))
            compact = alias.replace(' ', '')
            if compact != alias:
                aliases.append((compact, 'COMPACT_'+rule,
                                normalize_name_preserving_diacritics(source).replace(' ', '')))

    add(raw, 'SUBMITTED')
    without_branch = BRANCH_SUFFIX.sub('', raw).strip()
    if without_branch != raw:
        add(without_branch, 'BRANCH_QUALIFIER')
    return tuple(dict.fromkeys(aliases))


def normalize_tax(value):
    text = re.sub(r'\s+', '', str(value or '')).upper()
    if text.startswith('SI'):
        text = text[2:]
    return ''.join(character for character in text if character.isdigit())


def normalize_registration(value):
    return ''.join(character for character in str(value or '') if character.isdigit())


def normalize_email(value):
    value = str(value or '').strip().lower()
    if value.count('@') != 1:
        return ''
    local, domain = value.rsplit('@', 1)
    domain = extract_email_domain(value)
    return f'{local}@{domain}' if local and domain else ''


def normalize_phone(value):
    return normalize_candidate_phone(value)


@dataclass(frozen=True)
class MatchEvidence:
    kind: str
    normalized_value: str
    company_ids: tuple[int, ...]
    strength: str


@dataclass(frozen=True)
class ImportMatch:
    row_key: object
    outcome: str
    company_id: int | None
    match_method: str | None
    evidence: tuple[MatchEvidence, ...]
    candidate_company_ids: tuple[int, ...]
    conflicts: tuple[str, ...]
    has_reusable_enrichment: bool
    route_hint: str
    ai_eligibility: str = 'NOT_EVALUATED'

    def as_dict(self):
        return asdict(self)


def read_companies(path):
    uri = f'file:{path}?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(companies_lite)')}
        required = {'id','company_name','tax_number','registration_number','address','municipality'}
        if not required <= columns:
            raise ValueError('Canonical companies_lite schema is incompatible')
        return [dict(row) for row in conn.execute('''SELECT id,company_name,tax_number,
            registration_number,address,municipality FROM companies_lite ORDER BY id''')]
    finally:
        conn.close()


class CanonicalMatcher:
    def __init__(self, companies):
        self.companies = companies
        self.by_id = {row['id']: row for row in companies}
        self.tax = self._index(normalize_tax, 'tax_number')
        self.registration = self._index(normalize_registration, 'registration_number')
        self.names = self._index(normalize_name, 'company_name')

    def _index(self, normalizer, field):
        result = {}
        for company in self.companies:
            value = normalizer(company.get(field))
            if value:
                result.setdefault(value, []).append(company)
        return result

    @staticmethod
    def _candidate(company, method, rank=1, similarity=None, reasons=()):
        return {'company_id': company['id'], 'rank': rank, 'match_method': method,
                'similarity': similarity, 'reasons': list(reasons), 'company': company}

    def match(self, row):
        for value, index, method in (
            (row['normalized_tax_number'], self.tax, 'TAX_EXACT'),
            (row['normalized_registration_number'], self.registration, 'REGISTRATION_EXACT')):
            if value and value in index:
                hits = index[value]
                candidates = [self._candidate(company, method, rank) for rank, company in enumerate(hits, 1)]
                return ('MATCHED' if len(hits) == 1 else 'AMBIGUOUS', candidates)
        name = row['normalized_name']
        if name and name in self.names:
            hits = self.names[name]
            location = row['normalized_address'] or row['normalized_municipality']
            if location:
                coherent = []
                for company in hits:
                    address = normalize_text(company.get('address'))
                    municipality = normalize_text(company.get('municipality'))
                    if (row['normalized_address'] and row['normalized_address'] == address) or \
                       (row['normalized_municipality'] and row['normalized_municipality'] == municipality):
                        coherent.append(company)
                if coherent:
                    candidates = [self._candidate(c, 'NAME_LOCATION', rank,
                        reasons=('normalized legal name and location agree',)) for rank, c in enumerate(coherent, 1)]
                    return ('MATCHED' if len(coherent) == 1 else 'AMBIGUOUS', candidates)
            candidates = [self._candidate(c, 'NAME_EXACT', rank,
                reasons=('normalized legal name agrees',)) for rank, c in enumerate(hits, 1)]
            return ('MATCHED' if len(hits) == 1 else 'AMBIGUOUS', candidates)
        if name:
            scored = []
            for known, companies in self.names.items():
                ratio = difflib.SequenceMatcher(None, name, known).ratio()
                if ratio >= 0.82:
                    scored.extend((ratio, company) for company in companies)
            scored.sort(key=lambda item: (-item[0], item[1]['id']))
            candidates = [self._candidate(company, 'FUZZY_CANDIDATE', rank, round(score, 4),
                ('similar normalized legal name; manual review required',))
                for rank, (score, company) in enumerate(scored[:5], 1)]
            if candidates:
                return 'AMBIGUOUS', candidates
        return 'NOT_FOUND', []


class ImportMatcher:
    """Categorical matcher over an EnrichmentIndex; no search or AI side effects."""

    def __init__(self, index):
        self.index = index
        self.fuzzy = CanonicalMatcher(index.companies)

    @staticmethod
    def _value(row, *names):
        return next((row.get(name) for name in names if row.get(name)), '')

    def _evidence(self, kind, value, index, strength):
        hits = tuple(sorted(index.get(value, ()))) if value else ()
        return MatchEvidence(kind, value, hits, strength) if value else None

    def match(self, row, row_key=None):
        raw_name = self._value(row, 'company_name', 'normalized_name')
        name = normalize_name(raw_name)
        tax = normalize_tax(self._value(row, 'tax_number', 'normalized_tax_number'))
        registration = normalize_registration(self._value(
            row, 'registration_number', 'normalized_registration_number'))
        address = normalize_text(self._value(row, 'address', 'normalized_address'))
        municipality = normalize_text(self._value(
            row, 'municipality', 'normalized_municipality'))
        email = normalize_email(self._value(row, 'email', 'normalized_email'))
        email_domain = extract_email_domain(email)
        phone = normalize_phone(self._value(row, 'phone', 'normalized_phone'))

        evidence = [
            self._evidence('TAX_EXACT', tax, self.index.tax, 'DECISIVE'),
            self._evidence('REGISTRATION_EXACT', registration, self.index.registration, 'DECISIVE'),
            self._evidence('NAME_EXACT', name, self.index.names, 'STRONG'),
            self._evidence('ADDRESS_EXACT', address, self.index.addresses, 'SUPPORTING'),
            self._evidence('MUNICIPALITY_EXACT', municipality, self.index.municipalities, 'SUPPORTING'),
            self._evidence('EMAIL_EXACT', email, self.index.emails, 'STRONG'),
            (None if not email_domain or public_email_domain(email_domain) else
             self._evidence('EMAIL_DOMAIN', email_domain, self.index.domains, 'STRONG')),
            self._evidence('PHONE_EXACT', phone, self.index.phones, 'SUPPORTING'),
        ]
        evidence = tuple(item for item in evidence if item is not None)
        by_kind = {item.kind: set(item.company_ids) for item in evidence}
        conflicts = []

        decisive = [by_kind[kind] for kind in ('TAX_EXACT', 'REGISTRATION_EXACT')
                    if by_kind.get(kind)]
        strong = [by_kind[kind] for kind in ('NAME_EXACT', 'EMAIL_EXACT', 'EMAIL_DOMAIN')
                  if by_kind.get(kind)]
        if decisive:
            authorized = set.intersection(*decisive)
            if not authorized:
                conflicts.append('CONFLICTING_IDENTIFIERS')
            for signal in strong:
                if authorized and not authorized & signal:
                    conflicts.append('IDENTIFIER_CONFLICTS_WITH_IDENTITY_SIGNAL')
            if not conflicts and len(authorized) == 1:
                return self._matched(row_key, authorized.pop(),
                    'TAX_EXACT' if by_kind.get('TAX_EXACT') else 'REGISTRATION_EXACT', evidence)
            return self._unresolved(row_key, 'AMBIGUOUS', evidence, conflicts)

        exact_names = by_kind.get('NAME_EXACT', set())
        if exact_names:
            candidates = set(exact_names)
            location_signals = [by_kind[kind] for kind in ('ADDRESS_EXACT', 'MUNICIPALITY_EXACT')
                                if by_kind.get(kind)]
            identity_signals = [by_kind[kind] for kind in ('EMAIL_EXACT', 'EMAIL_DOMAIN')
                                if by_kind.get(kind)]
            for signal in identity_signals:
                if not candidates & signal:
                    conflicts.append('NAME_CONFLICTS_WITH_EMAIL_OR_DOMAIN')
                else:
                    candidates &= signal
            if not conflicts and len(candidates) > 1:
                for signal in location_signals:
                    narrowed = candidates & signal
                    if narrowed:
                        candidates = narrowed
            if not conflicts and len(candidates) == 1:
                company_id = next(iter(candidates))
                corroborated = bool(identity_signals or any(
                    company_id in signal for signal in location_signals))
                submitted_preserved = normalize_name_preserving_diacritics(raw_name)
                canonical_preserved = normalize_name_preserving_diacritics(
                    self.index.by_id[company_id]['company_name'])
                tokens = name.split()
                safe_name = (corroborated or (submitted_preserved == canonical_preserved
                    and ((len(tokens) >= 2 and len(name) >= 4)
                         or (has_explicit_legal_form(raw_name) and len(name) >= 3))))
                if not safe_name:
                    conflicts.append('EXACT_NAME_REQUIRES_CORROBORATION')
                    return self._unresolved(row_key, 'AMBIGUOUS', evidence, conflicts)
                method = ('NAME_DOMAIN' if identity_signals else
                          'NAME_LOCATION' if location_signals else 'NAME_EXACT')
                return self._matched(row_key, company_id, method, evidence)
            return self._unresolved(row_key, 'AMBIGUOUS', evidence, conflicts)

        alias_evidence = []
        alias_matches = []
        for alias, submitted_rule, submitted_preserved in submitted_name_aliases(raw_name):
            if submitted_rule == 'BRANCH_QUALIFIER':
                for company_id in sorted(self.index.names.get(alias, ())):
                    canonical_preserved = normalize_name_preserving_diacritics(
                        self.index.by_id[company_id]['company_name'])
                    alias_matches.append((company_id, alias, submitted_rule,
                                          submitted_preserved, 'FULL_NAME',
                                          canonical_preserved))
            for company_id in sorted(self.index.aliases.get(alias, ())):
                for canonical_rule, canonical_preserved in self.index.alias_rules.get(
                        (alias, company_id), ()):
                    alias_matches.append((company_id, alias, submitted_rule,
                                          submitted_preserved, canonical_rule,
                                          canonical_preserved))
            hits = set(self.index.aliases.get(alias, ()))
            if submitted_rule == 'BRANCH_QUALIFIER':
                hits.update(self.index.names.get(alias, ()))
            hits = tuple(sorted(hits))
            if hits:
                alias_evidence.append(MatchEvidence('CONTROLLED_ALIAS', alias, hits,
                                                    'CANDIDATE_ONLY'))
        alias_candidates = {item[0] for item in alias_matches}
        if alias_candidates:
            evidence += tuple(alias_evidence)
            if len(alias_candidates) == 1:
                company_id = next(iter(alias_candidates))
                identity_hit = company_id in (by_kind.get('EMAIL_EXACT', set()) |
                                              by_kind.get('EMAIL_DOMAIN', set()))
                authorized = []
                for item in alias_matches:
                    _, alias, submitted_rule, submitted_preserved, canonical_rule, canonical_preserved = item
                    tokens = alias.split()
                    preserved_equal = submitted_preserved == canonical_preserved
                    proprietor = (canonical_rule == 'PROPRIETOR'
                                  and has_explicit_legal_form(raw_name)
                                  and len(tokens) >= 2)
                    compact = (canonical_rule.startswith('COMPACT_')
                               and len(alias) >= 6 and preserved_equal
                               and (submitted_rule.startswith('COMPACT_')
                                    or normalize_name(raw_name) == alias))
                    branch = (submitted_rule == 'BRANCH_QUALIFIER'
                              and (identity_hit or len(tokens) >= 2
                                   or (has_explicit_legal_form(raw_name)
                                       and len(alias) >= 5))
                              and preserved_equal)
                    legal_core = (canonical_rule in ('LEGAL_CORE', 'LEADING_CLAUSE')
                                  and ((len(tokens) >= 2 and len(alias) >= 8)
                                       or (len(tokens) == 1 and len(alias) >= 5
                                           and has_explicit_legal_form(raw_name)))
                                  and preserved_equal)
                    distinctive_folded_core = (
                        canonical_rule in ('LEGAL_CORE', 'LEADING_CLAUSE')
                        and len(tokens) >= 2 and len(alias) >= 10
                        and normalize_name(raw_name) == alias)
                    if proprietor or compact or branch or legal_core or distinctive_folded_core:
                        authorized.append((canonical_rule, submitted_rule))
                if authorized:
                    priority = {'PROPRIETOR': 0, 'LEGAL_CORE': 1,
                                'LEADING_CLAUSE': 2}
                    canonical_rule, submitted_rule = min(
                        authorized, key=lambda item: (priority.get(item[0], 3), item))
                    rule = ('BRANCH_ALIAS' if submitted_rule == 'BRANCH_QUALIFIER'
                            else canonical_rule)
                    evidence += (MatchEvidence('ALIAS_RULE', rule, (company_id,), 'STRONG'),)
                    return self._matched(row_key, company_id, 'ALIAS_'+rule, evidence)
            return self._unresolved(row_key, 'AMBIGUOUS', evidence,
                                    ('CONTROLLED_ALIAS_REVIEW',))

        domain_candidates = by_kind.get('EMAIL_DOMAIN', set()) | by_kind.get('EMAIL_EXACT', set())
        if domain_candidates:
            return self._unresolved(row_key, 'AMBIGUOUS', evidence,
                                    ('COMPANY_NAME_CORROBORATION_REQUIRED',))

        # Reuse the established conservative fuzzy implementation only to expose
        # review candidates. It never authorizes an Import & Enrich match.
        legacy_row = {'normalized_name': name, 'normalized_tax_number': '',
                      'normalized_registration_number': '', 'normalized_address': address,
                      'normalized_municipality': municipality}
        status, fuzzy = self.fuzzy.match(legacy_row)
        if status == 'AMBIGUOUS':
            fuzzy_evidence = tuple(MatchEvidence('FUZZY_NAME', str(item['similarity']),
                (item['company_id'],), 'CANDIDATE_ONLY') for item in fuzzy)
            return self._unresolved(row_key, 'AMBIGUOUS', evidence + fuzzy_evidence, ())
        return self._unresolved(row_key, 'UNRESOLVED', evidence, ())

    def _matched(self, row_key, company_id, method, evidence):
        reusable = self.index.has_reusable_enrichment(company_id)
        return ImportMatch(row_key, 'MATCHED', company_id, method, evidence,
            (company_id,), (), reusable,
            'RESOLVED_WITHOUT_AI' if reusable else 'MATCHED_REQUIRES_ENRICHMENT')

    @staticmethod
    def _unresolved(row_key, outcome, evidence, conflicts):
        candidates = tuple(sorted({company_id for item in evidence
                                   for company_id in item.company_ids}))
        return ImportMatch(row_key, outcome, None, None, evidence, candidates,
            tuple(dict.fromkeys(conflicts)), False, 'UNRESOLVED_IDENTITY')
