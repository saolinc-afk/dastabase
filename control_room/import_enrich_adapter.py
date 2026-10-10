"""Durable deterministic Import & Enrich matching adapter."""
from pathlib import Path

from control_room.enrichment_index import EnrichmentIndex
from control_room.identity_adapter import IdentityResolutionAdapter
from control_room.import_export import export_import_xlsx
from control_room.import_outputs import (decode_requested_outputs,
                                         missing_discovery_outputs)
from control_room.knowledge_repository import KnowledgeRepository
from control_room.matching import ImportMatcher
from control_room.uploads import normalize_import_row


class ImportEnrichAdapter:
    def __init__(self, repository, canonical_db, discovery_results=(), *,
                 discovery_adapter=None, item_hook=None, identity_results=None,
                 identity_task_cap=1000, identity_query_cap=2000, storage_root=None):
        self.repository = repository
        self.canonical_db = Path(canonical_db).expanduser().absolute()
        self.discovery_results = tuple(discovery_results)
        self.discovery_adapter = discovery_adapter
        self.item_hook = item_hook
        self.identity_results = identity_results
        self.identity_task_cap = identity_task_cap
        self.identity_query_cap = identity_query_cap
        self.storage_root = (Path(storage_root).expanduser().absolute()
                             if storage_root is not None else None)
        self.worker_id = None

    def run(self, job, progress):
        if job['module'] != 'IMPORT_ENRICH':
            raise ValueError('ImportEnrichAdapter requires an IMPORT_ENRICH job')
        requested_outputs = decode_requested_outputs(job.get('requested_outputs_json'))
        self.repository.set_import_stage(job['job_id'], 'PARSING', self.worker_id)
        knowledge = KnowledgeRepository(self.canonical_db, self.discovery_results)
        index = EnrichmentIndex(self.canonical_db, knowledge=knowledge)
        matcher = ImportMatcher(index)
        self.repository.set_import_stage(job['job_id'], 'MATCHING', self.worker_id)
        for item in self.repository.pending_import_items(job['job_id']):
            normalized = normalize_import_row(
                item['headers'], item['original_values'], item['mapping'],
                row_key=item['upload_row_number'])
            match = matcher.match(normalized, row_key=item['upload_row_number'])
            summary = self.repository.complete_import_item(
                job['job_id'], item['item_position'], match, self.worker_id)
            if self.item_hook:
                self.item_hook(item, summary)
        identity = IdentityResolutionAdapter(self.repository, index,
            injected_results=self.identity_results, task_cap=self.identity_task_cap,
            query_cap=self.identity_query_cap)
        identity.run(job['job_id'], self.worker_id)
        self.repository.set_import_stage(job['job_id'], 'EXISTING_ENRICHMENT', self.worker_id)
        matched_ids = sorted({item['company_id'] for item in
            self.repository.job_items(job['job_id'])
            if item['match_status'] == 'MATCHED' and item['company_id'] is not None})
        payloads = {company_id: self._payload(knowledge, company_id,
                    requested_outputs=requested_outputs)
                    for company_id in matched_ids}
        sufficient = {company_id for company_id in matched_ids
                      if not missing_discovery_outputs(
                          knowledge.snapshot(company_id), requested_outputs)}
        discovery_ids = set(matched_ids) - sufficient
        for company_id in discovery_ids:
            payloads[company_id]['discovery_status'] = 'PENDING'
        self.repository.persist_import_enrichment(
            job['job_id'], payloads, sufficient, discovery_ids, self.worker_id)
        if not discovery_ids:
            self._export(job['job_id'])
            return 'COMPLETED'
        if self.discovery_adapter is None:
            raise ValueError('Selective Discovery adapter is not configured')
        self.repository.set_import_stage(job['job_id'], 'DISCOVERY', self.worker_id)
        self.discovery_adapter.worker_id = self.worker_id
        outcome, export_rows, results_path, run_id = self.discovery_adapter.run_subset(
            job, sorted(discovery_ids), lambda counts:
                self.repository.update_import_discovery_progress(
                    job['job_id'], counts, self.worker_id))
        refreshed_knowledge = KnowledgeRepository(self.canonical_db,
            [*self.discovery_results, results_path],
            run_ids={str(Path(results_path).resolve()): run_id})
        discovery_status = {row['company_id']: row.get('website_status') or 'REVIEW'
                            for row in export_rows}
        payloads = {company_id: self._payload(refreshed_knowledge, company_id,
                    discovery_status.get(company_id), requested_outputs)
                    for company_id in matched_ids}
        sufficient = {company_id for company_id in matched_ids
                      if not missing_discovery_outputs(
                          refreshed_knowledge.snapshot(company_id), requested_outputs)}
        self.repository.persist_import_enrichment(job['job_id'], payloads, sufficient,
            discovery_ids, self.worker_id, after_discovery=True)
        self._export(job['job_id'])
        return 'PARTIAL' if outcome['status'] == 'PARTIAL' else 'COMPLETED'

    def _export(self, job_id):
        if self.storage_root is None:
            return None
        job = self.repository.get_job(job_id)
        items = self.repository.job_items(job_id)
        upload = self.repository.get_upload(items[0]['upload_id']) if items else None
        if not upload or (upload['format'] != 'XLSX' and
                          not (job and job.get('origin_surface') == 'MERLIN'
                               and upload['format'] == 'CSV')):
            return None
        self.repository.set_import_stage(job_id, 'EXPORT', self.worker_id)
        return export_import_xlsx(self.repository, job_id, self.storage_root,
                                  worker_id=self.worker_id)

    @staticmethod
    def _payload(knowledge, company_id, discovery_status=None,
                 requested_outputs=None):
        record = knowledge.record(company_id)
        company = record['identity']
        financials = record['financials']
        facts = ([record['website']] if record['website'] else []) + record['emails'] + record['phones']
        provenance = [locator for fact in facts for locator in fact['evidence_locators']]
        payload = {
            'canonical_company_id': company_id,
            'canonical_company_name': company.get('company_name'),
            'tax_number': company.get('tax_number'),
            'registration_number': company.get('registration_number'),
            'address': company.get('address'),
            'municipality': company.get('municipality'),
            'revenue_2025': financials.get('revenue_2025'),
            'profit_2025': financials.get('profit_2025'),
            'employees_2025': financials.get('employees_2025'),
            'assets_2025': financials.get('assets_2025'),
            'capital_2025': financials.get('capital_2025'),
            'official_website': record['website']['value'] if record['website'] else None,
            'website_status': record['website']['status'] if record['website'] else None,
            'default_email': (record['default_email']['value']
                              if record['default_email'] else None),
            'default_phone': (record['default_phone']['value']
                              if record['default_phone'] else None),
            'sources': list(dict.fromkeys(item['source_type'] for item in provenance)),
            'provenance': provenance,
            'knowledge_conflicts': record['conflicts'],
            'discovery_status': (discovery_status or
                                 (record['website']['status'] if record['website'] else None)),
        }
        if requested_outputs is not None:
            payload['requested_outputs'] = list(requested_outputs)
        return payload
