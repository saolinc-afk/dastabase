"""Import a reviewed ambiguity workbook into durable diagnostic assertions."""
import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

from discovery.domain_generator import normalize_domain
from discovery_v2.assertion_store import (
    ASSERTION_COLUMNS, SOURCE_COLUMNS, AssertionStore,
)


NS = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'rel': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'package': 'http://schemas.openxmlformats.org/package/2006/relationships',
      'core': 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties',
      'dc': 'http://purl.org/dc/elements/1.1/',
      'dcterms': 'http://purl.org/dc/terms/'}
METHODOLOGY = 'DISCOVERY_AMBIGUITY_HUMAN_REVIEW'
METHODOLOGY_VERSION = '1'
AUTHORITY_LEVEL = 'HUMAN_VALIDATED'
AUTHORITY_RANK = 400
OUTPUT_NAMES = (
    'human_review_assertions.csv', 'human_review_assertions.sqlite3',
    'discovery_benchmark_100.csv', 'DISCOVERY_BENCHMARK_REPORT.md',
    'HUMAN_REVIEW_GROUND_TRUTH.md',
)
REVIEW_REQUIRED = {
    'SAMPLE_POSITION', 'company_name', 'tax_number', 'address',
    'ambiguity_category', 'ANDREJ_OFFICIAL_WEBSITE', 'ANDREJ_NOTES',
}
RAW_REQUIRED = {
    'SAMPLE_POSITION', 'SAMPLE_SEED', 'SAMPLE_SOURCE',
    'canonical_company_id', 'company_name', 'tax_number',
    'registration_number', 'address', 'ambiguity_category',
    'candidate_count', 'registrable_domain_count', 'current_selected_website',
    'current_selected_domain', 'diagnostic_leader', 'leader_score',
    'leader_tier', 'margin_to_next_candidate', 'CANDIDATES_COMPACT',
    'EVIDENCE_PREVIEW',
}
ASSERTION_EXPORT_HEADERS = (
    'source_id', 'source_type', 'source_identifier', 'actor_identifier',
    'methodology', 'methodology_version', 'source_created_at',
    'source_metadata_json',
) + tuple(column for column in ASSERTION_COLUMNS if column != 'source_id')
BENCHMARK_HEADERS = (
    'sample_position', 'canonical_company_id', 'company_name', 'tax_number',
    'registration_number', 'address', 'ambiguity_category', 'candidate_count',
    'registrable_domain_count', 'current_selected_website',
    'diagnostic_leader', 'leader_score', 'leader_tier',
    'margin_to_next_candidate', 'ANDREJ_OFFICIAL_WEBSITE', 'ANDREJ_NOTES',
    'human_assertion_status', 'human_confidence', 'human_domains',
    'stored_candidate_domains', 'candidate_preview_complete',
    'human_domain_is_diagnostic_leader',
    'human_domain_in_stored_candidates', 'matched_stored_domains',
    'benchmark_outcome', 'review_difficulty', 'failure_mechanism',
    'selective_ai_disposition', 'assertion_id', 'source_id',
)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _column_index(reference):
    match = re.match(r'[A-Z]+', reference or '')
    if not match:
        raise ValueError(f'Invalid XLSX cell reference {reference!r}')
    value = 0
    for letter in match.group(0):
        value = value * 26 + ord(letter) - 64
    return value - 1


def _xml_text(node):
    return ''.join(node.itertext()) if node is not None else ''


def _sheet_rows(archive, path, shared):
    root = ET.fromstring(archive.read(path))
    rows = []
    for row in root.findall('.//main:sheetData/main:row', NS):
        values = {}
        for cell in row.findall('main:c', NS):
            index = _column_index(cell.attrib.get('r'))
            kind = cell.attrib.get('t')
            value = cell.find('main:v', NS)
            inline = cell.find('main:is', NS)
            if kind == 's' and value is not None:
                text = shared[int(value.text)]
            elif kind == 'inlineStr':
                text = _xml_text(inline)
            else:
                text = value.text if value is not None and value.text is not None else ''
            values[index] = text
        if values:
            rendered = [''] * (max(values) + 1)
            for index, value in values.items():
                rendered[index] = value
            rows.append(rendered)
    return rows


