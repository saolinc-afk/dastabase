"""Read-only Discovery v2 CSV export joined to canonical Dastabase companies."""
import argparse
import csv
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path


USABLE_WEBSITE_STATUSES = {'VERIFIED', 'HIGH', 'MEDIUM'}
COMPANY_COLUMNS = ('id', 'company_name', 'tax_number', 'registration_number', 'address',
                   'municipality', 'revenue_2025', 'employees_2025')
RESULT_SCHEMA = {
    'discovery_runs': {'run_id', 'status', 'engine_version', 'rule_version'},
    'discovery_run_companies': {'run_id', 'company_id', 'status', 'selected_attempt_id'},
    'discovery_company_results': {
        'result_id', 'run_id', 'attempt_id', 'company_id', 'website_status',
        'official_website', 'website_observation_id', 'website_evidence_ids_json',
        'default_email_contact_id', 'default_phone_contact_id', 'contact_outcome',
        'completed_at'},
    'discovery_contacts': {
        'contact_id', 'run_id', 'attempt_id', 'company_id', 'contact_type',
        'normalized_value', 'primary_observation_id', 'supporting_observation_ids_json',
        'attribution_status', 'rule_version', 'roles_json'},
}
HEADERS = [
    'company_id', 'company_name', 'tax_number', 'registration_number', 'address',
    'municipality', 'revenue_2025', 'employees_2025',
    'website', 'website_status', 'website_observation_id', 'website_evidence_ids',
    'default_email', 'email_role', 'email_attribution_status',
    'email_primary_observation_id', 'email_supporting_observation_ids', 'email_rule_version',
    'default_phone', 'phone_role', 'phone_attribution_status',
    'phone_primary_observation_id', 'phone_supporting_observation_ids', 'phone_rule_version',
    'discovery_run_id', 'discovery_source_results_db', 'discovery_result_id',
    'discovery_attempt_id', 'discovery_contact_outcome', 'discovery_run_status',
    'discovery_engine_version', 'discovery_rule_version', 'discovery_completed_at',
]


def readonly(path):
    path = Path(path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, isolation_level=None,
                           timeout=0.2)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return path, conn


def require_columns(conn, required, label):
    for table, columns in required.items():
        found = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        missing = columns-found
        if missing:
            raise ValueError(f'{label}: incompatible schema; {table} missing {sorted(missing)}')


def json_list(raw, label):
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f'{label}: malformed JSON list') from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f'{label}: expected a JSON string list')
    return value


def contact_fields(row, prefix, expected_type, source_label):
    contact_id = row[f'{prefix}_contact_id']
    if contact_id is None:
        return {f'default_{prefix}': '', f'{prefix}_role': '',
                f'{prefix}_attribution_status': '', f'{prefix}_primary_observation_id': '',
                f'{prefix}_supporting_observation_ids': '', f'{prefix}_rule_version': ''}
    if row[f'{prefix}_type'] != expected_type or row[f'{prefix}_attribution'] != 'ATTRIBUTED':
        raise ValueError(f'{source_label}: invalid default {prefix} reference {contact_id}')
    roles = json_list(row[f'{prefix}_roles'], f'{source_label} {prefix} roles')
    evidence = json_list(row[f'{prefix}_evidence'], f'{source_label} {prefix} evidence')
    return {f'default_{prefix}': row[f'{prefix}_value'],
            f'{prefix}_role': '|'.join(roles),
            f'{prefix}_attribution_status': row[f'{prefix}_attribution'],
            f'{prefix}_primary_observation_id': row[f'{prefix}_observation'],
            f'{prefix}_supporting_observation_ids': json.dumps(evidence, ensure_ascii=False,
                                                               separators=(',', ':')),
            f'{prefix}_rule_version': row[f'{prefix}_rule_version']}


