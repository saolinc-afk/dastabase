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
    selected_company_count INTEGER NOT NULL CHECK(selected_company_count BETWEEN 1 AND 1000),
    processed_company_count INTEGER NOT NULL DEFAULT 0 CHECK(processed_company_count >= 0),
    emails_found INTEGER NOT NULL DEFAULT 0 CHECK(emails_found >= 0),
    websites_found INTEGER NOT NULL DEFAULT 0 CHECK(websites_found >= 0),
    phones_found INTEGER NOT NULL DEFAULT 0 CHECK(phones_found >= 0),
    error_code TEXT,
    error_message TEXT,
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

CREATE INDEX IF NOT EXISTS control_jobs_queue
ON control_jobs(status, job_number);

CREATE INDEX IF NOT EXISTS job_events_job
ON job_events(job_id, sequence DESC);

PRAGMA user_version=1;