def read_xlsx(path):
    """Read cell values without executing formulas or using external libraries."""
    resolved = Path(path).expanduser().resolve(strict=True)
    try:
        archive = zipfile.ZipFile(resolved)
    except zipfile.BadZipFile:
        raise ValueError(f'{resolved}: invalid XLSX file') from None
    with archive:
        try:
            workbook = ET.fromstring(archive.read('xl/workbook.xml'))
            relations = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        except (KeyError, ET.ParseError):
            raise ValueError(f'{resolved}: malformed XLSX workbook') from None
        shared = []
        try:
            shared_root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
            shared = [_xml_text(item) for item in shared_root.findall('main:si', NS)]
        except KeyError:
            pass
        targets = {item.attrib['Id']:item.attrib['Target'] for item in relations}
        sheets = {}
        for item in workbook.findall('.//main:sheets/main:sheet', NS):
            relationship = item.attrib['{'+NS['rel']+'}id']
            target = targets[relationship]
            if target.startswith('/xl/'):
                target = target[1:]
            elif not target.startswith('xl/'):
                target = 'xl/' + target
            sheets[item.attrib['name']] = _sheet_rows(archive, target, shared)
        metadata = {'last_modified_by': '', 'modified_at': ''}
        try:
            core = ET.fromstring(archive.read('docProps/core.xml'))
            author = core.find('core:lastModifiedBy', NS)
            modified = core.find('dcterms:modified', NS)
            metadata = {'last_modified_by': _xml_text(author),
                        'modified_at': _xml_text(modified)}
        except (KeyError, ET.ParseError):
            pass
    return resolved, sheets, metadata


def _table(rows, label, required):
    if not rows:
        raise ValueError(f'{label} sheet is empty')
    headers = rows[0]
    if len(headers) != len(set(headers)):
        raise ValueError(f'{label} has duplicate headers')
    missing = required - set(headers)
    if missing:
        raise ValueError(f'{label} missing columns {sorted(missing)}')
    output = []
    for row in rows[1:]:
        padded = row + [''] * (len(headers) - len(row))
        output.append(dict(zip(headers, padded[:len(headers)])))
    return output


def _position(value, label):
    try:
        numeric = int(float(value))
    except (TypeError, ValueError):
        raise ValueError(f'{label}: invalid SAMPLE_POSITION {value!r}') from None
    if str(numeric) != str(value).removesuffix('.0'):
        raise ValueError(f'{label}: non-integral SAMPLE_POSITION {value!r}')
    return numeric


def _indexed(rows, label):
    output = {}
    for row_number, row in enumerate(rows, 2):
        position = _position(row.get('SAMPLE_POSITION'), f'{label}:{row_number}')
        if position in output:
            raise ValueError(f'{label}:{row_number}: duplicate SAMPLE_POSITION {position}')
        output[position] = row
    return output


def _fold(value):
    normalized = unicodedata.normalize('NFKD', str(value or '').lower())
    return ''.join(character for character in normalized
                   if unicodedata.category(character) != 'Mn')


def _human_domains(value):
    pattern = re.compile(
        r'(?i)(?:https?://)?(?:www\.)?(?:[a-z0-9\u0080-\uffff-]+\.)+'
        r'[a-z]{2,}(?:/[^\s,;]*)?')
    domains = []
    for match in pattern.findall(value or ''):
        domain = normalize_domain(match)
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def _candidate_domains(value):
    domains = []
    for line in str(value or '').splitlines():
        domain = normalize_domain(line.split(' |', 1)[0])
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def _review_state(website, comment):
    folded = _fold(comment)
    wrong = any(phrase in folded for phrase in (
        'to ni ista firma', 'ne od slovenske firme',
        'slovenske strani ni - samo srbska', 'bosanska stran',
    ))
    complex_relationship = any(phrase in folded for phrase in (
        'stran "mame"', 'rebranding', 'del korporacije', 'principal',
        'dve spletni strani', 'kompliciran', 'korporacija', 'korpo stran',
        'njihovih lastnikov', 'vec domen', 'spletna trznica', 'mednarodna',
    ))
    if wrong:
        return 'WRONG_ENTITY_OR_CROSS_COUNTRY', 'MEDIUM'
    if complex_relationship:
        return 'COMPLEX_RELATIONSHIP', 'MEDIUM'
    if website:
        return 'CONFIRMED_OFFICIAL_WEBSITE', 'HIGH'
    if 'nisem nasel www' in folded:
        return 'LIKELY_NO_OFFICIAL_WEBSITE', 'MEDIUM'
    return 'UNRESOLVED_INSUFFICIENT_EVIDENCE', ''


