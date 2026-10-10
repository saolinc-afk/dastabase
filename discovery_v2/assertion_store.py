"""Append-only, provenance-preserving knowledge assertion storage."""
import json
import sqlite3
from pathlib import Path


APPLICATION_ID = 0x44544754  # DTGT -- Dastabase ground truth
SCHEMA_VERSION = 1
SOURCE_TYPES = {
    'HUMAN_REVIEW', 'DETERMINISTIC_RESOLVER', 'AI_ENRICHMENT',
    'STRUCTURED_EXTERNAL_SOURCE',
}
RELATION_TYPES = {'SUPERSEDES', 'CONFLICTS_WITH'}

SOURCE_COLUMNS = (
    'source_id', 'source_type', 'source_identifier', 'actor_identifier',
    'methodology', 'methodology_version', 'created_at', 'metadata_json',
)
ASSERTION_COLUMNS = (
    'assertion_id', 'canonical_company_id', 'claim_type', 'asserted_value',
    'normalized_values_json', 'assertion_status', 'source_id', 'source_row_key',
    'authority_level', 'authority_rank', 'confidence',
    'evidence_references_json', 'created_at', 'reviewed_at', 'valid_from',
    'expires_at', 'freshness_policy', 'supersedes_json', 'conflicts_with_json',
    'review_comment', 'metadata_json',
)

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE assertion_sources(
  source_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL CHECK(source_type IN
    ('HUMAN_REVIEW','DETERMINISTIC_RESOLVER','AI_ENRICHMENT',
     'STRUCTURED_EXTERNAL_SOURCE')),
  source_identifier TEXT NOT NULL,
  actor_identifier TEXT NOT NULL,
  methodology TEXT NOT NULL,
  methodology_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
  UNIQUE(source_type,source_identifier,methodology,methodology_version)
);
CREATE TABLE knowledge_assertions(
  assertion_id TEXT PRIMARY KEY,
  canonical_company_id INTEGER NOT NULL,
  claim_type TEXT NOT NULL,
  asserted_value TEXT NOT NULL,
  normalized_values_json TEXT NOT NULL CHECK(json_valid(normalized_values_json)),
  assertion_status TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES assertion_sources(source_id),
  source_row_key TEXT NOT NULL,
  authority_level TEXT NOT NULL,
  authority_rank INTEGER NOT NULL,
  confidence TEXT,
  evidence_references_json TEXT NOT NULL CHECK(json_valid(evidence_references_json)),
  created_at TEXT NOT NULL,
  reviewed_at TEXT,
  valid_from TEXT,
  expires_at TEXT,
  freshness_policy TEXT NOT NULL,
  supersedes_json TEXT NOT NULL CHECK(json_valid(supersedes_json)),
  conflicts_with_json TEXT NOT NULL CHECK(json_valid(conflicts_with_json)),
  review_comment TEXT NOT NULL,
  metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
  UNIQUE(source_id,source_row_key,claim_type)
);
CREATE INDEX assertions_company_claim
  ON knowledge_assertions(canonical_company_id,claim_type,authority_rank DESC);
