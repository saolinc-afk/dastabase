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
CREATE TABLE profile_deterministic_results(
 deterministic_result_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
 attempt_id TEXT NOT NULL UNIQUE REFERENCES profile_attempts(attempt_id),
 company_id INTEGER NOT NULL, rule_version TEXT NOT NULL, corpus_hash TEXT NOT NULL,
 corpus_status TEXT NOT NULL CHECK(corpus_status IN
 ('RICH_FIRST_PARTY','THIN_FIRST_PARTY','CANDIDATE_ONLY','SEARCH_ONLY','NO_EVIDENCE')),
 fact_count INTEGER NOT NULL, evidence_count INTEGER NOT NULL,
 completed_at TEXT NOT NULL
);
CREATE TABLE profile_deterministic_facts(
 fact_id TEXT PRIMARY KEY,
 deterministic_result_id TEXT NOT NULL REFERENCES profile_deterministic_results(deterministic_result_id),
 run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, company_id INTEGER NOT NULL,
 field_name TEXT NOT NULL, value_json TEXT NOT NULL, state TEXT NOT NULL,
 confidence TEXT NOT NULL CHECK(confidence IN ('HIGH','MEDIUM','UNKNOWN')),
 rule_id TEXT NOT NULL, rule_version TEXT NOT NULL,
 UNIQUE(deterministic_result_id,field_name)
);
CREATE TABLE profile_deterministic_fact_evidence(
 fact_id TEXT NOT NULL REFERENCES profile_deterministic_facts(fact_id),
 profile_evidence_id TEXT REFERENCES profile_evidence(evidence_id),
 block_id TEXT REFERENCES profile_content_blocks(block_id),
 origin_database TEXT, origin_run_id TEXT, origin_attempt_id TEXT,
 origin_result_id TEXT, origin_evidence_id TEXT, origin_content_hash TEXT,
 page_url TEXT, source_locator TEXT, exact_quote TEXT, metadata_value TEXT,
 PRIMARY KEY(fact_id,profile_evidence_id,block_id,source_locator,exact_quote,metadata_value)
);
CREATE INDEX profile_deterministic_company ON profile_deterministic_results(run_id,company_id);
CREATE INDEX profile_deterministic_facts_result ON profile_deterministic_facts(deterministic_result_id);
CREATE TRIGGER profile_run_company_lineage_update
BEFORE UPDATE OF run_id,company_id ON profile_run_companies
WHEN NEW.run_id IS NOT OLD.run_id OR NEW.company_id IS NOT OLD.company_id
BEGIN SELECT RAISE(ABORT,'profile run company lineage is immutable'); END;
CREATE TRIGGER profile_attempt_lineage_update
BEFORE UPDATE OF attempt_id,run_id,company_id ON profile_attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id OR NEW.run_id IS NOT OLD.run_id
 OR NEW.company_id IS NOT OLD.company_id
BEGIN SELECT RAISE(ABORT,'profile attempt lineage is immutable'); END;
CREATE TRIGGER profile_evidence_lineage_insert BEFORE INSERT ON profile_evidence
WHEN NOT EXISTS(SELECT 1 FROM profile_attempts a WHERE a.attempt_id=NEW.attempt_id
 AND a.run_id=NEW.run_id AND a.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'profile evidence lineage mismatch'); END;
CREATE TRIGGER profile_evidence_lineage_update BEFORE UPDATE ON profile_evidence
WHEN NEW.evidence_id IS NOT OLD.evidence_id OR NEW.run_id IS NOT OLD.run_id
 OR NEW.attempt_id IS NOT OLD.attempt_id OR NEW.company_id IS NOT OLD.company_id
 OR NOT EXISTS(SELECT 1 FROM profile_attempts a WHERE a.attempt_id=NEW.attempt_id
 AND a.run_id=NEW.run_id AND a.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'profile evidence lineage is immutable or mismatched'); END;
CREATE TRIGGER profile_block_lineage_insert BEFORE INSERT ON profile_content_blocks
WHEN NOT EXISTS(SELECT 1 FROM profile_evidence e WHERE e.evidence_id=NEW.evidence_id
 AND e.run_id=NEW.run_id AND e.attempt_id=NEW.attempt_id AND e.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'profile block lineage mismatch'); END;
CREATE TRIGGER profile_block_lineage_update BEFORE UPDATE ON profile_content_blocks
WHEN NEW.block_id IS NOT OLD.block_id OR NEW.run_id IS NOT OLD.run_id
 OR NEW.attempt_id IS NOT OLD.attempt_id OR NEW.company_id IS NOT OLD.company_id
 OR NEW.evidence_id IS NOT OLD.evidence_id
 OR NOT EXISTS(SELECT 1 FROM profile_evidence e WHERE e.evidence_id=NEW.evidence_id
 AND e.run_id=NEW.run_id AND e.attempt_id=NEW.attempt_id AND e.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'profile block lineage is immutable or mismatched'); END;
CREATE TRIGGER profile_deterministic_result_lineage_insert BEFORE INSERT ON profile_deterministic_results
WHEN NOT EXISTS(SELECT 1 FROM profile_attempts a WHERE a.attempt_id=NEW.attempt_id
 AND a.run_id=NEW.run_id AND a.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'deterministic result lineage mismatch'); END;