def _difficulty(comment):
    folded = _fold(comment)
    if 'lahko najti' in folded:
        return 'EASY_TO_FIND'
    if 'tezko najti' in folded or 'tezje najti' in folded or 'nemogoce najti' in folded:
        return 'DIFFICULT_TO_FIND'
    return 'NOT_STATED'


def _outcome(status, leader_match, candidate_match, candidate_preview_complete):
    if status == 'WRONG_ENTITY_OR_CROSS_COUNTRY':
        return 'WRONG_ENTITY_OR_CROSS_COUNTRY'
    if status == 'COMPLEX_RELATIONSHIP':
        return 'COMPLEX_BRAND_OR_GROUP_RELATIONSHIP'
    if status == 'LIKELY_NO_OFFICIAL_WEBSITE':
        return 'LIKELY_NO_OFFICIAL_WEBSITE'
    if status == 'UNRESOLVED_INSUFFICIENT_EVIDENCE':
        return 'HUMAN_REVIEW_UNRESOLVED'
    if leader_match:
        return 'CORRECT_DOMAIN_ALREADY_IN_STORED_EVIDENCE'
    if candidate_match:
        return 'CORRECT_DOMAIN_PRESENT_BUT_NOT_SELECTED'
    if not candidate_preview_complete:
        return 'HUMAN_REVIEW_UNRESOLVED'
    return 'OFFICIAL_EXISTS_BUT_SEARCH_MISSED'


def _mechanism(outcome):
    return {
        'CORRECT_DOMAIN_ALREADY_IN_STORED_EVIDENCE': 'AUTHORIZATION_THRESHOLD_TOO_STRICT',
        'CORRECT_DOMAIN_PRESENT_BUT_NOT_SELECTED': 'CANDIDATE_RANKING_OR_ATTRIBUTION',
        'OFFICIAL_EXISTS_BUT_SEARCH_MISSED': 'SEARCH_RECALL_OR_EXTRACTION_GAP',
        'LIKELY_NO_OFFICIAL_WEBSITE': 'NO_FALSE_NEGATIVE_SUPPORTED',
        'COMPLEX_BRAND_OR_GROUP_RELATIONSHIP': 'ENTITY_RELATIONSHIP_NOT_MODELED',
        'WRONG_ENTITY_OR_CROSS_COUNTRY': 'ENTITY_OR_COUNTRY_COLLISION',
        'HUMAN_REVIEW_UNRESOLVED': 'INSUFFICIENT_OR_TRUNCATED_EVIDENCE',
    }[outcome]


def _ai_disposition(outcome, difficulty):
    if outcome == 'COMPLEX_BRAND_OR_GROUP_RELATIONSHIP':
        return 'ELIGIBLE_RELATIONSHIP_DISAMBIGUATION'
    if outcome == 'WRONG_ENTITY_OR_CROSS_COUNTRY':
        return 'ELIGIBLE_ENTITY_DISAMBIGUATION'
    if outcome == 'OFFICIAL_EXISTS_BUT_SEARCH_MISSED':
        return ('ELIGIBLE_SEARCH_INTERPRETATION' if difficulty == 'DIFFICULT_TO_FIND'
                else 'NEW_SEARCH_BEFORE_AI')
    if outcome == 'HUMAN_REVIEW_UNRESOLVED':
        return 'ELIGIBLE_ONLY_IF_BUSINESS_VALUE_JUSTIFIES'
    if outcome == 'LIKELY_NO_OFFICIAL_WEBSITE':
        return 'NO_AI_BY_DEFAULT'
    return 'NO_AI_DETERMINISTIC_EVIDENCE_SUFFICIENT'


