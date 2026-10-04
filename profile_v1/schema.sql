PRAGMA foreign_keys=ON;
CREATE TABLE profile_runs(
 run_id TEXT PRIMARY KEY, job_type TEXT NOT NULL CHECK(job_type='PROFILE_ACTIVITY'),
 status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','PARTIAL','COMPLETED')),
 engine_version TEXT NOT NULL, rule_version TEXT NOT NULL,
 source_descriptor_json TEXT NOT NULL, manifest_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE TABLE profile_run_companies(
 run_id TEXT NOT NULL REFERENCES profile_runs(run_id), company_id INTEGER NOT NULL,
 manifest_position INTEGER NOT NULL, identity_snapshot_json TEXT NOT NULL,
 registered_activity_json TEXT, discovery_attempt_id TEXT NOT NULL,
 discovery_result_id TEXT NOT NULL, accepted_website TEXT, status TEXT NOT NULL CHECK(status IN
 ('PENDING','RUNNING','FAILED','PARTIAL','COMPLETED','INSUFFICIENT_EVIDENCE','REVIEW')),
 selected_attempt_id TEXT, PRIMARY KEY(run_id,company_id), UNIQUE(run_id,manifest_position)
);
CREATE TABLE profile_attempts(
 attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, company_id INTEGER NOT NULL,
 attempt_number INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN
 ('RUNNING','FAILED','PARTIAL','COMPLETED','INSUFFICIENT_EVIDENCE','REVIEW')),
 started_at TEXT NOT NULL, finished_at TEXT, diagnostics_json TEXT NOT NULL DEFAULT '{}',
 interpreter_output_json TEXT NOT NULL DEFAULT '[]',
 UNIQUE(run_id,company_id,attempt_number),
 FOREIGN KEY(run_id,company_id) REFERENCES profile_run_companies(run_id,company_id)
);
CREATE TABLE profile_evidence(
 evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 company_id INTEGER NOT NULL, source_kind TEXT NOT NULL, source_class TEXT NOT NULL,
 requested_url TEXT, final_url TEXT, title TEXT, snippet_body TEXT, html TEXT,
 content_hash TEXT, observed_at TEXT, payload_json TEXT NOT NULL,
 origin_database TEXT, origin_run_id TEXT, origin_attempt_id TEXT,
 origin_evidence_id TEXT, origin_content_hash TEXT,
 FOREIGN KEY(attempt_id) REFERENCES profile_attempts(attempt_id)
);
CREATE TABLE profile_content_blocks(
 block_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 company_id INTEGER NOT NULL, evidence_id TEXT NOT NULL,
 source_url TEXT, block_type TEXT NOT NULL, source_locator TEXT NOT NULL,
 text TEXT NOT NULL, text_hash TEXT NOT NULL, language TEXT,
 UNIQUE(evidence_id,block_type,source_locator,text_hash),
 FOREIGN KEY(evidence_id) REFERENCES profile_evidence(evidence_id)
);
CREATE TABLE profile_claims(
 claim_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 company_id INTEGER NOT NULL, claim_type TEXT NOT NULL,
 normalized_value_json TEXT NOT NULL, display_value TEXT NOT NULL,
 confidence TEXT NOT NULL CHECK(confidence IN ('HIGH','MEDIUM','LOW')),
 status TEXT NOT NULL CHECK(status IN ('SUPPORTED','REJECTED')),
 ambiguity_note TEXT, rejection_reason TEXT, interpreter_version TEXT NOT NULL,
 taxonomy_version TEXT NOT NULL,
 FOREIGN KEY(attempt_id) REFERENCES profile_attempts(attempt_id)
);
CREATE TABLE profile_claim_evidence(
 claim_id TEXT NOT NULL REFERENCES profile_claims(claim_id),
 block_id TEXT NOT NULL REFERENCES profile_content_blocks(block_id),
 quote TEXT NOT NULL, PRIMARY KEY(claim_id,block_id,quote)
);
CREATE TABLE profile_company_results(
 result_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE,
 company_id INTEGER NOT NULL, profile_status TEXT NOT NULL CHECK(profile_status IN
 ('PROFILED','PARTIAL','INSUFFICIENT_EVIDENCE','REVIEW')),
 registered_activity_json TEXT, supported_claim_ids_json TEXT NOT NULL,
 rejected_claim_ids_json TEXT NOT NULL, overall_confidence TEXT,
 taxonomy_version TEXT NOT NULL, diagnostic_category TEXT NOT NULL,
 evidence_count INTEGER NOT NULL, block_count INTEGER NOT NULL,
 claim_count INTEGER NOT NULL, completed_at TEXT NOT NULL,
 FOREIGN KEY(attempt_id) REFERENCES profile_attempts(attempt_id)
);
CREATE TABLE profile_interpretation_requests(
 request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 company_id INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 model_config_json TEXT NOT NULL, prompt_version TEXT NOT NULL,
 taxonomy_version TEXT NOT NULL, input_hash TEXT NOT NULL,
 request_payload_json TEXT NOT NULL, raw_response_json TEXT,
 candidate_claims_json TEXT, requested_at TEXT NOT NULL, completed_at TEXT,
 status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','ERROR','CACHED')),
 error_message TEXT, cached_from_request_id TEXT,
 FOREIGN KEY(attempt_id) REFERENCES profile_attempts(attempt_id)
);
CREATE INDEX profile_evidence_attempt ON profile_evidence(attempt_id);
CREATE INDEX profile_blocks_attempt ON profile_content_blocks(attempt_id);
CREATE INDEX profile_claims_attempt ON profile_claims(attempt_id);
CREATE INDEX profile_interpretation_cache ON profile_interpretation_requests(
 provider,model,model_config_json,prompt_version,taxonomy_version,input_hash,status);
