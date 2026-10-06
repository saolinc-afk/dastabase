"""Read-only canonical and accepted-enrichment index for Import & Enrich."""
from collections import defaultdict
from pathlib import Path
import sqlite3

from discovery.domain_generator import normalize_domain
from discovery_v2.evidence import public_email_domain
from discovery_v2.export import result_candidates, resolve_candidates

from control_room.matching import (normalize_email, normalize_name, normalize_phone,
                                   normalize_registration, normalize_tax, normalize_text,
                                   canonical_name_aliases)


CANONICAL_COLUMNS = (
    'id', 'company_name', 'registration_number', 'tax_number', 'address',
    'municipality', 'revenue_2025', 'profit_2025', 'employees_2025',
    'assets_2025', 'capital_2025', 'gvin_company_id', 'gvin_detail_url',
    'financial_status', 'collected_at',
)
CANONICAL_REQUIRED = {'id', 'company_name', 'registration_number', 'tax_number',
                      'address', 'municipality'}
SPARROW_WEBSITE_REQUIRED = {'id', 'company_id', 'website', 'status', 'verified_scope',
                            'relationship'}
SPARROW_EMAIL_REQUIRED = {'id', 'company_id', 'email', 'website'}


def _readonly(path):
    resolved = Path(path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True,
                           isolation_level=None, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return resolved, conn


def _columns(conn, table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def _require(found, required, label):
    missing = required - found
    if missing:
        raise ValueError(f'{label} missing required columns {sorted(missing)}')


def _domain(value):
    return normalize_domain(value or '')


class EnrichmentIndex:
    """Immutable in-memory projection; every SQLite input is opened read-only."""

    def __init__(self, canonical_path, discovery_results=(), *, precedence='newest',
                 run_ids=None):
        if precedence not in ('newest', 'input-order'):
            raise ValueError('precedence must be newest or input-order')
        self.canonical_path, self.companies = self._canonical(canonical_path)
        self.discovery_results = tuple(
            Path(path).expanduser().resolve(strict=True) for path in discovery_results)
        self.by_id = {company['id']: company for company in self.companies}
        self.enrichment = {company['id']: {
            'website': None, 'website_status': None, 'default_email': None,
            'default_phone': None, 'sources': [], 'provenance': []}
            for company in self.companies}
        self._load_sparrow()
        self._load_discovery(precedence, run_ids or {})
        self._build_indexes()

    @staticmethod
    def _canonical(path):
        resolved, conn = _readonly(path)
        try:
            found = _columns(conn, 'companies_lite')
            _require(found, CANONICAL_REQUIRED, f'{resolved}: companies_lite')
            selected = [column for column in CANONICAL_COLUMNS if column in found]
            rows = []
            for stored in conn.execute(
                    f'SELECT {",".join(selected)} FROM companies_lite ORDER BY id'):
                row = {column: None for column in CANONICAL_COLUMNS}
                row.update(dict(stored))
                rows.append(row)
            if not rows:
                raise ValueError(f'{resolved}: companies_lite is empty')
            return resolved, rows
        finally:
            conn.close()

    def _load_sparrow(self):
        _, conn = _readonly(self.canonical_path)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if 'website_discovery' not in tables and 'email_discovery' not in tables:
                return
            if not {'website_discovery', 'email_discovery'} <= tables:
                raise ValueError(f'{self.canonical_path}: incomplete SPARROW schema')
            _require(_columns(conn, 'website_discovery'), SPARROW_WEBSITE_REQUIRED,
                     f'{self.canonical_path}: website_discovery')
            _require(_columns(conn, 'email_discovery'), SPARROW_EMAIL_REQUIRED,
                     f'{self.canonical_path}: email_discovery')
            accepted = {}
            query = '''SELECT w.* FROM website_discovery w JOIN
                (SELECT company_id,MAX(id) AS id FROM website_discovery GROUP BY company_id) latest
                ON latest.id=w.id WHERE w.status='VERIFIED'
                  AND TRIM(COALESCE(w.website,''))<>''
                  AND TRIM(COALESCE(w.verified_scope,''))<>''
                  AND w.relationship='LEGAL_ENTITY' '''
            for row in conn.execute(query):
                if row['company_id'] not in self.by_id:
                    continue
                domain = _domain(row['verified_scope'] or row['website'])
                if not domain:
                    continue
                accepted[row['company_id']] = domain
                record = self.enrichment[row['company_id']]
                record.update(website=row['verified_scope'] or row['website'],
                              website_status='VERIFIED')
                record['sources'].append('SPARROW')
                record['provenance'].append({'source': 'SPARROW', 'table': 'website_discovery',
                                             'record_id': row['id']})
            # Historical emails support identity only when their domain agrees with
            # the strict accepted first-party host. They are not promoted to defaults.
            for row in conn.execute('SELECT id,company_id,email,website FROM email_discovery'):
                domain = accepted.get(row['company_id'])
                email = normalize_email(row['email'])
                if (domain and email and not public_email_domain(email.rsplit('@', 1)[1])
                        and _domain(email.rsplit('@', 1)[1]) == domain):
                    self.enrichment[row['company_id']].setdefault('accepted_emails', []).append({
                        'value': email, 'source': 'SPARROW', 'record_id': row['id']})
        finally:
            conn.close()

    def _load_discovery(self, precedence, run_ids):
        if not self.discovery_results:
            return
        normalized_run_ids = {
            str(Path(path).expanduser().resolve()): value for path, value in run_ids.items()}
        candidates = []
        for source_index, path in enumerate(self.discovery_results):
            requested = normalized_run_ids.get(str(path))
            _, rows = result_candidates(path, source_index, requested)
            candidates.extend(rows)
        selected = resolve_candidates(candidates, precedence)
        for company_id, row in selected.items():
            if company_id not in self.by_id:
                continue
            record = self.enrichment[company_id]
            if row.get('website'):
                record.update(website=row['website'], website_status=row['website_status'])
            # Contacts are identity input only when they belong to a result with
            # an accepted official website. REVIEW contacts remain provenance in
            # their Discovery database and cannot identify an uploaded company.
            if row.get('website') and row.get('default_email'):
                record['default_email'] = row['default_email']
                record.setdefault('accepted_emails', []).append({
                    'value': normalize_email(row['default_email']), 'source': 'DISCOVERY_V2',
                    'record_id': row.get('discovery_result_id')})
            if row.get('website') and row.get('default_phone'):
                record['default_phone'] = row['default_phone']
            record['sources'].append('DISCOVERY_V2')
            record['provenance'].append({
                'source': 'DISCOVERY_V2', 'results_db': row['discovery_source_results_db'],
                'run_id': row['discovery_run_id'], 'result_id': row['discovery_result_id']})

    @staticmethod
    def _add(index, value, company_id):
        if value:
            index[value].add(company_id)

    def _build_indexes(self):
        self.tax = defaultdict(set)
        self.registration = defaultdict(set)
        self.names = defaultdict(set)
        self.addresses = defaultdict(set)
        self.municipalities = defaultdict(set)
        self.domains = defaultdict(set)
        self.emails = defaultdict(set)
        self.phones = defaultdict(set)
        self.aliases = defaultdict(set)
        self.alias_rules = defaultdict(set)
        for company in self.companies:
            company_id = company['id']
            self._add(self.tax, normalize_tax(company['tax_number']), company_id)
            self._add(self.registration, normalize_registration(company['registration_number']), company_id)
            self._add(self.names, normalize_name(company['company_name']), company_id)
            for alias, rule, preserved in canonical_name_aliases(company['company_name']):
                if alias != normalize_name(company['company_name']):
                    self._add(self.aliases, alias, company_id)
                    self.alias_rules[(alias, company_id)].add((rule, preserved))
            self._add(self.addresses, normalize_text(company['address']), company_id)
            self._add(self.municipalities, normalize_text(company['municipality']), company_id)
            enrichment = self.enrichment[company_id]
            website_domain = _domain(enrichment.get('website'))
            self._add(self.domains, website_domain, company_id)
            for item in enrichment.get('accepted_emails', []):
                email = normalize_email(item['value'])
                if not email:
                    continue
                domain = email.rsplit('@', 1)[1]
                if public_email_domain(domain):
                    continue
                self._add(self.emails, email, company_id)
                # An explicitly attributed external mailbox is useful exact-email
                # evidence, but its provider domain does not identify the company.
                if domain == website_domain:
                    self._add(self.domains, domain, company_id)
            self._add(self.phones, normalize_phone(enrichment.get('default_phone')), company_id)

    def company(self, company_id):
        company = self.by_id[company_id]
        return {**company, 'enrichment': self.enrichment[company_id]}

    def has_reusable_enrichment(self, company_id):
        record = self.enrichment[company_id]
        return bool(record.get('website') or record.get('default_email') or
                    record.get('default_phone'))