def _stable_id(prefix, *values):
    payload = '\x1f'.join(str(value) for value in values).encode('utf-8')
    return prefix + hashlib.sha256(payload).hexdigest()


def load_reviewed_workbook(path, *, expected_size=100, reviewer=None,
                           reviewed_at=None):
    resolved, sheets, metadata = read_xlsx(path)
    if 'REVIEW' not in sheets or 'RAW DATA' not in sheets:
        raise ValueError('Workbook must contain REVIEW and RAW DATA sheets')
    review = _indexed(_table(sheets['REVIEW'], 'REVIEW', REVIEW_REQUIRED), 'REVIEW')
    raw = _indexed(_table(sheets['RAW DATA'], 'RAW DATA', RAW_REQUIRED), 'RAW DATA')
    if set(review) != set(raw):
        raise ValueError('REVIEW and RAW DATA sample positions do not match')
    if expected_size is not None and len(review) != expected_size:
        raise ValueError(f'Expected {expected_size} reviewed companies, found {len(review)}')
    if sorted(review) != list(range(1, len(review)+1)):
        raise ValueError('SAMPLE_POSITION must be contiguous from 1')
    for position in sorted(review):
        left, right = review[position], raw[position]
        for field in ('tax_number', 'address', 'ambiguity_category'):
            if left[field] != right[field]:
                raise ValueError(f'SAMPLE_POSITION {position}: {field} mapping mismatch')

    source_sha = _sha256(resolved)
    actor = reviewer if reviewer is not None else metadata['last_modified_by']
    timestamp = reviewed_at if reviewed_at is not None else metadata['modified_at']
    if not actor or not timestamp:
        raise ValueError('Reviewer and reviewed timestamp are required')
    source_identifier = f'xlsx:{resolved.name}:sha256:{source_sha}'
    source_id = _stable_id('src_', 'HUMAN_REVIEW', source_identifier,
                           METHODOLOGY, METHODOLOGY_VERSION)
    source = {
        'source_id': source_id, 'source_type': 'HUMAN_REVIEW',
        'source_identifier': source_identifier, 'actor_identifier': actor,
        'methodology': METHODOLOGY,
        'methodology_version': METHODOLOGY_VERSION,
        'created_at': timestamp,
        'metadata_json': {'workbook_filename': resolved.name,
                          'workbook_sha256': source_sha,
                          'review_sheet': 'REVIEW', 'raw_sheet': 'RAW DATA',
                          'reviewed_companies': len(review)},
    }
    return resolved, source, [(position, review[position], raw[position])
                              for position in sorted(review)]


