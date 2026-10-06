"""Read-only access to accepted Dastabase company knowledge."""
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from discovery.domain_generator import normalize_domain
from discovery.domain_policy import blocks_official
from discovery_v2.evidence import public_email_domain
from discovery_v2.export import result_candidates

from control_room.matching import normalize_email


USABLE = {'VERIFIED', 'HIGH', 'MEDIUM'}
CANONICAL_REQUIRED = {'id', 'company_name', 'registration_number', 'tax_number',
                      'address', 'municipality'}
CANONICAL_COLUMNS = ('id', 'company_name', 'registration_number', 'tax_number',
    'address', 'municipality', 'revenue_2025', 'profit_2025', 'employees_2025',
    'assets_2025', 'capital_2025', 'gvin_company_id', 'gvin_detail_url',
    'financial_status', 'collected_at')


def readonly(path):
    resolved = Path(path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True,
                           isolation_level=None, timeout=.2)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return resolved, conn


def columns(conn, table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def require(found, expected, label):
    missing = expected - found
    if missing:
        raise ValueError(f'{label} missing required columns {sorted(missing)}')


def row_value(row, name):
    return row[name] if name in row.keys() else None


def timestamp(value):
    if not value:
        return datetime.min
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


@dataclass(frozen=True)
class KnowledgeFact:
    value: str
    status: str | None = None
    provenance: tuple[dict, ...] = ()
    conflicts: tuple[dict, ...] = ()
    observed_at: str | None = None
    stale: bool | None = None


@dataclass(frozen=True)
class CompanyKnowledgeSnapshot:
    company_id: int
    identity: dict
    financials: dict
    website: KnowledgeFact | None = None
    default_email: KnowledgeFact | None = None
    default_phone: KnowledgeFact | None = None
    conflicts: tuple[dict, ...] = ()
    missing_fields: tuple[str, ...] = ()
    stale_fields: tuple[str, ...] = ()

    def has_website(self): return self.website is not None
    def has_reusable_email(self): return self.default_email is not None
    def has_reusable_phone(self): return self.default_phone is not None

    def sufficient_for_import(self):
        return self.has_website() and self.has_reusable_email()


class KnowledgeRepository:
    """Immutable snapshot of explicitly configured read-only sources.

    Discovery registry order wins with ``input-order``. With ``newest``, the
    newest accepted field wins. REVIEW and missing fields never erase older
    accepted facts. Every accepted conflicting value remains visible.
    """

    def __init__(self, canonical_path, discovery_results=(), *,
                 precedence='newest', run_ids=None, stale_policy=None):
        if precedence not in ('newest', 'input-order'):
            raise ValueError('precedence must be newest or input-order')
        self.precedence = precedence
        self.stale_policy = stale_policy
        self.run_ids = {str(Path(key).expanduser().resolve()): value
                        for key, value in (run_ids or {}).items()}
        self.canonical_path, self.companies = self._load_companies(canonical_path)
        self.by_id = {row['id']: row for row in self.companies}
        self.discovery_results = tuple(Path(path).expanduser().resolve(strict=True)
                                       for path in discovery_results)
        self.historical_domain_candidates = defaultdict(set)
        self.historical_email_candidates = defaultdict(set)
        self._sparrow = self._load_sparrow()
        self._discovery = self._load_discovery()
        self._snapshots = {company_id: self._snapshot(company_id)
                           for company_id in self.by_id}

    @staticmethod
    def _load_companies(path):
        resolved, conn = readonly(path)
        try:
            found = columns(conn, 'companies_lite')
            require(found, CANONICAL_REQUIRED, f'{resolved}: companies_lite')
            selected = [name for name in CANONICAL_COLUMNS if name in found]
            rows = []
            for stored in conn.execute(
                    f'SELECT {",".join(selected)} FROM companies_lite ORDER BY id'):
                row = {name: None for name in CANONICAL_COLUMNS}
                row.update(dict(stored)); rows.append(row)
            if not rows:
                raise ValueError(f'{resolved}: companies_lite is empty')
            return resolved, rows
        finally:
            conn.close()

    @staticmethod
    def _provenance(source, namespace, database, company_id, **values):
        return {'source_type': source, 'source_namespace': namespace,
                'source_database': str(database), 'source_company_id': company_id,
                **values}

    def _load_sparrow(self):
        result = {company_id: {'websites': [], 'emails': []} for company_id in self.by_id}
        _, conn = readonly(self.canonical_path)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'website_discovery', 'email_discovery'} <= tables:
                missing = sorted({'website_discovery', 'email_discovery'} - tables)
                raise ValueError(f'{self.canonical_path}: missing SPARROW tables {missing}')
            require(columns(conn, 'website_discovery'),
                {'id','company_id','website','status','verified_scope','relationship'},
                f'{self.canonical_path}: website_discovery')
            require(columns(conn, 'email_discovery'),
                {'id','company_id','email','website'},
                f'{self.canonical_path}: email_discovery')
            for row in conn.execute('''SELECT * FROM website_discovery WHERE
                    TRIM(COALESCE(website,''))<>'' OR
                    TRIM(COALESCE(verified_scope,''))<>'' '''):
                if row['company_id'] not in self.by_id: continue
                host = normalize_domain(row['verified_scope'] or row['website'])
                if host: self.historical_domain_candidates[host].add(row['company_id'])
            accepted = {}
            for row in conn.execute('''SELECT w.* FROM website_discovery w JOIN
                (SELECT company_id,MAX(id) id FROM website_discovery GROUP BY company_id) latest
                ON latest.id=w.id WHERE w.status='VERIFIED' AND w.relationship='LEGAL_ENTITY'
                AND TRIM(COALESCE(w.website,''))<>''
                AND TRIM(COALESCE(w.verified_scope,''))<>'' '''):
                company_id = row['company_id']
                if company_id not in self.by_id: continue
                value = row['verified_scope'] or row['website']
                host = normalize_domain(value)
                if not host or blocks_official(value): continue
                accepted[company_id] = host
                prov = self._provenance('SPARROW','SPARROW_0.9',self.canonical_path,
                    company_id, record_id=row['id'], table='website_discovery',
                    confidence=row_value(row,'confidence'), status=row['status'],
                    rule_version=row_value(row,'rule_version'), evidence_reference=row['id'],
                    observed_at=row_value(row,'checked_at') or row_value(row,'discovered_at'))
                result[company_id]['websites'].append(
                    {'value': value, 'status': 'VERIFIED', 'provenance': prov})
            for row in conn.execute('SELECT * FROM email_discovery ORDER BY company_id,LOWER(email),id'):
                company_id = row['company_id']
                email = normalize_email(row['email'])
                if not email or company_id not in self.by_id: continue
                email_host = email.rsplit('@',1)[1]
                if not public_email_domain(email_host):
                    self.historical_email_candidates[email_host].add(company_id)
                official_host = accepted.get(company_id)
                stored_host = normalize_domain(row['website'])
                raw_evidence = row_value(row,'evidence_json')
                try:
                    email_evidence = json.loads(raw_evidence) if raw_evidence else {}
                except (TypeError,json.JSONDecodeError):
                    email_evidence = {'invalid': True}
                attribution = email_evidence.get('attribution', {})
                contact_block = email_evidence.get('contact_block', {})
                evidence_conflict = (
                    email_evidence.get('invalid') is True or
                    contact_block.get('other_entity') is True or
                    (email_evidence.get('domain_relation') not in
                     (None,'same_company_domain')) or
                    (attribution and attribution.get('attributable') is not True) or
                    (attribution.get('entity_id') not in (None,company_id)) or
                    (attribution.get('ownership_scope') and
                     normalize_domain(attribution['ownership_scope']) != official_host))
                if (not official_host or public_email_domain(email_host) or evidence_conflict or
                        normalize_domain(email_host) != official_host or
                        stored_host != official_host or
                        (row_value(row,'page_url') and blocks_official(row['page_url']))):
                    continue
                prov = self._provenance('SPARROW','SPARROW_0.9',self.canonical_path,
                    company_id, record_id=row['id'], table='email_discovery',
                    confidence=row_value(row,'confidence'), status='ATTRIBUTED_FIRST_PARTY_DOMAIN',
                    role='UNKNOWN', evidence_reference=row['id'],
                    page_url=row_value(row,'page_url'), observed_at=row_value(row,'checked_at') or
                    row_value(row,'discovered_at'))
                result[company_id]['emails'].append(
                    {'value': email, 'status': 'ATTRIBUTED_FIRST_PARTY_DOMAIN',
                     'provenance': prov})
            return result
        finally:
            conn.close()

    def _load_discovery(self):
        result = defaultdict(lambda: {'websites': [], 'emails': [], 'phones': []})
        for source_index, path in enumerate(self.discovery_results):
            # Non-selected observations remain candidate-only identity evidence.
            # They never become accepted current knowledge.
            namespaces = {}
            _, conn = readonly(path)
            try:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if 'discovery_observations' in tables and {
                        'company_id','observation_type','normalized_value'} <= columns(
                            conn,'discovery_observations'):
                    for row in conn.execute('''SELECT company_id,observation_type,
                        normalized_value FROM discovery_observations WHERE observation_type IN
                        ('WEBSITE_CANDIDATE','EMAIL_CANDIDATE')'''):
                        if row['company_id'] not in self.by_id: continue
                        if row['observation_type']=='WEBSITE_CANDIDATE':
                            host=normalize_domain(row['normalized_value'])
                        else:
                            email=normalize_email(row['normalized_value'])
                            host=email.rsplit('@',1)[1] if email else ''
                        if host and not public_email_domain(host):
                            self.historical_domain_candidates[host].add(row['company_id'])
                if ('discovery_run_companies' in tables and
                        {'run_id','company_id','source_namespace'} <= columns(
                            conn,'discovery_run_companies')):
                    namespaces = {(row['run_id'],row['company_id']):row['source_namespace']
                        for row in conn.execute('''SELECT run_id,company_id,source_namespace
                            FROM discovery_run_companies''')}
            finally:
                conn.close()
            requested = self.run_ids.get(str(path))
            _, rows = result_candidates(path, source_index, requested)
            for row in rows:
                company_id = row['company_id']
                if company_id not in self.by_id: continue
                namespace = namespaces.get((row['discovery_run_id'],company_id),'DISCOVERY_V2')
                base = self._provenance('DISCOVERY_V2',namespace,path,company_id,
                    run_id=row['discovery_run_id'], attempt_id=row['discovery_attempt_id'],
                    result_id=row['discovery_result_id'], completed_at=row['discovery_completed_at'],
                    rule_version=row['discovery_rule_version'], source_index=source_index)
                if row['website'] and row['website_status'] in USABLE:
                    result[company_id]['websites'].append({'value':row['website'],
                        'status':row['website_status'], 'completed_at':row['discovery_completed_at'],
                        'source_index':source_index, 'provenance':{**base,
                        'status':row['website_status'],
                        'evidence_reference':row['website_evidence_ids'],
                        'observation_id':row['website_observation_id']}})
                    if row['default_email']:
                        result[company_id]['emails'].append({'value':row['default_email'],
                            'status':row['email_attribution_status'],
                            'completed_at':row['discovery_completed_at'],'source_index':source_index,
                            'provenance':{**base,'status':row['email_attribution_status'],
                            'role':row['email_role'],
                            'evidence_reference':row['email_supporting_observation_ids'],
                            'observation_id':row['email_primary_observation_id']}})
                    if row['default_phone']:
                        result[company_id]['phones'].append({'value':row['default_phone'],
                            'status':row['phone_attribution_status'],
                            'completed_at':row['discovery_completed_at'],'source_index':source_index,
                            'provenance':{**base,'status':row['phone_attribution_status'],
                            'role':row['phone_role'],
                            'evidence_reference':row['phone_supporting_observation_ids'],
                            'observation_id':row['phone_primary_observation_id']}})
        return result

    def _ordered(self, entries):
        if self.precedence == 'input-order':
            return sorted(entries, key=lambda item: (
                item.get('source_index', 10**9), -timestamp(item.get('completed_at')).timestamp()))
        return sorted(entries, key=lambda item: timestamp(item.get('completed_at')), reverse=True)

    def _fact(self, discovery, sparrow=()):
        entries = self._ordered(discovery)
        entries.extend(sparrow)
        if not entries: return None
        selected = entries[0]
        same = [entry for entry in entries if entry['value'] == selected['value']]
        distinct = []
        for entry in entries:
            if entry['value'] != selected['value'] and entry['value'] not in {
                    item['value'] for item in distinct}:
                distinct.append(entry)
        conflicts = tuple({'selected_value':selected['value'],
            'conflicting_value':entry['value'], 'provenance':entry['provenance']}
            for entry in distinct)
        observed = selected.get('completed_at') or selected['provenance'].get('observed_at')
        stale = self.stale_policy(selected) if self.stale_policy else None
        return KnowledgeFact(selected['value'], selected.get('status'),
            tuple(entry['provenance'] for entry in same), conflicts, observed, stale)

    def _snapshot(self, company_id):
        company = self.by_id[company_id]
        discovered = self._discovery[company_id]
        sparrow = self._sparrow[company_id]
        website = self._fact(discovered['websites'], sparrow['websites'])
        email = self._fact(discovered['emails'], sparrow['emails'])
        phone = self._fact(discovered['phones'])
        facts = {'website':website, 'default_email':email, 'default_phone':phone}
        conflicts = tuple(conflict for fact in facts.values() if fact
                          for conflict in fact.conflicts)
        missing = tuple(name for name, fact in facts.items() if fact is None)
        stale = tuple(name for name, fact in facts.items() if fact and fact.stale is True)
        identity = {key:company.get(key) for key in ('id','company_name','tax_number',
                    'registration_number','address','municipality')}
        financials = {key:company.get(key) for key in ('revenue_2025','profit_2025',
                      'employees_2025','assets_2025','capital_2025')}
        return CompanyKnowledgeSnapshot(company_id,identity,financials,website,email,phone,
                                        conflicts,missing,stale)

    def snapshot(self, company_id):
        try: return self._snapshots[company_id]
        except KeyError as exc: raise ValueError(f'Unknown canonical company {company_id}') from exc

    def missing(self, company_id, requested=('website','default_email','default_phone')):
        snapshot = self.snapshot(company_id)
        return tuple(name for name in requested if getattr(snapshot,name) is None)

    def as_enrichment(self):
        values = {}
        for company_id, snapshot in self._snapshots.items():
            provenance = []
            for fact in (snapshot.website,snapshot.default_email,snapshot.default_phone):
                if fact: provenance.extend(fact.provenance)
            sources = list(dict.fromkeys(item['source_type'] for item in provenance))
            values[company_id] = {'website':snapshot.website.value if snapshot.website else None,
                'website_status':snapshot.website.status if snapshot.website else None,
                'default_email':snapshot.default_email.value if snapshot.default_email else None,
                'default_phone':snapshot.default_phone.value if snapshot.default_phone else None,
                'sources':sources, 'provenance':provenance,
                'knowledge_conflicts':list(snapshot.conflicts)}
            if snapshot.default_email:
                values[company_id]['accepted_emails']=[{'value':snapshot.default_email.value,
                    'source':snapshot.default_email.provenance[0]['source_type'],
                    'record_id':snapshot.default_email.provenance[0].get('record_id') or
                                snapshot.default_email.provenance[0].get('result_id')}]
        return values

    def inventory(self):
        counts = Counter()
        source_counts = Counter()
        run_counts = Counter()
        for snapshot in self._snapshots.values():
            counts['companies'] += 1
            for key, value in snapshot.identity.items():
                if value not in (None,''): counts[f'identity_{key}'] += 1
            for key, value in snapshot.financials.items():
                if value is not None: counts[f'financial_{key}'] += 1
            for name, fact in (('websites',snapshot.website),('emails',snapshot.default_email),
                               ('phones',snapshot.default_phone)):
                if fact:
                    counts[f'accepted_{name}'] += 1
                    counts[f'{name}_with_conflicts'] += bool(fact.conflicts)
                    counts[f'{name}_conflicting_values'] += len(fact.conflicts)
                    for item in fact.provenance:
                        source_counts[(name,item['source_type'])] += 1
                        if item.get('run_id'):
                            run_counts[(item['source_database'],item['run_id'],name)] += 1
            counts['conflicts'] += bool(snapshot.conflicts)
            counts['sufficient_for_import'] += snapshot.sufficient_for_import()
        return {'coverage':dict(counts),
                'by_source':{'|'.join(key):value for key,value in source_counts.items()},
                'by_discovery_run':{'|'.join(key):value for key,value in run_counts.items()}}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Read-only accepted-knowledge inventory')
    parser.add_argument('--canonical',required=True)
    parser.add_argument('--results',action='append',default=[])
    parser.add_argument('--precedence',choices=('newest','input-order'),default='newest')
    args=parser.parse_args(argv)
    print(json.dumps(KnowledgeRepository(args.canonical,args.results,
        precedence=args.precedence).inventory(),ensure_ascii=False,sort_keys=True,indent=2))


if __name__ == '__main__':
    main()
