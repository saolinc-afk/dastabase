"""Serial, bounded DISCOVERY_CONTACTS execution with durable run-local resume."""
import argparse
import json
import sqlite3

from bs4 import BeautifulSoup
from discovery.domain_generator import generate_candidates, normalize_url
from discovery.domain_policy import blocks_official
from discovery.ownership import in_scope
from discovery.website_verifier import internal_identity_links
from discovery_v2.candidates import eligible, rank, brand_match
from discovery.domain_generator import normalize_domain
from discovery_v2.contacts import resolve_contacts, select_default
from discovery_v2.evidence import EvidenceWriter
from discovery_v2.interfaces import RecordingFetcher, VERIFIER_VERSION, evaluate_website, new_fetcher
from discovery_v2.models import Config, EXECUTION_MODE, JOB_TYPE
from discovery_v2.search import (DOMAIN_CONTACT, default_provider, domain_query, queries,
                                 successful)
from discovery_v2.store import Store, encode, now, read_manifest


def discover(store, context, company, config, provider, fetcher_factory, evaluator):
    writer = EvidenceWriter(store, context, company)
    fetcher = RecordingFetcher(fetcher_factory(config), writer)
    planned = queries(company, config.use_municipality)
    diagnostics = {'queries': [dict(query_type=kind, query_text=query, provider=provider.name, status='PENDING')
                               for kind, query in planned] + [dict(query_type=DOMAIN_CONTACT, status='PENDING')],
                   'website_candidates': [], 'verifier_version': VERIFIER_VERSION}
    counts = {'search_calls': 0, 'http_requests': 0}

    def checkpoint():
        counts['http_requests'] = fetcher.requests
        diagnostics['fetch_errors'] = list(fetcher.errors)
        store.progress(context['attempt_id'], diagnostics, counts)

    def search(query):
        query_type, query_text = query
        entry = next(q for q in diagnostics['queries'] if q['query_type'] == query_type)
        entry.update(query_text=query_text, provider=provider.name, status='RUNNING')
        entry.pop('reason', None)
        counts['search_calls'] += 1
        checkpoint()
        try:
            outcome = provider.search(query_text, config.results_per_query)
            # Keep explicitly supplied legacy/test providers compatible.
            if isinstance(outcome, list):
                outcome = successful(outcome)
            results = list(outcome.results)
            for rank, item in enumerate(results[:config.results_per_query], 1):
                position = item.get('position')
                writer.search_result(item, query_type, query_text, provider.name,
                                     position if isinstance(position, int) and position > 0 else rank)
            entry.update(status='COMPLETED', search_outcome=outcome.status,
                         result_count=min(len(results), config.results_per_query))
        except Exception as exc:
            entry.update(status='FAILED', search_outcome='FAILED', error=f'{type(exc).__name__}: {exc}')
        checkpoint()

    selected = None
    evaluated = set()
    evaluated_hosts = set()

    def evaluate_candidates(limit):
        nonlocal selected
        ordered = sorted((o for o in writer.observations if o['observation_type'] == 'WEBSITE_CANDIDATE'), key=lambda o: rank(company, o))
        reserved = []
        hosts = set()
        for observation in ordered:
            host = normalize_domain(observation['normalized_value'])
            if (observation['extraction_method'] in ('email_domain', 'domain_guess')
                    and brand_match(company, observation['normalized_value']) and host not in hosts):
                reserved.append(observation)
                hosts.add(host)
                if len(reserved) == min(2, limit):
                    break
        for observation in reserved + [o for o in ordered if o not in reserved]:
            if observation['observation_type'] != 'WEBSITE_CANDIDATE':
                continue
            url = normalize_url(observation['normalized_value'])
            if not url or url in evaluated:
                continue
            if not eligible(company, url, observation.get('value', {}).get('title', ''), observation.get('value', {}).get('body', '')):
                evaluated.add(url)
                diagnostics['website_candidates'].append({'url': url, 'observation_id': observation['observation_id'],
                    'status': 'INELIGIBLE', 'reason': 'Third-party/disallowed official website'})
                continue
            if normalize_domain(url) in evaluated_hosts:
                continue
            if observation['extraction_method'] != 'domain_guess' and not brand_match(company, url) and not observation.get('value', {}).get('identity_match'):
                continue
            used = sum(c['status'] != 'INELIGIBLE' for c in diagnostics['website_candidates'])
            if selected or used >= limit:
                continue
            evaluated.add(url)
            evaluated_hosts.add(normalize_domain(url))
            assessment = evaluator(company, url, fetcher)
            entry = {'url': url, 'observation_id': observation['observation_id'], 'status': assessment['status'], 'assessment': assessment}
            diagnostics['website_candidates'].append(entry)
            # Defence in depth: an adapter's Boolean cannot authorize a publisher URL.
            scope = assessment.get('verified_scope') or assessment.get('ownership', {}).get('scope')
            if (assessment.get('verified') and assessment.get('ownership', {}).get('status') == 'VERIFIED'
                    and scope and normalize_url(scope) and not blocks_official(scope)):
                selected = (observation, assessment, scope)
            checkpoint()

    completed = False
    try:
        if getattr(provider, 'staged', False):
            # Serper credits are consumed progressively. A verified first-stage
            # result is sufficient; unresolved companies retain the later query
            # types as explicit escalation stages.
            search(planned[0])
            for url in generate_candidates(company['company_name']):
                writer.generated(url)
            evaluate_candidates(max(1, config.max_candidates - 2))
            for query in planned[1:]:
                if (config.max_search_queries_per_company is not None
                        and counts['search_calls'] >= config.max_search_queries_per_company):
                    entry = next(q for q in diagnostics['queries'] if q['query_type'] == query[0])
                    entry.update(status='SKIPPED', reason='Search query budget exhausted')
                elif selected:
                    entry = next(q for q in diagnostics['queries'] if q['query_type'] == query[0])
                    entry.update(status='SKIPPED', reason='Verified official website from earlier search stage')
                else:
                    search(query)
                    evaluate_candidates(max(1, config.max_candidates - 2))
        else:
            # Preserve the existing DDGS query schedule and evaluation order.
            for query in planned:
                search(query)
            for url in generate_candidates(company['company_name']):
                writer.generated(url)
            evaluate_candidates(max(1, config.max_candidates - 2))
        # A likely domain may still be unverified. Never seed this from a publisher.
        likely = selected[2] if selected else next((c['url'] for c in diagnostics['website_candidates']
                    if c['status'] == 'REVIEW' and brand_match(company, c['url'])
                    and c.get('assessment', {}).get('relationship') != 'THIRD_PARTY'
                    and any(set(p.get('signals', [])) & {'company_name_exact', 'organization_name_exact', 'tax_exact', 'registration_exact'}
                            for p in c.get('assessment', {}).get('evidence', []))), None)
        staged_budget_exhausted = (getattr(provider, 'staged', False)
            and config.max_search_queries_per_company is not None
            and counts['search_calls'] >= config.max_search_queries_per_company)
        if staged_budget_exhausted:
            diagnostics['queries'][-1].update(status='SKIPPED', reason='Search query budget exhausted')
        elif likely and not (getattr(provider, 'staged', False) and selected):
            search(domain_query(likely))
        elif likely:
            diagnostics['queries'][-1].update(status='SKIPPED', reason='Verified official website; no additional Serper query required')
        else:
            diagnostics['queries'][-1].update(status='SKIPPED', reason='No likely eligible official domain')
        evaluate_candidates(config.max_candidates)
        if (selected and diagnostics['queries'][-1]['status'] == 'SKIPPED'
                and not getattr(provider, 'staged', False)):
            search(domain_query(selected[2]))
        owner = selected[1]['ownership'] if selected else None
        contact_error_start = len(fetcher.errors)
        if selected:
            scope = selected[2]
            queue = [u for u in (selected[1].get('contact_page'), scope) if u and in_scope(u, scope)]
            visited = set()
            while queue and len(visited) < config.max_contact_pages:
                url = queue.pop(0)
                if url in visited or not in_scope(url, scope):
                    continue
                visited.add(url)
                response = fetcher.fetch(url, allowed_site=scope)
                if response is not None and in_scope(response.url, scope):
                    soup = BeautifulSoup(response.text, 'html.parser')
                    queue.extend(u for u in internal_identity_links(response.url, soup) if u not in visited and u not in queue and in_scope(u, scope))
                checkpoint()
        contacts = resolve_contacts(company, writer.observations, owner, fetcher.responses)
        relevant_errors = fetcher.errors[contact_error_start:] if selected else fetcher.errors
        if selected:
            relevant_errors = relevant_errors + [error for url, errors in fetcher.failures.items()
                                                  if in_scope(url, selected[2]) for error in errors]
        # Missing optional pages and non-HTML responses are candidate outcomes,
        # not transport failures. Failed guesses do not invalidate a verified site.
        transport_errors = [e for e in relevant_errors if not e.startswith(('HTTP 404 ', 'HTTP 410 ', 'HTTP 200 or non-HTML:', 'Redirect outside accepted site:'))]
        has_errors = bool(transport_errors) or any(q['status'] == 'FAILED' for q in diagnostics['queries'])
        # Commit useful assessments as PARTIAL; resume must retry incomplete work.
        diagnostics['completeness'] = {'complete': not has_errors, 'retryable': has_errors, 'errors': transport_errors}
        diagnostics['error'] = 'Discovery incomplete; partial evidence retained' if has_errors else None
        website_status = 'VERIFIED' if selected else ('REVIEW' if has_errors or any(c['status'] in ('REVIEW', 'GROUP_REVIEW') for c in diagnostics['website_candidates']) else 'NOT_FOUND')
        website_evidence = []
        if selected:
            website_evidence = [selected[0]['evidence_id']] + list(dict.fromkeys(
                evidence_id for (url, _), evidence_id in writer.pages.items() if in_scope(url, selected[2])))
        result = dict(website_status=website_status, official_website=selected[2] if selected else None,
            verified_scope=selected[2] if selected else None, website_observation_id=selected[0]['observation_id'] if selected else None,
            website_evidence_ids_json=encode(website_evidence),
            website_assessment_json=encode({'candidates': diagnostics['website_candidates'], 'verifier_version': VERIFIER_VERSION}),
            default_email_contact_id=select_default(contacts, 'EMAIL'), default_phone_contact_id=select_default(contacts, 'PHONE'),
            contact_outcome='INCOMPLETE' if has_errors else 'FOUND' if any(c['attribution_status'] == 'ATTRIBUTED' for c in contacts) else 'NO_ATTRIBUTED_CONTACT')
        checkpoint()
        store.complete(context, contacts, result)
        completed = True
    finally:
        try:
            if not completed:
                checkpoint()
        finally:
            fetcher.close()