def build_benchmark(path, *, expected_size=100, reviewer=None, reviewed_at=None):
    source_path, source, imported = load_reviewed_workbook(
        path, expected_size=expected_size, reviewer=reviewer,
        reviewed_at=reviewed_at)
    assertions = []
    benchmark = []
    for position, review, raw in imported:
        website = review['ANDREJ_OFFICIAL_WEBSITE']
        comment = review['ANDREJ_NOTES']
        status, confidence = _review_state(website, comment)
        human_domains = _human_domains(website)
        candidates = _candidate_domains(raw['CANDIDATES_COMPACT'])
        try:
            plausible_count = int(float(raw['candidate_count']))
        except (TypeError, ValueError):
            raise ValueError(
                f'SAMPLE_POSITION {position}: invalid candidate_count') from None
        candidate_preview_complete = plausible_count <= len(candidates)
        leader = normalize_domain(raw['diagnostic_leader'])
        matched = [domain for domain in human_domains if domain in candidates]
        leader_match = bool(leader and leader in human_domains)
        candidate_match = bool(matched)
        outcome = _outcome(status, leader_match, candidate_match,
                           candidate_preview_complete)
        difficulty = _difficulty(comment)
        company_id = int(float(raw['canonical_company_id']))
        assertion_id = _stable_id('assert_', source['source_id'], position,
                                  'OFFICIAL_WEBSITE')
        evidence_references = [
            {'type': 'HUMAN_REVIEW_WORKBOOK_ROW', 'sheet': 'REVIEW',
             'row_number': position + 1, 'sample_position': position,
             'source_identifier': source['source_identifier']},
            {'type': 'DISCOVERY_AMBIGUITY_AUDIT_ROW', 'sheet': 'RAW DATA',
             'row_number': position + 1, 'sample_position': position,
             'canonical_company_id': company_id,
             'ambiguity_category': raw['ambiguity_category']},
        ]
        assertion = {
            'assertion_id': assertion_id,
            'canonical_company_id': company_id,
            'claim_type': 'OFFICIAL_WEBSITE',
            'asserted_value': website,
            'normalized_values_json': human_domains,
            'assertion_status': status,
            'source_id': source['source_id'],
            'source_row_key': str(position),
            'authority_level': AUTHORITY_LEVEL,
            'authority_rank': AUTHORITY_RANK,
            'confidence': confidence or None,
            'evidence_references_json': evidence_references,
            'created_at': source['created_at'],
            'reviewed_at': source['created_at'],
            'valid_from': source['created_at'],
            'expires_at': None,
            'freshness_policy': 'REVIEW_ON_CONFLICT_OR_MATERIAL_COMPANY_CHANGE',
            'supersedes_json': [],
            'conflicts_with_json': [],
            'review_comment': comment,
            'metadata_json': {
                'benchmark_outcome': outcome,
                'review_difficulty': difficulty,
                'ambiguity_category': raw['ambiguity_category'],
                'human_domain_in_stored_candidates': candidate_match,
                'human_domain_is_diagnostic_leader': leader_match,
            },
        }
        assertions.append(assertion)
        benchmark.append({
            'sample_position': position,
            'canonical_company_id': company_id,
            'company_name': raw['company_name'],
            'tax_number': raw['tax_number'],
            'registration_number': raw['registration_number'],
            'address': raw['address'],
            'ambiguity_category': raw['ambiguity_category'],
            'candidate_count': raw['candidate_count'],
            'registrable_domain_count': raw['registrable_domain_count'],
            'current_selected_website': raw['current_selected_website'],
            'diagnostic_leader': raw['diagnostic_leader'],
            'leader_score': raw['leader_score'],
            'leader_tier': raw['leader_tier'],
            'margin_to_next_candidate': raw['margin_to_next_candidate'],
            'ANDREJ_OFFICIAL_WEBSITE': website,
            'ANDREJ_NOTES': comment,
            'human_assertion_status': status,
            'human_confidence': confidence,
            'human_domains': '|'.join(human_domains),
            'stored_candidate_domains': '|'.join(candidates),
            'candidate_preview_complete': 'Y' if candidate_preview_complete else 'N',
            'human_domain_is_diagnostic_leader': 'Y' if leader_match else 'N',
            'human_domain_in_stored_candidates': 'Y' if candidate_match else 'N',
            'matched_stored_domains': '|'.join(matched),
            'benchmark_outcome': outcome,
            'review_difficulty': difficulty,
            'failure_mechanism': _mechanism(outcome),
            'selective_ai_disposition': _ai_disposition(outcome, difficulty),
            'assertion_id': assertion_id,
            'source_id': source['source_id'],
        })
    return source_path, source, assertions, benchmark


def _counter(rows, field):
    return dict(sorted(Counter(row[field] for row in rows).items()))


