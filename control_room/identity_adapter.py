"""Durable local/search identity stage for IMPORT_ENRICH; no live provider."""
from control_room.identity_resolver import (RESOLVER_VERSION, fingerprint,
    local_resolve, plan_queries, registration_input, resolve_results)
from control_room.uploads import normalize_import_row


class IdentityResolutionAdapter:
    """Runs local rules and consumes only explicitly injected stored results."""

    def __init__(self, repository, index, *, injected_results=None,
                 task_cap=1000, query_cap=2000):
        self.repository = repository
        self.index = index
        self.injected_results = injected_results
        self.task_cap = task_cap
        self.query_cap = query_cap

    @staticmethod
    def _candidate_ids(item):
        return sorted({company_id for evidence in item['match_evidence']
                       for company_id in evidence.get('company_ids', ())})

    def run(self, job_id, worker_id):
        self.repository.recover_identity_queries(job_id, worker_id)
        self.repository.set_import_stage(job_id, 'IDENTITY_LOCAL', worker_id)
        for item in self.repository.identity_registration_items(job_id):
            normalized = normalize_import_row(item['headers'], item['original_values'],
                item['mapping'], row_key=item['upload_row_number'])
            inputs = registration_input(normalized)
            candidates = self._candidate_ids(item)
            spec = {'inputs': inputs, 'origin_status': item['initial_match_status'],
                    'candidate_ids': candidates, 'conflicts': item['conflicts'],
                    'resolver_version': RESOLVER_VERSION}
            spec['fingerprint'] = fingerprint(inputs, spec['origin_status'], candidates,
                                              spec['conflicts'])
            self.repository.ensure_identity_task(
                job_id, item['item_position'], spec, worker_id)

        for task in self.repository.identity_tasks(job_id):
            if task['status'] != 'PENDING':
                continue
            decision = local_resolve(self.index, task['normalized_input'],
                task['origin_status'], task['origin_candidate_ids'], task['origin_conflicts'])
            self.repository.apply_identity_decision(task['task_id'], decision, worker_id)

        self.repository.set_import_stage(job_id, 'IDENTITY_PREFLIGHT', worker_id)
        for task in self.repository.identity_tasks(job_id):
            if task['status'] != 'SEARCH_ELIGIBLE':
                continue
            plans = plan_queries(task['normalized_input'], task['alternative_company_ids'],
                                 self.index)
            self.repository.plan_identity_queries(task['task_id'], plans, worker_id)
        preflight = self.repository.update_identity_preflight(
            job_id, self.task_cap, self.query_cap, worker_id=worker_id)

        self.repository.set_import_stage(job_id, 'IDENTITY_SEARCH', worker_id)
        if self.injected_results is not None:
            for task in self.repository.identity_tasks(job_id):
                if task['status'] != 'SEARCH_PLANNED':
                    continue
                queries = self.repository.identity_queries(task['task_id'])
                supplied = self.injected_results(task, queries) or {}
                for query in queries:
                    if query['status'] == 'COMPLETED':
                        continue
                    payload = supplied.get(query['sequence'])
                    if payload is not None:
                        self.repository.persist_identity_response(
                            query['query_id'], payload, provider='INJECTED',
                            worker_id=worker_id)

        for task in self.repository.identity_tasks(job_id):
            if task['status'] != 'SEARCH_PLANNED':
                continue
            queries = self.repository.identity_queries(task['task_id'])
            completed = [query for query in queries if query['status'] == 'COMPLETED']
            if not completed:
                continue
            decision = resolve_results(self.index, task['normalized_input'],
                task['origin_status'], task['origin_candidate_ids'],
                [query['raw_result'] for query in completed])
            self.repository.apply_identity_decision(task['task_id'], decision, worker_id)
        return self.repository.update_identity_preflight(
            job_id, self.task_cap, self.query_cap, worker_id=worker_id)