def run(store, run_id, *, max_items=None, batch_size=100, provider=None,
        fetcher_factory=new_fetcher, evaluator=evaluate_website):
    if not 1 <= batch_size <= 200 or (max_items is not None and max_items < 1):
        raise ValueError('batch_size must be 1..200; max_items must be positive')
    with store.exclusive_runner():
        _, config = store.validate_run(run_id)
        provider = provider or default_provider()
        with store.conn:
            store.conn.execute("UPDATE discovery_attempts SET status='INTERRUPTED',finished_at=? WHERE run_id=? AND status='RUNNING'", (now(), run_id))
            store.conn.execute("UPDATE discovery_run_companies SET status='PENDING' WHERE run_id=? AND status='RUNNING'", (run_id,))
            store.conn.execute("UPDATE discovery_runs SET status='RUNNING',started_at=COALESCE(started_at,?),finished_at=NULL WHERE run_id=?", (now(), run_id))
        processed, failures, partial, last_position = 0, 0, 0, -1
        try:
            while max_items is None or processed < max_items:
                limit = batch_size if max_items is None else min(batch_size, max_items - processed)
                rows = store.conn.execute("""SELECT * FROM discovery_run_companies
                    WHERE run_id=? AND status IN ('PENDING','FAILED','PARTIAL') AND manifest_position>?
                    ORDER BY manifest_position LIMIT ?""", (run_id, last_position, limit)).fetchall()
                if not rows:
                    break
                for row in rows:
                    last_position = row['manifest_position']
                    company = json.loads(row['identity_snapshot_json'])
                    attempt_id = store.start_attempt(run_id, row['company_id'])
                    context = dict(run_id=run_id, company_id=row['company_id'], attempt_id=attempt_id)
                    try:
                        discover(store, context, company, config, provider, fetcher_factory, evaluator)
                        partial += store.conn.execute('SELECT status FROM discovery_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()[0] == 'PARTIAL'
                    except BaseException as exc:
                        with store.conn:
                            data = json.loads(store.conn.execute('SELECT diagnostics_json FROM discovery_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()[0])
                            data['error'] = f'{type(exc).__name__}: {exc}'
                            status = 'FAILED' if isinstance(exc, Exception) else 'INTERRUPTED'
                            store.conn.execute('UPDATE discovery_attempts SET status=?,finished_at=?,diagnostics_json=? WHERE attempt_id=?',
                                               (status, now(), encode(data), attempt_id))
                            store.conn.execute("UPDATE discovery_run_companies SET status='FAILED' WHERE run_id=? AND company_id=?", (run_id, row['company_id']))
                        failures += 1
                        if not isinstance(exc, Exception):
                            raise
                    processed += 1
        finally:
            remaining = store.conn.execute("""SELECT COUNT(*) FROM discovery_run_companies
                WHERE run_id=? AND status NOT IN ('COMPLETED','INELIGIBLE')""", (run_id,)).fetchone()[0]
            with store.conn:
                store.conn.execute('UPDATE discovery_runs SET status=?,finished_at=? WHERE run_id=?',
                                   ('PARTIAL' if remaining else 'COMPLETED', None if remaining else now(), run_id))
        return {'run_id': run_id, 'processed': processed, 'failed': failures, 'partial': partial, 'remaining': remaining,
                'status': 'PARTIAL' if remaining else 'COMPLETED'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='Freeze an explicit company manifest; no network')
    create.add_argument('--source', required=True)
    create.add_argument('--namespace', required=True)
    create.add_argument('--results', required=True)
    create.add_argument('--ids', required=True, help='Comma-separated explicit source company IDs')
    create.add_argument('--job-type', choices=[JOB_TYPE], default=JOB_TYPE)
    create.add_argument('--execution-mode', choices=[EXECUTION_MODE], default=EXECUTION_MODE)
    create.add_argument('--use-municipality', action='store_true')
    create.add_argument('--max-search-queries-per-company', type=int)
    for name in ('run', 'resume'):
        command = commands.add_parser(name, help='Execute pending/failed companies in this run only (network)')
        command.add_argument('--results', required=True)
        command.add_argument('--run-id', required=True)
        command.add_argument('--max-items', type=int)
        command.add_argument('--batch-size', type=int, default=100)
    args = parser.parse_args(argv)
    store = None
    try:
        if args.command == 'create':
            descriptor, companies = read_manifest(args.source, [int(i) for i in args.ids.split(',')], args.namespace)
            store = Store(args.results, source=args.source, create=True)
            config = Config(use_municipality=args.use_municipality,
                max_search_queries_per_company=args.max_search_queries_per_company)
            result = {'run_id': store.create_run(descriptor, companies, config, args.job_type, args.execution_mode)}
        else:
            store = Store(args.results)
            result = run(store, args.run_id, max_items=args.max_items, batch_size=args.batch_size)
        print(encode(result))
        return 1 if result.get('failed') or result.get('partial') else 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f'error: {exc}\n')
    finally:
        if store:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
