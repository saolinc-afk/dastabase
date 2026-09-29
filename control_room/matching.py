"""Conservative read-only matching of upload rows to canonical companies."""
import difflib
import re
import sqlite3
import unicodedata


LEGAL_FORMS = re.compile(r'\b(?:d\s*o\s*o|s\s*p|d\s*d|d\s*n\s*o|k\s*d)\b')


def normalize_text(value):
    text = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower()
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text).split())


def normalize_name(value):
    return ' '.join(LEGAL_FORMS.sub(' ', normalize_text(value)).split())


def normalize_tax(value):
    text = re.sub(r'\s+', '', str(value or '')).upper()
    if text.startswith('SI'):
        text = text[2:]
    return ''.join(character for character in text if character.isdigit())


def normalize_registration(value):
    return ''.join(character for character in str(value or '') if character.isdigit())


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