def _category_table(rows):
    lines = ['| Ambiguity category | n | leader supported | any candidate supported | '
             'search miss | likely no site | complex/wrong | unresolved |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    for category in sorted({row['ambiguity_category'] for row in rows}):
        group = [row for row in rows if row['ambiguity_category'] == category]
        lines.append('| {} | {} | {} | {} | {} | {} | {} | {} |'.format(
            category, len(group),
            sum(row['human_domain_is_diagnostic_leader'] == 'Y' for row in group),
            sum(row['human_domain_in_stored_candidates'] == 'Y' for row in group),
            sum(row['benchmark_outcome'] == 'OFFICIAL_EXISTS_BUT_SEARCH_MISSED'
                for row in group),
            sum(row['benchmark_outcome'] == 'LIKELY_NO_OFFICIAL_WEBSITE'
                for row in group),
            sum(row['benchmark_outcome'] in (
                'COMPLEX_BRAND_OR_GROUP_RELATIONSHIP',
                'WRONG_ENTITY_OR_CROSS_COUNTRY') for row in group),
            sum(row['benchmark_outcome'] == 'HUMAN_REVIEW_UNRESOLVED'
                for row in group)))
    return '\n'.join(lines)


def benchmark_report(source, rows):
    found = [row for row in rows if row['human_domains']]
    stored = [row for row in found
              if row['human_domain_in_stored_candidates'] == 'Y']
    misses = [row for row in found
              if row['human_domain_in_stored_candidates'] == 'N']
    incomplete = [row for row in misses
                  if row['candidate_preview_complete'] == 'N']
    easy = [row for row in rows if row['review_difficulty'] == 'EASY_TO_FIND']
    difficult = [row for row in rows if row['review_difficulty'] == 'DIFFICULT_TO_FIND']
    outcomes = _counter(rows, 'benchmark_outcome')
    mechanisms = _counter(rows, 'failure_mechanism')
    output = ['# Discovery ambiguity benchmark — human review', '',
        'This report is diagnostic. It does not change or promote Discovery results.', '',
        '## Source', '',
        f'- `{source["source_identifier"]}`',
        f'- Reviewer: `{source["actor_identifier"]}`',
        f'- Reviewed at: `{source["created_at"]}`',
        f'- Companies: {len(rows)}', '', '## Main findings', '',
        f'- Human review recorded a website domain for **{len(found)}** companies.',
        f'- **{len(stored)} / {len(found)}** human-found websites '
        f'({len(stored)/len(found):.1%}) were already among candidates shown in '
        'the stored compact export. This is a lower bound on full-evidence presence.',
        f'- **{len(misses)} / {len(found)}** human-found websites '
        f'({len(misses)/len(found):.1%}) were absent from the compact candidate preview.',
        f'- **{len(incomplete)}** of those absent-domain rows had a truncated candidate '
        'preview and therefore remain unresolved rather than being labeled search misses.',
        f'- **{outcomes.get("OFFICIAL_EXISTS_BUT_SEARCH_MISSED", 0)} / '
        f'{len(found)}** human-found websites '
        f'({outcomes.get("OFFICIAL_EXISTS_BUT_SEARCH_MISSED", 0)/len(found):.1%}) '
        'remain genuine search-recall/extraction misses after separating explicit '
        'relationship and wrong-entity cases.',
        f'- **{outcomes.get("LIKELY_NO_OFFICIAL_WEBSITE", 0)}** companies are '
        'human-reviewed as likely having no official website; this is not an absolute '
        'nonexistence claim.',
        f'- **{outcomes.get("COMPLEX_BRAND_OR_GROUP_RELATIONSHIP", 0)}** are '
        'brand/group/rebrand/parent/marketplace or multi-domain relationship cases.',
        f'- **{outcomes.get("WRONG_ENTITY_OR_CROSS_COUNTRY", 0)}** are explicit '
        'wrong-entity or cross-country cases.', '', '### Outcome counts', '']
    output.extend(f'- {key}: {value}' for key, value in outcomes.items())
    output.extend(['', 'The outcome names are deliberately narrow: '
        '`CORRECT_DOMAIN_ALREADY_IN_STORED_EVIDENCE` means the human domain was '
        'the diagnostic leader; `CORRECT_DOMAIN_PRESENT_BUT_NOT_SELECTED` means '
        'it was another stored candidate; and `OFFICIAL_EXISTS_BUT_SEARCH_MISSED` '
        'means no human-entered domain was in a complete compact candidate preview.', '',
        '## Diagnostic-category trust', '', _category_table(rows), '',
        '“Leader supported” means the human-entered domain equals the diagnostic '
        'leader. It is not an acceptance-rate estimate. Likely-no-site, complex, and '
        'unresolved rows remain separate instead of being forced into correct/incorrect.', '',
        '## Easy versus difficult', '',
        f'- Easy to find: {len(easy)}; stored candidate present for '
        f'{sum(row["human_domain_in_stored_candidates"] == "Y" for row in easy)}; '
        f'missed for {sum(row["benchmark_outcome"] == "OFFICIAL_EXISTS_BUT_SEARCH_MISSED" for row in easy)}.',
        f'- Difficult/impossible to find: {len(difficult)}; stored candidate present for '
        f'{sum(row["human_domain_in_stored_candidates"] == "Y" for row in difficult)}; '
        f'missed for {sum(row["benchmark_outcome"] == "OFFICIAL_EXISTS_BUT_SEARCH_MISSED" for row in difficult)}.',
        '- Easy misses point first to query/result extraction gaps. Difficult misses and '
        'relationship cases are the strongest selective-AI candidates.', '',
        '## Deterministic false-negative mechanisms', ''])
    output.extend(f'- {key}: {value}' for key, value in mechanisms.items())
    output.extend(['',
        '- A supported diagnostic leader that was not accepted indicates an authorization '
        'or attribution gap, not a need for broader AI search.',
        '- A correct non-leading stored candidate indicates ranking, entity attribution, '
        'or conflict-resolution work for Resolver vNext.',
        '- A human domain absent from a complete candidate preview indicates search '
        'recall or candidate extraction failure; truncated previews remain unresolved.',
        '- Parent/group/rebrand/multi-domain cases require an explicit relationship model.',
        '- Likely-no-site outcomes must remain UNKNOWN/likely absent rather than being '
        'converted into a false accepted website.', '',
        '## Discovery vNext stages', '',
        '1. Structured deterministic sources: exact registration/tax-linked sources; '
        'investigate Sloexport separately, without treating it as official-site proof.',
        '2. Search collection: preserve query/result/rank/snippet and candidate extraction '
        'with explicit publisher and entity attribution.',
        '3. Deterministic resolver: hard exclusions, relationship classification, '
        'evidence tiers, categorical margins, and frozen-evidence ownership validation.',
        '4. Selective AI fallback: only after stages 1–3 remain ambiguous, for difficult '
        'search interpretation, entity relationships, or cross-country collisions.',
        '5. UNKNOWN/likely-no-website: retain this outcome when evidence cannot support a '
        'site; never force a winner.', '',
        'AI is not allowed when a deterministic evidence tier can resolve the candidate, '
        'merely because a low score or old threshold blocked it. AI output remains a '
        'versioned assertion and cannot outrank conflicting human ground truth.', ''])
    return '\n'.join(output)


def ground_truth_guide(source):
    return f'''# Human Review / Ground Truth layer

The import represented here is append-only and separate from canonical company and
Discovery result databases. Original Discovery evidence remains immutable.

## Assertion contract

Each assertion records canonical company, claim type, exact asserted value,
normalized values, status, source and actor, methodology/version, authority,
confidence, evidence references, review/freshness timestamps, and explicit
supersedes/conflicts metadata. Re-importing the same source row is idempotent.
A changed record with the same deterministic ID is rejected rather than updated.

Today's source is `{source['source_identifier']}`. `HUMAN_REVIEW` has explicit
`HUMAN_VALIDATED` authority rank 400. That rank represents precedence policy input,
not permission to erase older assertions. Conflicting assertions remain separate
rows; `SUPERSEDES` and `CONFLICTS_WITH` are explicit relations.

Statuses used for this review:

- `CONFIRMED_OFFICIAL_WEBSITE`
- `LIKELY_NO_OFFICIAL_WEBSITE`
- `UNRESOLVED_INSUFFICIENT_EVIDENCE`
- `COMPLEX_RELATIONSHIP`
- `WRONG_ENTITY_OR_CROSS_COUNTRY`

`LIKELY_NO_OFFICIAL_WEBSITE` is intentionally weaker than a proven nonexistence
claim. Complex and wrong-entity values preserve what the reviewer entered without
silently treating that URL as an accepted company website.

## Future Knowledge Repository / Merlin use

1. Query all assertions for the company and claim before enrichment.
2. Reuse only assertions meeting the caller's authority, confidence, and freshness
   requirements, while surfacing unresolved conflicts.
3. Invoke paid AI only when knowledge is missing, stale, conflicting, or below the
   required confidence after deterministic resolution.
4. Store validated AI results as `AI_ENRICHMENT` assertions with model/provider,
   prompt/method version, evidence references, token counts, cost metadata, and
   validity/freshness timestamps.
5. Never update or delete the human row. A later result either coexists, explicitly
   conflicts with it, or explicitly supersedes it after review.
6. Knowledge Repository integration should be a later read-policy adapter; this
   benchmark does not alter its current behavior.
'''


def _write_csv(path, rows, headers):
    with path.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction='ignore',
                                lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow({key:row.get(key, '') for key in headers})


def _assertion_export_row(source, assertion):
    row = {**assertion,
        'source_id': source['source_id'],
        'source_type': source['source_type'],
        'source_identifier': source['source_identifier'],
        'actor_identifier': source['actor_identifier'],
        'methodology': source['methodology'],
        'methodology_version': source['methodology_version'],
        'source_created_at': source['created_at'],
        'source_metadata_json': source['metadata_json'],
    }
    for key in ('source_metadata_json', 'metadata_json', 'normalized_values_json',
                'evidence_references_json', 'supersedes_json',
                'conflicts_with_json'):
        value = row.get(key)
        row[key] = json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) if not isinstance(value, str) else value
    return row


