"""Build a compact human-review CSV from a completed ambiguity audit."""
import argparse
import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from discovery.domain_generator import normalize_domain
from discovery_v2.ambiguity_audit import DIAGNOSTIC_CATEGORIES
from discovery_v2.coverage_audit import (_atomic_csv, columns, load_master,
                                         readonly)
from discovery_v2.store import APPLICATION_ID, SCHEMA_VERSION


OUTPUT_NAMES = ('ambiguity_human_review_100.csv',
                'AMBIGUITY_HUMAN_REVIEW_GUIDE.md')
PRIMARY_QUOTAS = {
    'NO_MEANINGFUL_IDENTITY_SUPPORT': 30,
    'THIRD_PARTY_NOISE_DOMINATES': 20,
    'ONE_MODERATE_LEADER': 20,
    'ONE_CLEAR_EVIDENCE_LEADER': 20,
}
TAIL_CATEGORIES = (
    'MANY_LOW_SIGNAL_CANDIDATES', 'CROSS_COUNTRY_COLLISION',
    'TWO_CLOSE_CANDIDATES', 'SAME_DOMAIN_VARIANTS',
)
REVIEW_RESULTS = (
    'CORRECT_CANDIDATE_PRESENT', 'OFFICIAL_EXISTS_BUT_NOT_FOUND',
    'NO_OFFICIAL_WEBSITE', 'INSUFFICIENT_TO_TELL',
    'WRONG_ENTITY / CROSS_COUNTRY',
)
INPUT_REQUIRED = {
    'company_id', 'company_name', 'tax_number', 'registration_number',
    'address', 'attempt_id', 'accepted_website', 'plausible_host_count',
    'plausible_registrable_domain_count', 'diagnostic_category',
    'leader_domain', 'leader_score', 'runner_up_score', 'leader_margin',
    'candidate_details_json',
}
OUTPUT_HEADERS = (
    'SAMPLE_POSITION', 'SAMPLE_SEED', 'SAMPLE_SOURCE',
    'canonical_company_id', 'company_name', 'tax_number',
    'registration_number', 'address', 'ambiguity_category',
    'candidate_count', 'registrable_domain_count',
    'current_selected_website', 'current_selected_domain',
    'diagnostic_leader', 'leader_score', 'leader_tier',
    'margin_to_next_candidate', 'DETERMINISTIC_FAILURE_HINT',
    'CANDIDATES_COMPACT', 'EVIDENCE_PREVIEW',
    'ANDREJ_OFFICIAL_WEBSITE', 'ANDREJ_RESULT',
    'ANDREJ_BEST_CANDIDATE', 'ANDREJ_NOTES',
)
RESULT_REQUIRED = {
    'discovery_runs': {'run_id'},
    'discovery_attempts': {'attempt_id', 'run_id', 'company_id'},
    'discovery_evidence': {
        'evidence_id', 'run_id', 'attempt_id', 'company_id', 'source_kind',
        'provider', 'query_type', 'query_text', 'result_rank', 'result_url',
        'result_host', 'title', 'snippet_body', 'requested_url', 'final_url',
    },
}


def _read_csv(path):
    resolved = Path(path).expanduser().resolve(strict=True)
    with resolved.open('r', newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        missing = INPUT_REQUIRED - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f'{resolved}: missing ambiguity columns {sorted(missing)}')
        rows = list(reader)
    seen = set()
    for row_number, row in enumerate(rows, 2):
        try:
            company_id = int(row['company_id'])
        except (TypeError, ValueError):
            raise ValueError(f'{resolved}:{row_number}: invalid company_id') from None
        if company_id in seen:
            raise ValueError(f'{resolved}:{row_number}: duplicate company_id {company_id}')
        seen.add(company_id)
        row['company_id'] = company_id
        if row['diagnostic_category'] not in DIAGNOSTIC_CATEGORIES:
            raise ValueError(f'{resolved}:{row_number}: unknown diagnostic category')
        try:
            candidates = json.loads(row['candidate_details_json'])
        except (TypeError, json.JSONDecodeError):
            raise ValueError(f'{resolved}:{row_number}: invalid candidate_details_json') from None
        if not isinstance(candidates, list) or not all(isinstance(item, dict)
                                                       for item in candidates):
            raise ValueError(f'{resolved}:{row_number}: candidates must be a JSON array')
        row['_candidates'] = candidates
    return resolved, rows