def result_candidates(path, source_index, run_id=None):
    resolved, conn = readonly(path)
    try:
        require_columns(conn, RESULT_SCHEMA, str(resolved))
        if run_id is not None and conn.execute(
                'SELECT 1 FROM discovery_runs WHERE run_id=?', (run_id,)).fetchone() is None:
            raise ValueError(f'{resolved}: unknown requested run ID {run_id}')
        parameters = []
        run_filter = ''
        if run_id is not None:
            run_filter = ' AND r.run_id=?'
            parameters.append(run_id)
        query = '''SELECT r.*, run.status AS run_status, run.engine_version,
            run.rule_version AS run_rule_version,
            email.contact_id AS email_contact_id, email.contact_type AS email_type,
            email.normalized_value AS email_value, email.roles_json AS email_roles,
            email.attribution_status AS email_attribution,
            email.primary_observation_id AS email_observation,
            email.supporting_observation_ids_json AS email_evidence,
            email.rule_version AS email_rule_version,
            phone.contact_id AS phone_contact_id, phone.contact_type AS phone_type,
            phone.normalized_value AS phone_value, phone.roles_json AS phone_roles,
            phone.attribution_status AS phone_attribution,
            phone.primary_observation_id AS phone_observation,
            phone.supporting_observation_ids_json AS phone_evidence,
            phone.rule_version AS phone_rule_version
            FROM discovery_company_results r
            JOIN discovery_runs run ON run.run_id=r.run_id
            JOIN discovery_run_companies rc ON rc.run_id=r.run_id
              AND rc.company_id=r.company_id AND rc.selected_attempt_id=r.attempt_id
              AND rc.status IN ('COMPLETED','PARTIAL')
            LEFT JOIN discovery_contacts email ON email.contact_id=r.default_email_contact_id
              AND email.run_id=r.run_id AND email.attempt_id=r.attempt_id
              AND email.company_id=r.company_id
            LEFT JOIN discovery_contacts phone ON phone.contact_id=r.default_phone_contact_id
              AND phone.run_id=r.run_id AND phone.attempt_id=r.attempt_id
              AND phone.company_id=r.company_id
            WHERE 1=1''' + run_filter
        rows = []
        for stored in conn.execute(query, parameters):
            row = dict(stored)
            label = f'{resolved} run {row["run_id"]} company {row["company_id"]}'
            if row['default_email_contact_id'] and not row['email_contact_id']:
                raise ValueError(f'{label}: missing default email contact')
            if row['default_phone_contact_id'] and not row['phone_contact_id']:
                raise ValueError(f'{label}: missing default phone contact')
            website_evidence = json_list(row['website_evidence_ids_json'],
                                         f'{label} website evidence')
            website = row['official_website'] if row['website_status'] in USABLE_WEBSITE_STATUSES else ''
            if row['website_status'] in USABLE_WEBSITE_STATUSES and not website:
                raise ValueError(f'{label}: usable website status lacks official website')
            rows.append({
                'company_id': row['company_id'], 'website': website or '',
                'website_status': row['website_status'],
                'website_observation_id': row['website_observation_id'] or '',
                'website_evidence_ids': json.dumps(website_evidence, ensure_ascii=False,
                                                   separators=(',', ':')),
                **contact_fields(row, 'email', 'EMAIL', label),
                **contact_fields(row, 'phone', 'PHONE', label),
                'discovery_run_id': row['run_id'],
                'discovery_source_results_db': str(resolved),
                'discovery_result_id': row['result_id'],
                'discovery_attempt_id': row['attempt_id'],
                'discovery_contact_outcome': row['contact_outcome'],
                'discovery_run_status': row['run_status'],
                'discovery_engine_version': row['engine_version'],
                'discovery_rule_version': row['run_rule_version'],
                'discovery_completed_at': row['completed_at'],
                '_source_index': source_index,
            })
        return resolved, rows
    finally:
        conn.close()


def completed_timestamp(candidate):
    try:
        value = datetime.fromisoformat(candidate['discovery_completed_at'])
    except (TypeError, ValueError) as exc:
        raise ValueError('Duplicate company results require valid persisted completed_at '
                         'timestamps or explicit input-order precedence') from exc
    if value.utcoffset() is None:
        raise ValueError('Duplicate company results require timezone-aware completed_at '
                         'timestamps or explicit input-order precedence')
    return value


def newest_unique(candidates):
    ordered = sorted(((completed_timestamp(candidate), candidate) for candidate in candidates),
                     key=lambda item: item[0], reverse=True)
    if len(ordered) > 1 and ordered[0][0] == ordered[1][0]:
        raise ValueError('Duplicate company results have tied completed_at timestamps; '
                         'select a run or use explicit input-order precedence')
    return ordered[0][1]


def resolve_candidates(candidates, precedence):
    grouped = defaultdict(list)
    for candidate in candidates:
        grouped[candidate['company_id']].append(candidate)
    selected = {}
    for company_id, choices in grouped.items():
        if len(choices) == 1:
            selected[company_id] = choices[0]
        elif precedence == 'newest':
            selected[company_id] = newest_unique(choices)
        else:
            highest = min(choice['_source_index'] for choice in choices)
            within_source = [choice for choice in choices if choice['_source_index'] == highest]
            selected[company_id] = (within_source[0] if len(within_source) == 1
                                    else newest_unique(within_source))
    return selected


