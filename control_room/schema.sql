PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS control_jobs (
    job_number INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    input_kind TEXT NOT NULL CHECK(input_kind IN ('FAKE','UPLOAD','DASTABASE_SELECTION')),
    module TEXT NOT NULL CHECK(module='DISCOVERY_CONTACTS'),
    status TEXT NOT NULL CHECK(status IN (
        'DRAFT','REVIEW_REQUIRED','QUEUED','STARTING','RUNNING',
        'EXPORTING','COMPLETED','PARTIAL','FAILED')),
    created_at TEXT NOT NULL,
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    worker_id TEXT,
    worker_heartbeat_at TEXT,
    selected_company_count INTEGER NOT NULL CHECK(selected_company_count BETWEEN 1 AND 5000),
    processed_company_count INTEGER NOT NULL DEFAULT 0 CHECK(processed_company_count >= 0),
    emails_found INTEGER NOT NULL DEFAULT 0 CHECK(emails_found >= 0),
    websites_found INTEGER NOT NULL DEFAULT 0 CHECK(websites_found >= 0),
    phones_found INTEGER NOT NULL DEFAULT 0 CHECK(phones_found >= 0),
    error_code TEXT,
    error_message TEXT,
    execution_adapter TEXT NOT NULL DEFAULT 'FAKE' CHECK(execution_adapter IN ('FAKE','DISCOVERY_V2')),
    discovery_run_id TEXT,
    completed_company_count INTEGER NOT NULL DEFAULT 0,
    partial_company_count INTEGER NOT NULL DEFAULT 0,
    failed_company_count INTEGER NOT NULL DEFAULT 0,
    ineligible_company_count INTEGER NOT NULL DEFAULT 0,
    CHECK(processed_company_count <= selected_company_count)
);

CREATE TABLE IF NOT EXISTS job_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES control_jobs(job_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('INFO','WARNING','ERROR')),
    event_code TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(job_id, sequence)
);

CREATE TABLE IF NOT EXISTS control_workers (
    worker_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ONLINE','STOPPED'))
);

CREATE TABLE IF NOT EXISTS uploads (
    upload_id TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    format TEXT NOT NULL CHECK(format IN ('CSV','XLSX')),
    worksheet_name TEXT,
    created_at TEXT NOT NULL,
    headers_json TEXT NOT NULL,
    mapping_json TEXT NOT NULL DEFAULT '{}',
    row_count INTEGER NOT NULL CHECK(row_count >= 0),
    status TEXT NOT NULL CHECK(status IN ('MAPPING','REVIEW','CONFIRMED'))
);

CREATE TABLE IF NOT EXISTS upload_rows (
    upload_id TEXT NOT NULL REFERENCES uploads(upload_id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL CHECK(row_number > 0),
    original_values_json TEXT NOT NULL,
    normalized_name TEXT,
    normalized_tax_number TEXT,
    normalized_registration_number TEXT,
    normalized_address TEXT,
    normalized_municipality TEXT,
    match_status TEXT NOT NULL CHECK(match_status IN ('PENDING','MATCHED','AMBIGUOUS','NOT_FOUND','EXCLUDED')),
    selected_company_id INTEGER,
    selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1)),
    match_method TEXT,
    PRIMARY KEY(upload_id,row_number)
);

CREATE TABLE IF NOT EXISTS match_candidates (
    upload_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    rank INTEGER NOT NULL CHECK(rank > 0),
    match_method TEXT NOT NULL,
    similarity REAL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1)),
    PRIMARY KEY(upload_id,row_number,company_id),
    FOREIGN KEY(upload_id,row_number) REFERENCES upload_rows(upload_id,row_number) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS job_items (
    job_id TEXT NOT NULL REFERENCES control_jobs(job_id) ON DELETE CASCADE,
    item_position INTEGER NOT NULL CHECK(item_position > 0),
    upload_id TEXT NOT NULL,
    upload_row_number INTEGER NOT NULL,
    company_id INTEGER,
    match_status TEXT NOT NULL,
    match_method TEXT,
    selected INTEGER NOT NULL CHECK(selected IN (0,1)),
    PRIMARY KEY(job_id,item_position),
    FOREIGN KEY(upload_id,upload_row_number) REFERENCES upload_rows(upload_id,row_number)
);

CREATE TABLE IF NOT EXISTS job_artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES control_jobs(job_id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL CHECK(artifact_type IN
        ('DISCOVERY_RESULTS_DB','DISCOVERY_EXPORT','UPLOAD_RECONCILIATION','MANIFEST','LOG')),
    relative_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    sha256 TEXT NOT NULL,
    UNIQUE(job_id,artifact_type)
);

CREATE INDEX IF NOT EXISTS control_jobs_queue
ON control_jobs(status, job_number);

CREATE INDEX IF NOT EXISTS job_events_job
ON job_events(job_id, sequence DESC);

CREATE INDEX IF NOT EXISTS upload_rows_status ON upload_rows(upload_id,match_status,row_number);
CREATE INDEX IF NOT EXISTS match_candidates_row ON match_candidates(upload_id,row_number,rank);
CREATE INDEX IF NOT EXISTS job_items_job ON job_items(job_id,item_position);
CREATE INDEX IF NOT EXISTS job_artifacts_job ON job_artifacts(job_id,artifact_type);

PRAGMA user_version=3;