def _stable(rows, seed):
    return sorted(rows, key=lambda row: hashlib.sha256(
        f'{seed}:{row["company_id"]}'.encode()).hexdigest())


def _sample_counts(rows):
    return Counter(row['diagnostic_category'] for row in rows)


def sample_is_sufficient(rows):
    if len(rows) != 100 or len({row['company_id'] for row in rows}) != 100:
        return False
    counts = _sample_counts(rows)
    if any(abs(counts[category] - quota) > 2
           for category, quota in PRIMARY_QUOTAS.items()):
        return False
    return 8 <= sum(counts[category] for category in TAIL_CATEGORIES) <= 12


def stratified_sample(rows, seed='ambiguity-review-v1', sample_size=100):
    if sample_size != 100:
        raise ValueError('The human-review contract requires sample_size=100')
    groups = defaultdict(list)
    for row in rows:
        groups[row['diagnostic_category']].append(row)
    for category in groups:
        groups[category] = _stable(groups[category], seed)

    selected = []
    selected_ids = set()
    for category, quota in PRIMARY_QUOTAS.items():
        for row in groups[category][:quota]:
            selected.append(row)
            selected_ids.add(row['company_id'])

    tail_groups = {category: [row for row in groups[category]
                              if row['company_id'] not in selected_ids]
                   for category in TAIL_CATEGORIES}
    index = 0
    tail_target = min(10, sample_size - len(selected))
    tail_added = 0
    while tail_added < tail_target:
        progressed = False
        for category in TAIL_CATEGORIES:
            if index < len(tail_groups[category]):
                row = tail_groups[category][index]
                selected.append(row)
                selected_ids.add(row['company_id'])
                tail_added += 1
                progressed = True
                if tail_added == tail_target:
                    break
        if not progressed:
            break
        index += 1

    if len(selected) < sample_size:
        remaining = _stable([row for row in rows
            if row['company_id'] not in selected_ids], seed + ':fill')
        selected.extend(remaining[:sample_size-len(selected)])
    if len(selected) < sample_size:
        raise ValueError(f'Only {len(selected)} unique ambiguity rows available; 100 required')
    return selected[:sample_size]


def select_sample(companies_path, existing_sample_path=None,
                  seed='ambiguity-review-v1'):
    companies_resolved, rows = _read_csv(companies_path)
    if existing_sample_path:
        sample_resolved, existing = _read_csv(existing_sample_path)
        source_ids = {row['company_id'] for row in rows}
        if (sample_is_sufficient(existing) and
                all(row['company_id'] in source_ids for row in existing)):
            return companies_resolved, sample_resolved, existing, 'EXISTING_SAMPLE'
    return (companies_resolved,
            Path(existing_sample_path).expanduser().resolve() if existing_sample_path else None,
            stratified_sample(rows, seed), 'DERIVED_STRATIFIED')


def _validate_master(master, sample):
    master_path, companies, _ = load_master(master)
    for row in sample:
        company = companies.get(row['company_id'])
        if company is None:
            raise ValueError(f'Company {row["company_id"]} is outside canonical master')
        for csv_field, master_field in (
                ('tax_number','tax_number'),
                ('registration_number','registration_number')):
            expected = str(company.get(master_field) or '').strip()
            observed = str(row.get(csv_field) or '').strip()
            if expected and observed and expected != observed:
                raise ValueError(f'Company {row["company_id"]} {csv_field} mismatch')
    return master_path


def _chunks(values, size=500):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index:index+size]


