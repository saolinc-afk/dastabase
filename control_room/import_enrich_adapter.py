"""Durable deterministic Import & Enrich matching adapter."""
from pathlib import Path

from control_room.enrichment_index import EnrichmentIndex
from control_room.matching import ImportMatcher
from control_room.uploads import normalize_import_row


class ImportEnrichAdapter:
    def __init__(self, repository, canonical_db, discovery_results=(), *, item_hook=None):
        self.repository = repository
        self.canonical_db = Path(canonical_db).expanduser().absolute()
        self.discovery_results = tuple(discovery_results)
        self.item_hook = item_hook
        self.worker_id = None

    def run(self, job, progress):
        if job['module'] != 'IMPORT_ENRICH':
            raise ValueError('ImportEnrichAdapter requires an IMPORT_ENRICH job')
        self.repository.set_import_stage(job['job_id'], 'PARSING', self.worker_id)
        index = EnrichmentIndex(self.canonical_db, self.discovery_results)
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
        return 'COMPLETED'
