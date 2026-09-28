-- This schema belongs only to an isolated Discovery v2 result database.
CREATE TABLE discovery_runs (
 run_id TEXT PRIMARY KEY,
 job_type TEXT NOT NULL CHECK(job_type = 'DISCOVERY_CONTACTS'),
 execution_mode TEXT NOT NULL CHECK(execution_mode = 'FRESH_DISCOVERY'),
 status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','PARTIAL','COMPLETED')),
 engine_version TEXT NOT NULL, rule_version TEXT NOT NULL,
 source_snapshot_json TEXT NOT NULL, config_json TEXT NOT NULL,
 config_hash TEXT NOT NULL, manifest_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE TABLE discovery_run_companies (
 run_id TEXT NOT NULL REFERENCES discovery_runs(run_id), company_id INTEGER NOT NULL,
 source_namespace TEXT NOT NULL, manifest_position INTEGER NOT NULL,
 identity_snapshot_json TEXT NOT NULL,
 eligibility_status TEXT NOT NULL CHECK(eligibility_status IN ('ELIGIBLE','INELIGIBLE')),
 eligibility_reason TEXT,
 status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','FAILED','PARTIAL','COMPLETED','INELIGIBLE')),
 selected_attempt_id TEXT,
 PRIMARY KEY(run_id, company_id), UNIQUE(run_id, manifest_position),
 CHECK((eligibility_status='ELIGIBLE' AND eligibility_reason IS NULL) OR
       (eligibility_status='INELIGIBLE' AND eligibility_reason='BANKRUPTCY')),
 FOREIGN KEY(selected_attempt_id,run_id,company_id)
 REFERENCES discovery_attempts(attempt_id,run_id,company_id)
);
CREATE TABLE discovery_attempts (
 attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, company_id INTEGER NOT NULL,
 attempt_number INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('RUNNING','INTERRUPTED','FAILED','PARTIAL','COMPLETED')),
 started_at TEXT NOT NULL, finished_at TEXT,
 request_counts_json TEXT NOT NULL DEFAULT '{}', diagnostics_json TEXT NOT NULL DEFAULT '{}',
 UNIQUE(attempt_id,run_id,company_id), UNIQUE(run_id,company_id,attempt_number),
 FOREIGN KEY(run_id,company_id) REFERENCES discovery_run_companies(run_id,company_id)
);
CREATE TABLE discovery_evidence (
 evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 company_id INTEGER NOT NULL, source_kind TEXT NOT NULL,
 provider TEXT, source_class TEXT NOT NULL, observed_at TEXT NOT NULL,
 query_type TEXT, query_text TEXT, result_rank INTEGER, result_url TEXT, result_host TEXT,
 title TEXT, snippet_body TEXT, requested_url TEXT, final_url TEXT, http_status INTEGER,
 content_hash TEXT, evidence_payload_json TEXT NOT NULL,
 UNIQUE(evidence_id,run_id,attempt_id,company_id),
 FOREIGN KEY(attempt_id,run_id,company_id) REFERENCES discovery_attempts(attempt_id,run_id,company_id)
);
CREATE TABLE discovery_observations (
 observation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 company_id INTEGER NOT NULL, evidence_id TEXT NOT NULL,
 observation_type TEXT NOT NULL, raw_value TEXT NOT NULL, normalized_value TEXT NOT NULL,
 value_json TEXT NOT NULL, extraction_method TEXT NOT NULL, extractor_version TEXT NOT NULL,
 source_locator TEXT NOT NULL, observed_at TEXT NOT NULL,
 UNIQUE(observation_id,run_id,attempt_id,company_id),
 FOREIGN KEY(evidence_id,run_id,attempt_id,company_id)
 REFERENCES discovery_evidence(evidence_id,run_id,attempt_id,company_id)
);
CREATE TABLE discovery_contacts (
 contact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 company_id INTEGER NOT NULL, contact_type TEXT NOT NULL CHECK(contact_type IN ('EMAIL','PHONE')),
 raw_value TEXT NOT NULL, normalized_value TEXT NOT NULL, primary_observation_id TEXT NOT NULL,
 supporting_observation_ids_json TEXT NOT NULL,
 attribution_status TEXT NOT NULL CHECK(attribution_status IN ('CANDIDATE','ATTRIBUTED','REJECTED','UNCERTAIN')),
 attribution_reason TEXT NOT NULL, rule_version TEXT NOT NULL, roles_json TEXT NOT NULL,
 role_basis_json TEXT NOT NULL, person_name TEXT, person_title TEXT, department TEXT,
 person_context_evidence_json TEXT NOT NULL DEFAULT '{}',
 UNIQUE(contact_id,run_id,attempt_id,company_id), UNIQUE(attempt_id,contact_type,normalized_value),
 FOREIGN KEY(primary_observation_id,run_id,attempt_id,company_id)
 REFERENCES discovery_observations(observation_id,run_id,attempt_id,company_id)
);
CREATE TABLE discovery_company_results (
 result_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE,
 company_id INTEGER NOT NULL, website_status TEXT NOT NULL, official_website TEXT,
 verified_scope TEXT, website_observation_id TEXT, website_evidence_ids_json TEXT NOT NULL,
 website_assessment_json TEXT NOT NULL, default_email_contact_id TEXT, default_phone_contact_id TEXT,
 contact_outcome TEXT NOT NULL, completed_at TEXT NOT NULL,
 FOREIGN KEY(attempt_id,run_id,company_id) REFERENCES discovery_attempts(attempt_id,run_id,company_id),
 FOREIGN KEY(website_observation_id,run_id,attempt_id,company_id)
 REFERENCES discovery_observations(observation_id,run_id,attempt_id,company_id),
 FOREIGN KEY(default_email_contact_id,run_id,attempt_id,company_id)
 REFERENCES discovery_contacts(contact_id,run_id,attempt_id,company_id),
 FOREIGN KEY(default_phone_contact_id,run_id,attempt_id,company_id)
 REFERENCES discovery_contacts(contact_id,run_id,attempt_id,company_id)
);
CREATE INDEX evidence_attempt ON discovery_evidence(attempt_id);
CREATE INDEX observations_attempt ON discovery_observations(attempt_id);
CREATE INDEX companies_pending ON discovery_run_companies(run_id,status,manifest_position);