def _load_evidence(results, run_id, sample, max_candidates):
    results_path, conn = readonly(results)
    try:
        if (conn.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or
                conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
            raise ValueError(f'{results_path}: incompatible Discovery result database')
        for table, expected in RESULT_REQUIRED.items():
            missing = expected - columns(conn, table)
            if missing:
                raise ValueError(f'{results_path}: {table} missing {sorted(missing)}')
        if conn.execute('SELECT 1 FROM discovery_runs WHERE run_id=?',
                        (run_id,)).fetchone() is None:
            raise ValueError(f'{results_path}: unknown run ID {run_id}')

        requested = {}
        lineage = {}
        for row in sample:
            candidates = sorted(row['_candidates'], key=lambda item: (
                item.get('domain') != row.get('leader_domain'),
                -int(item.get('score') or 0), item.get('domain') or ''))[:max_candidates]
            row['_review_candidates'] = candidates
            for candidate in candidates:
                for evidence_id in candidate.get('evidence_ids') or []:
                    requested[str(evidence_id)] = None
                    lineage[str(evidence_id)] = (row['company_id'], row['attempt_id'])

        for batch in _chunks(requested):
            placeholders = ','.join('?' for _ in batch)
            query = f'''SELECT evidence_id,run_id,attempt_id,company_id,source_kind,
              provider,query_type,query_text,result_rank,result_url,result_host,
              title,snippet_body,requested_url,final_url
              FROM discovery_evidence WHERE run_id=? AND evidence_id IN ({placeholders})'''
            for stored in conn.execute(query, (run_id, *batch)):
                evidence = dict(stored)
                expected = lineage[evidence['evidence_id']]
                if (evidence['company_id'], evidence['attempt_id']) != expected:
                    raise ValueError(f'Cross-lineage evidence {evidence["evidence_id"]}')
                requested[evidence['evidence_id']] = evidence
        missing = sorted(key for key, value in requested.items() if value is None)
        if missing:
            raise ValueError(f'Missing review evidence IDs: {missing[:5]}')
        return results_path, requested
    finally:
        conn.close()


def truncate(value, limit):
    value = ' '.join(str(value or '').split())
    if len(value) <= limit:
        return value
    return value[:max(0, limit-1)].rstrip() + '…'


def _yes(value):
    return 'Y' if value else '-'


def _candidate_line(candidate):
    urls = candidate.get('urls') or []
    negatives = [name for name, active in (
        ('BLOCKED', candidate.get('blocked_as_official')),
        ('ID_CONFLICT', candidate.get('identifier_conflict')),
        ('ADDRESS_CONFLICT', candidate.get('address_conflict')),
        ('ENTITY_CONFLICT', candidate.get('entity_conflict')),
        ('FOREIGN_TLD', candidate.get('foreign_country_domain')),
        ('NAME_OR_RANK_ONLY', candidate.get('name_or_rank_only')),
    ) if active]
    location = '/'.join(name for name, active in (
        ('address', candidate.get('exact_address')),
        ('street', candidate.get('exact_street')),
        ('postal', candidate.get('exact_postal')),
        ('city', candidate.get('exact_municipality')),
    ) if active) or '-'
    return (f"{candidate.get('domain','')} | url={truncate(urls[0] if urls else '', 120)} "
        f"| type={candidate.get('candidate_type','')} | score={candidate.get('score','')} "
        f"| tax={_yes(candidate.get('exact_tax'))} reg={_yes(candidate.get('exact_registration'))} "
        f"legal={_yes(candidate.get('exact_legal_name'))} location={location} "
        f"phone={_yes(candidate.get('attributed_phone_agreement'))} "
        f"email_attr={_yes(candidate.get('attributed_email_domain_agreement'))} "
        f"email_raw={_yes(candidate.get('raw_email_domain_agreement'))} "
        f"evidence={candidate.get('independent_evidence_count',0)} "
        f"queries={candidate.get('query_occurrence_count',0)} "
        f"rank={candidate.get('minimum_search_rank') or '-'} "
        f"| negative={','.join(negatives) if negatives else '-'}")


def _evidence_line(domain, evidence):
    if evidence['source_kind'] == 'SEARCH_RESULT':
        publisher = evidence.get('result_host') or evidence.get('result_url') or ''
        return (f"[{domain}] SEARCH rank={evidence.get('result_rank') or '-'} "
            f"publisher={truncate(publisher, 80)} provider={truncate(evidence.get('provider'), 30)} "
            f"query={truncate(evidence.get('query_text'), 100)} | "
            f"title={truncate(evidence.get('title'), 120)} | "
            f"snippet={truncate(evidence.get('snippet_body'), 220)}")
    return (f"[{domain}] PAGE url={truncate(evidence.get('final_url') or evidence.get('requested_url'), 120)} "
            f"| title={truncate(evidence.get('title'), 140)}")


def _failure_hint(category):
    return {
        'NO_MEANINGFUL_IDENTITY_SUPPORT': 'Candidates have only weak name/search/rank support.',
        'THIRD_PARTY_NOISE_DOMINATES': 'Known third-party candidates dominate stored evidence.',
        'ONE_MODERATE_LEADER': 'A moderate leader exists but current authorization gates were not met.',
        'ONE_CLEAR_EVIDENCE_LEADER': 'A strong diagnostic leader exists but was not an accepted ownership result.',
        'MANY_LOW_SIGNAL_CANDIDATES': 'Several different domains remain without strong support.',
        'CROSS_COUNTRY_COLLISION': 'Supported same-name candidates span country-code domains.',
        'TWO_CLOSE_CANDIDATES': 'Two candidates have no decisive evidence margin.',
        'SAME_DOMAIN_VARIANTS': 'Host variants were counted separately before registrable-domain grouping.',
    }[category]


def build_review(master, results, run_id, ambiguity_companies,
                 existing_sample=None, seed='ambiguity-review-v1', max_candidates=6):
    if not 1 <= max_candidates <= 8:
        raise ValueError('max_candidates must be 1..8')
    companies_path, sample_path, sample, source = select_sample(
        ambiguity_companies, existing_sample, seed)
    master_path = _validate_master(master, sample)
    results_path, evidence = _load_evidence(results, run_id, sample, max_candidates)
    output = []
    for position, row in enumerate(sample, 1):
        candidates = row['_review_candidates']
        previews = []
        for candidate in candidates:
            candidate_evidence = [evidence[evidence_id]
                for evidence_id in candidate.get('evidence_ids') or []]
            candidate_evidence.sort(key=lambda item: (
                item['source_kind'] != 'SEARCH_RESULT',
                item.get('result_rank') or 10_000, item['evidence_id']))
            previews.extend(_evidence_line(candidate.get('domain',''), item)
                            for item in candidate_evidence[:2])
        website = row.get('accepted_website') or ''
        category = row['diagnostic_category']
        output.append({
            'SAMPLE_POSITION': position, 'SAMPLE_SEED': seed,
            'SAMPLE_SOURCE': source,
            'canonical_company_id': row['company_id'],
            'company_name': row.get('company_name') or '',
            'tax_number': row.get('tax_number') or '',
            'registration_number': row.get('registration_number') or '',
            'address': row.get('address') or '',
            'ambiguity_category': category,
            'candidate_count': row.get('plausible_host_count') or '',
            'registrable_domain_count': row.get('plausible_registrable_domain_count') or '',
            'current_selected_website': website,
            'current_selected_domain': normalize_domain(website),
            'diagnostic_leader': row.get('leader_domain') or '',
            'leader_score': row.get('leader_score') or '',
            'leader_tier': category if category in (
                'ONE_CLEAR_EVIDENCE_LEADER','ONE_MODERATE_LEADER') else 'NO_LEADER',
            'margin_to_next_candidate': row.get('leader_margin') or '',
            'DETERMINISTIC_FAILURE_HINT': _failure_hint(category),
            'CANDIDATES_COMPACT': '\n'.join(_candidate_line(item) for item in candidates),
            'EVIDENCE_PREVIEW': '\n'.join(previews),
            'ANDREJ_OFFICIAL_WEBSITE': '', 'ANDREJ_RESULT': '',
            'ANDREJ_BEST_CANDIDATE': '', 'ANDREJ_NOTES': '',
        })
    summary = {
        'sample_size': len(output), 'seed': seed, 'sample_source': source,
        'category_counts': dict(sorted(_sample_counts(sample).items())),
        'max_candidates_per_company': max_candidates,
        'inputs': {'master': str(master_path), 'results': str(results_path),
            'run_id': run_id, 'ambiguity_companies': str(companies_path),
            'existing_sample': str(sample_path) if sample_path else None},
    }
    return output, summary


def guide(summary):
    decisions = '\n'.join(f'- `{value}`' for value in REVIEW_RESULTS)
    counts = '\n'.join(f'- {key}: {value}' for key, value in
                       summary['category_counts'].items())
    return f'''# Ambiguity human-review guide

This is a read-only diagnostic sample. Candidate leaders and scores are not
accepted websites.

## Sample

- Source: {summary['sample_source']}
- Seed: `{summary['seed']}`
- Companies: {summary['sample_size']}
- Maximum displayed candidates per company: {summary['max_candidates_per_company']}

{counts}

## Andrej's fields

- `ANDREJ_OFFICIAL_WEBSITE`: verified official URL, if one exists.
- `ANDREJ_RESULT`: choose exactly one of:
{decisions}
- `ANDREJ_BEST_CANDIDATE`: best domain already present in `CANDIDATES_COMPACT`,
  if applicable.
- `ANDREJ_NOTES`: concise reason, conflict, missing search result, or entity nuance.

For each row decide whether the correct site was already present, whether the
diagnostic leader is trustworthy, whether third-party noise caused the ambiguity,
and whether the next intervention is deterministic resolution or genuinely new
search. `EVIDENCE_PREVIEW` is truncated stored evidence only; no live lookup was
performed. Empty or insufficient evidence should be marked
`INSUFFICIENT_TO_TELL`, not guessed.
'''


def _atomic_text(path, value, overwrite):
    if path.exists() and not overwrite:
        raise ValueError(f'Output already exists (use --overwrite): {path}')
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp',
                                     dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(value)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_review(output_dir, rows, summary, overwrite=False):
    output = Path(output_dir).expanduser().absolute()
    source_paths = {Path(value).resolve() for key, value in summary['inputs'].items()
                    if key != 'run_id' and value}
    if output.resolve() in source_paths:
        raise ValueError('Output directory cannot be an input file')
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output/OUTPUT_NAMES[0]
    guide_path = output/OUTPUT_NAMES[1]
    if not overwrite and (csv_path.exists() or guide_path.exists()):
        raise ValueError('Review output already exists; choose a new directory or use --overwrite')
    if csv_path.resolve() in source_paths or guide_path.resolve() in source_paths:
        raise ValueError('Review output cannot overwrite an input')
    _atomic_csv(csv_path, rows, OUTPUT_HEADERS, overwrite)
    _atomic_text(guide_path, guide(summary), overwrite)
    return {OUTPUT_NAMES[0]: str(csv_path), OUTPUT_NAMES[1]: str(guide_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--master', required=True)
    parser.add_argument('--results', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--ambiguity-companies', required=True)
    parser.add_argument('--existing-sample')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--seed', default='ambiguity-review-v1')
    parser.add_argument('--max-candidates', type=int, default=6)
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    try:
        rows, summary = build_review(args.master, args.results, args.run_id,
            args.ambiguity_companies, args.existing_sample, args.seed,
            args.max_candidates)
        outputs = write_review(args.output_dir, rows, summary, args.overwrite)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f'error: {exc}\n')
    print('REVIEW_SAMPLE=' + json.dumps(summary, sort_keys=True, separators=(',', ':')))
    for name, path in outputs.items():
        print(f'{name.upper().replace(".", "_")}={path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