CREATE TRIGGER profile_deterministic_result_lineage_update BEFORE UPDATE ON profile_deterministic_results
WHEN NEW.deterministic_result_id IS NOT OLD.deterministic_result_id
 OR NEW.run_id IS NOT OLD.run_id OR NEW.attempt_id IS NOT OLD.attempt_id
 OR NEW.company_id IS NOT OLD.company_id
 OR NOT EXISTS(SELECT 1 FROM profile_attempts a WHERE a.attempt_id=NEW.attempt_id
 AND a.run_id=NEW.run_id AND a.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'deterministic result lineage is immutable or mismatched'); END;
CREATE TRIGGER profile_deterministic_fact_lineage_insert BEFORE INSERT ON profile_deterministic_facts
WHEN NOT EXISTS(SELECT 1 FROM profile_deterministic_results r
 WHERE r.deterministic_result_id=NEW.deterministic_result_id AND r.run_id=NEW.run_id
 AND r.attempt_id=NEW.attempt_id AND r.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'deterministic fact lineage mismatch'); END;
CREATE TRIGGER profile_deterministic_fact_lineage_update BEFORE UPDATE ON profile_deterministic_facts
WHEN NEW.fact_id IS NOT OLD.fact_id OR NEW.deterministic_result_id IS NOT OLD.deterministic_result_id
 OR NEW.run_id IS NOT OLD.run_id OR NEW.attempt_id IS NOT OLD.attempt_id
 OR NEW.company_id IS NOT OLD.company_id
 OR NOT EXISTS(SELECT 1 FROM profile_deterministic_results r
 WHERE r.deterministic_result_id=NEW.deterministic_result_id AND r.run_id=NEW.run_id
 AND r.attempt_id=NEW.attempt_id AND r.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'deterministic fact lineage is immutable or mismatched'); END;
CREATE TRIGGER profile_deterministic_evidence_lineage_insert BEFORE INSERT ON profile_deterministic_fact_evidence
WHEN (NEW.profile_evidence_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f JOIN profile_evidence e
 ON e.evidence_id=NEW.profile_evidence_id AND e.run_id=f.run_id
 AND e.attempt_id=f.attempt_id AND e.company_id=f.company_id
 JOIN profile_run_companies rc ON rc.run_id=f.run_id AND rc.company_id=f.company_id
 WHERE f.fact_id=NEW.fact_id AND e.origin_database IS NEW.origin_database
 AND e.origin_run_id IS NEW.origin_run_id AND e.origin_attempt_id IS NEW.origin_attempt_id
 AND e.origin_evidence_id IS NEW.origin_evidence_id
 AND e.origin_content_hash IS NEW.origin_content_hash
 AND rc.discovery_result_id IS NEW.origin_result_id
 AND COALESCE(e.final_url,e.requested_url) IS NEW.page_url))
 OR (NEW.profile_evidence_id IS NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f
 JOIN profile_run_companies rc ON rc.run_id=f.run_id AND rc.company_id=f.company_id
 JOIN profile_runs pr ON pr.run_id=f.run_id WHERE f.fact_id=NEW.fact_id
 AND rc.discovery_result_id IS NEW.origin_result_id
 AND json_extract(pr.source_descriptor_json,'$.database_path') IS NEW.origin_database
 AND json_extract(pr.source_descriptor_json,'$.run_id') IS NEW.origin_run_id))
 OR (NEW.block_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f JOIN profile_content_blocks b
 ON b.block_id=NEW.block_id AND b.evidence_id=NEW.profile_evidence_id
 AND b.run_id=f.run_id AND b.attempt_id=f.attempt_id AND b.company_id=f.company_id
 WHERE f.fact_id=NEW.fact_id))
BEGIN SELECT RAISE(ABORT,'deterministic evidence lineage mismatch'); END;
CREATE TRIGGER profile_deterministic_evidence_lineage_update BEFORE UPDATE ON profile_deterministic_fact_evidence
WHEN (NEW.profile_evidence_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f JOIN profile_evidence e
 ON e.evidence_id=NEW.profile_evidence_id AND e.run_id=f.run_id
 AND e.attempt_id=f.attempt_id AND e.company_id=f.company_id
 JOIN profile_run_companies rc ON rc.run_id=f.run_id AND rc.company_id=f.company_id
 WHERE f.fact_id=NEW.fact_id AND e.origin_database IS NEW.origin_database
 AND e.origin_run_id IS NEW.origin_run_id AND e.origin_attempt_id IS NEW.origin_attempt_id
 AND e.origin_evidence_id IS NEW.origin_evidence_id
 AND e.origin_content_hash IS NEW.origin_content_hash
 AND rc.discovery_result_id IS NEW.origin_result_id
 AND COALESCE(e.final_url,e.requested_url) IS NEW.page_url))
 OR (NEW.profile_evidence_id IS NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f
 JOIN profile_run_companies rc ON rc.run_id=f.run_id AND rc.company_id=f.company_id
 JOIN profile_runs pr ON pr.run_id=f.run_id WHERE f.fact_id=NEW.fact_id
 AND rc.discovery_result_id IS NEW.origin_result_id
 AND json_extract(pr.source_descriptor_json,'$.database_path') IS NEW.origin_database
 AND json_extract(pr.source_descriptor_json,'$.run_id') IS NEW.origin_run_id))
 OR (NEW.block_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f JOIN profile_content_blocks b
 ON b.block_id=NEW.block_id AND b.evidence_id=NEW.profile_evidence_id
 AND b.run_id=f.run_id AND b.attempt_id=f.attempt_id AND b.company_id=f.company_id
 WHERE f.fact_id=NEW.fact_id))
BEGIN SELECT RAISE(ABORT,'deterministic evidence lineage mismatch'); END;