def canonical_companies(path, company_ids):
    resolved, conn = readonly(path)
    try:
        require_columns(conn, {'companies_lite': set(COMPANY_COLUMNS)}, str(resolved))
        if not company_ids:
            return resolved, {}
        rows = {}
        ordered = sorted(company_ids)
        for offset in range(0, len(ordered), 400):
            batch = ordered[offset:offset+400]
            placeholders = ','.join('?' for _ in batch)
            query = f'''SELECT {','.join(COMPANY_COLUMNS)} FROM companies_lite
                        WHERE id IN ({placeholders})'''
            rows.update({row['id']: dict(row) for row in conn.execute(query, batch)})
        missing = sorted(set(company_ids)-set(rows))
        if missing:
            raise ValueError(f'{resolved}: canonical companies missing IDs {missing}')
        return resolved, rows
    finally:
        conn.close()


def build_rows(source, results, company_ids=None, precedence='newest', run_ids=None):
    if precedence not in ('newest', 'input-order'):
        raise ValueError('precedence must be newest or input-order')
    if not results:
        raise ValueError('At least one Discovery v2 results database is required')
    run_ids = {str(Path(path).expanduser().resolve()): value
               for path, value in (run_ids or {}).items()}
    all_candidates = []
    resolved_results = []
    for index, result_path in enumerate(results):
        requested = run_ids.get(str(Path(result_path).expanduser().resolve()))
        resolved, candidates = result_candidates(result_path, index, requested)
        resolved_results.append(str(resolved))
        all_candidates.extend(candidates)
    unknown_filters = sorted(set(run_ids)-set(resolved_results))
    if unknown_filters:
        raise ValueError(f'Run selection supplied for an unknown results database: {unknown_filters}')
    selected = resolve_candidates(all_candidates, precedence)
    requested_ids = set(company_ids) if company_ids is not None else set(selected)
    if company_ids is not None and any(type(value) is not int or value < 0 for value in requested_ids):
        raise ValueError('Company IDs must be nonnegative integers')
    _, companies = canonical_companies(source, requested_ids)
    rows = []
    for company_id in sorted(requested_ids):
        company = companies[company_id]
        candidate = selected.get(company_id, {})
        row = {
            'company_id': company['id'], 'company_name': company['company_name'] or '',
            'tax_number': company['tax_number'] or '',
            'registration_number': company['registration_number'] or '',
            'address': company['address'] or '', 'municipality': company['municipality'] or '',
            'revenue_2025': company['revenue_2025'],
            'employees_2025': company['employees_2025'],
        }
        row.update({header: candidate.get(header, '') for header in HEADERS if header not in row})
        rows.append(row)
    return rows


def write_csv(output, rows, inputs=()):
    output = Path(output).expanduser().absolute()
    resolved_output = output.resolve()
    if any(resolved_output == Path(value).expanduser().resolve() for value in inputs):
        raise ValueError('CSV output must differ from every SQLite input')
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+output.name+'.', suffix='.tmp', dir=output.parent)
    try:
        with os.fdopen(fd, 'w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADERS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, output)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def parse_run_ids(values):
    result = {}
    for value in values:
        if '=' not in value:
            raise ValueError('--run-id must use RESULTS_DB=RUN_ID')
        path, run_id = value.rsplit('=', 1)
        if not path or not run_id or str(Path(path).expanduser().resolve()) in result:
            raise ValueError('Each --run-id must uniquely map RESULTS_DB=RUN_ID')
        result[str(Path(path).expanduser().resolve())] = run_id
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Canonical dastabase_lite.db')
    parser.add_argument('--results', action='append', required=True,
                        help='Discovery v2 results.sqlite3; repeat for multiple sources')
    parser.add_argument('--output', required=True, help='Destination CSV')
    parser.add_argument('--company-id', action='append', type=int,
                        help='Export an explicit canonical company ID; repeat as needed')
    parser.add_argument('--precedence', choices=('newest', 'input-order'), default='newest',
                        help='Duplicate policy; input-order explicitly prefers the first --results')
    parser.add_argument('--run-id', action='append', default=[], metavar='RESULTS_DB=RUN_ID',
                        help='Restrict one results database to an explicit run; repeat as needed')
    args = parser.parse_args(argv)
    try:
        run_ids = parse_run_ids(args.run_id)
        rows = build_rows(args.source, args.results, args.company_id, args.precedence, run_ids)
        write_csv(args.output, rows, [args.source, *args.results])
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f'error: {exc}\n')
    print(json.dumps({'output': str(Path(args.output).expanduser().absolute()),
                      'companies': len(rows)}, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
