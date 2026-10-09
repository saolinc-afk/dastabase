PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','DISABLED')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_jobs (
    job_number INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    input_kind TEXT NOT NULL CHECK(input_kind IN ('FAKE','UPLOAD','DASTABASE_SELECTION')),
    module TEXT NOT NULL CHECK(module IN ('DISCOVERY_CONTACTS','IMPORT_ENRICH')),
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
    execution_adapter TEXT NOT NULL DEFAULT 'FAKE' CHECK(execution_adapter IN ('FAKE','DISCOVERY_V2','IMPORT_ENRICH')),
    discovery_run_id TEXT,
    completed_company_count INTEGER NOT NULL DEFAULT 0,
    partial_company_count INTEGER NOT NULL DEFAULT 0,
    failed_company_count INTEGER NOT NULL DEFAULT 0,
    ineligible_company_count INTEGER NOT NULL DEFAULT 0,
    progress_stage TEXT,
    import_total_rows INTEGER NOT NULL DEFAULT 0,
    import_matched_count INTEGER NOT NULL DEFAULT 0,
    import_ambiguous_count INTEGER NOT NULL DEFAULT 0,
    import_unresolved_count INTEGER NOT NULL DEFAULT 0,
    import_resolved_without_ai_count INTEGER NOT NULL DEFAULT 0,
    import_matched_requires_enrichment_count INTEGER NOT NULL DEFAULT 0,
    import_matched_company_count INTEGER NOT NULL DEFAULT 0,
    import_existing_satisfied_company_count INTEGER NOT NULL DEFAULT 0,
    import_discovery_required_company_count INTEGER NOT NULL DEFAULT 0,
    import_discovery_processed_company_count INTEGER NOT NULL DEFAULT 0,
    import_discovery_usable_company_count INTEGER NOT NULL DEFAULT 0,
    import_discovery_missing_company_count INTEGER NOT NULL DEFAULT 0,
    import_identity_unmatched_rows INTEGER NOT NULL DEFAULT 0,
    import_identity_task_count INTEGER NOT NULL DEFAULT 0,
    import_identity_local_resolved_count INTEGER NOT NULL DEFAULT 0,
    import_identity_search_eligible_count INTEGER NOT NULL DEFAULT 0,
    import_identity_search_ineligible_count INTEGER NOT NULL DEFAULT 0,
    import_identity_planned_query_count INTEGER NOT NULL DEFAULT 0,
    import_identity_actual_query_count INTEGER NOT NULL DEFAULT 0,
    import_identity_actual_provider_request_count INTEGER NOT NULL DEFAULT 0,
    identity_task_cap INTEGER NOT NULL DEFAULT 1000,
    identity_query_cap INTEGER NOT NULL DEFAULT 2000,
    identity_estimated_provider_requests INTEGER NOT NULL DEFAULT 0,
    identity_max_provider_requests INTEGER NOT NULL DEFAULT 0,
    identity_estimated_cost REAL,
    identity_actual_cost REAL,
    identity_approval_status TEXT NOT NULL DEFAULT 'NOT_CONFIGURED',
    requested_outputs_json TEXT,
    workspace_id TEXT REFERENCES workspaces(workspace_id),
    origin_surface TEXT NOT NULL DEFAULT 'CONTROL_ROOM'
        CHECK(origin_surface IN ('CONTROL_ROOM','MERLIN')),
    CHECK(origin_surface != 'MERLIN' OR workspace_id IS NOT NULL),
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
    status TEXT NOT NULL CHECK(status IN ('MAPPING','REVIEW','CONFIRMED')),
    workspace_id TEXT REFERENCES workspaces(workspace_id),
    origin_surface TEXT NOT NULL DEFAULT 'CONTROL_ROOM'
        CHECK(origin_surface IN ('CONTROL_ROOM','MERLIN')),
    CHECK(origin_surface != 'MERLIN' OR workspace_id IS NOT NULL)
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
    processing_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(processing_status IN ('PENDING','COMPLETED')),
    match_evidence_json TEXT NOT NULL DEFAULT '[]',
    conflicts_json TEXT NOT NULL DEFAULT '[]',
    route_hint TEXT,
    reusable_enrichment INTEGER NOT NULL DEFAULT 0 CHECK(reusable_enrichment IN (0,1)),
    ai_eligibility TEXT NOT NULL DEFAULT 'NOT_EVALUATED',
    ai_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    estimated_input_tokens INTEGER,
    estimated_output_tokens INTEGER,
    estimated_cost REAL,
    actual_input_tokens INTEGER,
    actual_output_tokens INTEGER,
    actual_cost REAL,
    enrichment_status TEXT NOT NULL DEFAULT 'NOT_EVALUATED',
    enrichment_json TEXT NOT NULL DEFAULT '{}',
    discovery_status TEXT,
    initial_match_status TEXT,
    initial_match_method TEXT,
    initial_match_evidence_json TEXT NOT NULL DEFAULT '[]',
    initial_conflicts_json TEXT NOT NULL DEFAULT '[]',
    identity_task_id TEXT,
    identity_status TEXT NOT NULL DEFAULT 'NOT_EVALUATED',
    PRIMARY KEY(job_id,item_position),
    FOREIGN KEY(upload_id,upload_row_number) REFERENCES upload_rows(upload_id,row_number)
);