def _atomic_text(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp',
                                     dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(value)
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except OSError: pass
        raise


def write_outputs(output_dir, source, assertions, benchmark, *, overwrite=False):
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = {name:output/name for name in OUTPUT_NAMES}
    existing = [path for path in paths.values() if path.exists()]
    if existing and not overwrite:
        raise ValueError('Output already exists: ' + ', '.join(str(path) for path in existing))

    assertion_rows = [_assertion_export_row(source, assertion)
                      for assertion in assertions]
    staged = {}
    try:
        for name, rows, headers in (
                ('human_review_assertions.csv', assertion_rows, ASSERTION_EXPORT_HEADERS),
                ('discovery_benchmark_100.csv', benchmark, BENCHMARK_HEADERS)):
            fd, temporary = tempfile.mkstemp(prefix='.'+name+'.', suffix='.tmp',
                                             dir=output)
            os.close(fd)
            temporary = Path(temporary)
            _write_csv(temporary, rows, headers)
            staged[name] = temporary
        for name, value in (
                ('DISCOVERY_BENCHMARK_REPORT.md', benchmark_report(source, benchmark)),
                ('HUMAN_REVIEW_GROUND_TRUTH.md', ground_truth_guide(source))):
            fd, temporary = tempfile.mkstemp(prefix='.'+name+'.', suffix='.tmp',
                                             dir=output)
            os.close(fd)
            temporary = Path(temporary)
            _atomic_text(temporary, value)
            staged[name] = temporary
        database = output/'.human_review_assertions.sqlite3.tmp'
        if database.exists():
            database.unlink()
        with AssertionStore(database, create=True) as store:
            result = store.ingest(source, assertions)
            if result != {'inserted': len(assertions), 'skipped': 0}:
                raise AssertionError('Unexpected initial assertion import result')
            if store.connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Assertion database integrity check failed')
        staged['human_review_assertions.sqlite3'] = database
        for name, temporary in staged.items():
            os.replace(temporary, paths[name])
    except BaseException:
        for temporary in staged.values():
            try: Path(temporary).unlink()
            except OSError: pass
        raise
    return {name:str(path) for name, path in paths.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reviewed-xlsx', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--reviewer')
    parser.add_argument('--reviewed-at')
    parser.add_argument('--expected-size', type=int, default=100)
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    try:
        _, source, assertions, benchmark = build_benchmark(
            args.reviewed_xlsx, expected_size=args.expected_size,
            reviewer=args.reviewer, reviewed_at=args.reviewed_at)
        outputs = write_outputs(args.output_dir, source, assertions, benchmark,
                                overwrite=args.overwrite)
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        parser.exit(2, f'error: {exc}\n')
    summary = {'source_id': source['source_id'], 'companies': len(benchmark),
               'outcomes': _counter(benchmark, 'benchmark_outcome')}
    print('BENCHMARK=' + json.dumps(summary, sort_keys=True, separators=(',', ':')))
    for name, path in outputs.items():
        print(f'{name.upper().replace(".", "_")}={path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