CREATE TABLE assertion_relations(
  relation_id TEXT PRIMARY KEY,
  assertion_id TEXT NOT NULL REFERENCES knowledge_assertions(assertion_id),
  related_assertion_id TEXT NOT NULL REFERENCES knowledge_assertions(assertion_id),
  relation_type TEXT NOT NULL CHECK(relation_type IN ('SUPERSEDES','CONFLICTS_WITH')),
  created_at TEXT NOT NULL,
  UNIQUE(assertion_id,related_assertion_id,relation_type),
  CHECK(assertion_id<>related_assertion_id)
);
'''


def _canonical_json(value):
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def _normalize_record(record, columns, json_columns=()):
    normalized = {column: record.get(column) for column in columns}
    for column in json_columns:
        normalized[column] = _canonical_json(normalized[column] or [])
    return normalized


def _stored_record(connection, table, key, value, columns):
    selected = ','.join(columns)
    row = connection.execute(
        f'SELECT {selected} FROM {table} WHERE {key}=?', (value,)).fetchone()
    return dict(row) if row else None


class AssertionStore:
    """Append-only store; duplicate imports are idempotent, not updates."""

    def __init__(self, path, *, create=False):
        self.path = Path(path).expanduser().resolve()
        if not create and not self.path.exists():
            raise FileNotFoundError(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        if create and self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() is None:
            self.connection.executescript(SCHEMA)
            self.connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            self.connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        self._validate()

    def _validate(self):
        if self.connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
            raise ValueError(f'{self.path}: not a Dastabase assertion store')
        if self.connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
            raise ValueError(f'{self.path}: unsupported assertion schema version')

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def ingest(self, source, assertions, relations=()):
        source = _normalize_record(source, SOURCE_COLUMNS, ('metadata_json',))
        if source['source_type'] not in SOURCE_TYPES:
            raise ValueError(f'Unknown source type {source["source_type"]}')
        normalized_assertions = [_normalize_record(row, ASSERTION_COLUMNS, (
            'normalized_values_json', 'evidence_references_json',
            'supersedes_json', 'conflicts_with_json', 'metadata_json'))
            for row in assertions]
        seen = set()
        for row in normalized_assertions:
            if row['assertion_id'] in seen:
                raise ValueError(f'Duplicate assertion ID in import: {row["assertion_id"]}')
            seen.add(row['assertion_id'])
            if row['source_id'] != source['source_id']:
                raise ValueError('Assertion source_id does not match imported source')
            if not isinstance(row['canonical_company_id'], int) or row['canonical_company_id'] < 1:
                raise ValueError('canonical_company_id must be a positive integer')

        inserted = skipped = 0
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            existing_source = _stored_record(self.connection, 'assertion_sources',
                'source_id', source['source_id'], SOURCE_COLUMNS)
            if existing_source is None:
                placeholders = ','.join('?' for _ in SOURCE_COLUMNS)
                self.connection.execute(
                    f'INSERT INTO assertion_sources({",".join(SOURCE_COLUMNS)}) '
                    f'VALUES({placeholders})', tuple(source[key] for key in SOURCE_COLUMNS))
            elif existing_source != source:
                raise ValueError('Source ID collision with different provenance')

            placeholders = ','.join('?' for _ in ASSERTION_COLUMNS)
            for row in normalized_assertions:
                existing = _stored_record(self.connection, 'knowledge_assertions',
                    'assertion_id', row['assertion_id'], ASSERTION_COLUMNS)
                if existing is None:
                    self.connection.execute(
                        f'INSERT INTO knowledge_assertions({",".join(ASSERTION_COLUMNS)}) '
                        f'VALUES({placeholders})',
                        tuple(row[key] for key in ASSERTION_COLUMNS))
                    inserted += 1
                elif existing == row:
                    skipped += 1
                else:
                    raise ValueError(
                        f'Assertion ID collision with different content: {row["assertion_id"]}')

            for relation in relations:
                relation_type = relation['relation_type']
                if relation_type not in RELATION_TYPES:
                    raise ValueError(f'Unknown assertion relation {relation_type}')
                values = (relation['relation_id'], relation['assertion_id'],
                          relation['related_assertion_id'], relation_type,
                          relation['created_at'])
                existing = self.connection.execute('''SELECT relation_id,assertion_id,
                    related_assertion_id,relation_type,created_at
                    FROM assertion_relations WHERE relation_id=?''',
                    (relation['relation_id'],)).fetchone()
                if existing is None:
                    self.connection.execute(
                        'INSERT INTO assertion_relations VALUES(?,?,?,?,?)', values)
                elif tuple(existing) != values:
                    raise ValueError('Assertion relation ID collision')
            self.connection.execute('COMMIT')
        except BaseException:
            self.connection.execute('ROLLBACK')
            raise
        return {'inserted': inserted, 'skipped': skipped}

    def assertions(self, company_id=None, claim_type=None):
        clauses = []
        values = []
        if company_id is not None:
            clauses.append('canonical_company_id=?'); values.append(company_id)
        if claim_type is not None:
            clauses.append('claim_type=?'); values.append(claim_type)
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        return [dict(row) for row in self.connection.execute(
            'SELECT * FROM knowledge_assertions' + where +
            ' ORDER BY canonical_company_id,claim_type,created_at,assertion_id', values)]