CREATE TABLE IF NOT EXISTS identity_resolution_tasks (
    task_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES control_jobs(job_id) ON DELETE CASCADE,
    identity_fingerprint TEXT NOT NULL,
    normalized_input_json TEXT NOT NULL,
    origin_status TEXT NOT NULL CHECK(origin_status IN ('AMBIGUOUS','UNRESOLVED')),
    origin_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
    origin_conflicts_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('PENDING','LOCAL_RESOLVED','SEARCH_ELIGIBLE',
        'SEARCH_PLANNED','SEARCH_RESOLVED','AMBIGUOUS','UNRESOLVED','NOT_ELIGIBLE',
        'EXTERNAL_ENTITY_IDENTIFIED','CANONICAL_NOT_FOUND','FAILED')),
    canonical_company_id INTEGER,
    identity_resolution_status TEXT NOT NULL DEFAULT 'UNRESOLVED' CHECK(
        identity_resolution_status IN ('RESOLVED_EXISTING','RESOLVED_NEW_ENTITY',
        'AMBIGUOUS','UNRESOLVED','NOT_ELIGIBLE')),
    canonical_persistence_status TEXT NOT NULL DEFAULT 'NOT_APPLICABLE' CHECK(
        canonical_persistence_status IN ('EXISTING','PENDING_CREATE','CREATED',
        'NOT_APPLICABLE','FAILED')),
    resolution_rule TEXT,
    decisive_evidence_json TEXT NOT NULL DEFAULT '[]',
    alternative_company_ids_json TEXT NOT NULL DEFAULT '[]',
    conflicts_json TEXT NOT NULL DEFAULT '[]',
    resolver_version TEXT NOT NULL,
    proposed_identity_json TEXT,
    enrichment_scope TEXT NOT NULL DEFAULT 'NOT_EVALUATED' CHECK(enrichment_scope IN (
        'BULK_IN_SCOPE','OUTSIDE_BULK_SCOPE','ON_DEMAND','NOT_EVALUATED')),
    research_mode TEXT NOT NULL DEFAULT 'ON_DEMAND' CHECK(research_mode IN (
        'BULK','ON_DEMAND')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_message TEXT,
    UNIQUE(job_id,identity_fingerprint)
);

CREATE TABLE IF NOT EXISTS identity_task_rows (
    task_id TEXT NOT NULL REFERENCES identity_resolution_tasks(task_id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    item_position INTEGER NOT NULL,
    PRIMARY KEY(task_id,item_position),
    UNIQUE(job_id,item_position),
    FOREIGN KEY(job_id,item_position) REFERENCES job_items(job_id,item_position) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS identity_search_queries (
    query_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES identity_resolution_tasks(task_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 2),
    provider TEXT NOT NULL,
    query_text TEXT NOT NULL,
    query_strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    locale_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PLANNED','RUNNING','COMPLETED','FAILED','INTERRUPTED')),
    sanitized_error TEXT,
    raw_result_json TEXT,
    logical_call_count INTEGER NOT NULL DEFAULT 0,
    provider_request_count INTEGER NOT NULL DEFAULT 0,
    uncertain_billing INTEGER NOT NULL DEFAULT 0 CHECK(uncertain_billing IN (0,1)),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(task_id,sequence),
    UNIQUE(task_id,query_text)
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
CREATE INDEX IF NOT EXISTS job_items_processing ON job_items(job_id,processing_status,item_position);
CREATE INDEX IF NOT EXISTS job_items_company ON job_items(job_id,company_id,match_status);
CREATE INDEX IF NOT EXISTS job_artifacts_job ON job_artifacts(job_id,artifact_type);
CREATE INDEX IF NOT EXISTS identity_tasks_job ON identity_resolution_tasks(job_id,status);
CREATE INDEX IF NOT EXISTS identity_queries_task ON identity_search_queries(task_id,sequence);

PRAGMA user_version=8;
