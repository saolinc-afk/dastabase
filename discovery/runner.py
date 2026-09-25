"""Bounded Phase 2 runner using the existing Lite discovery functions/tables."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from database_lite import (get_connection, initialize_discovery_database, save_website, save_email)
from discovery.website_discovery import discover
from discovery.search_engine import SearchCircuitBreaker, SearchInfrastructureStopped
from discovery.website_verifier import Fetcher, RULE_VERSION
from discovery.email_discovery import crawl_site

MAX_COMPANIES = 200


def load_companies(limit=20, ids=None, retry=False, force_ids=False):
    if not 1 <= limit <= MAX_COMPANIES or (ids and len(set(ids)) > MAX_COMPANIES):
        raise ValueError(f'Phase 2 runner is capped at {MAX_COMPANIES} companies per invocation')
    conn = get_connection()
    try:
        rows = conn.execute('''SELECT c.*, w.id AS website_row_id, w.website, w.confidence,
            w.status, w.method, w.contact_page, w.evidence_json, w.email_status, w.rule_version,
            w.verified_scope, w.ownership_json
            FROM companies_lite c LEFT JOIN website_discovery w ON w.id=(
                SELECT id FROM website_discovery WHERE company_id=c.id ORDER BY id DESC LIMIT 1)
            ORDER BY c.id''').fetchall()
        selected = []
        for row in rows:
            company = dict(row)
            if ids and company['id'] not in ids: continue
            fresh = company['website_row_id'] is None
            interrupted = company['status'] == 'VERIFIED' and company['email_status'] in (None, 'PENDING')
            retryable = retry and (company['status'] in ('NOT_FOUND', 'ERROR', 'REVIEW', 'GROUP_REVIEW', 'FOUND') or company['email_status'] == 'ERROR')
            if force_ids or fresh or interrupted or retryable:
                selected.append(company)
        if ids:
            order = {id_: i for i, id_ in enumerate(ids)}
            selected.sort(key=lambda c: order[c['id']])
        return selected[:limit]
    finally:
        conn.close()


def persist_email_result(company_id, result):
    conn = get_connection()
    try:
        with conn:
            for email in result['emails']:
                save_email(conn, company_id, email['email'], email['confidence'], email['page_url'],
                           email['page_title'], email['found_in'], website=email['website'], evidence=email['evidence'])
            conn.execute('''UPDATE website_discovery SET email_status=?, checked_at=?,
                last_error=? WHERE id=(SELECT id FROM website_discovery WHERE company_id=? ORDER BY id DESC LIMIT 1)''',
                (result['status'], datetime.now(timezone.utc).isoformat(), '\n'.join(result.get('errors', [])), company_id))
    finally:
        conn.close()


def run(limit=20, ids=None, retry=False, report_path=None, resume_report=None,
        search_failure_threshold=10, force_ids=False):
    if not 1 <= limit <= MAX_COMPANIES or (ids and len(set(ids)) > MAX_COMPANIES):
        raise ValueError(f'Maximum {MAX_COMPANIES} companies')
    breaker = SearchCircuitBreaker(search_failure_threshold)
    initialize_discovery_database()
    existing_report = None
    if resume_report:
        report_file = Path(resume_report)
        existing_report = json.loads(report_file.read_text())
        done = {entry['company_id'] for entry in existing_report.get('companies', [])}
        ids = [company_id for company_id in existing_report.get('selected_ids', []) if company_id not in done]
        limit = len(ids)
        report_path = report_file
        if not ids:
            existing_report['complete'] = True
            report_file.write_text(json.dumps(existing_report, ensure_ascii=False, indent=2))
            return existing_report
    companies = load_companies(limit, ids, retry, force_ids=force_ids or bool(resume_report))
    report_path = Path(report_path or Path(__file__).resolve().parent.parent / 'logs' /
                       ('phase2_pilot_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.json'))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = existing_report or {'started_at': datetime.now(timezone.utc).isoformat(), 'rule_version': RULE_VERSION,
              'selected_ids': [c['id'] for c in companies], 'companies': [], 'complete': False,
              'http_requests': 0, 'search_calls': 0,
              'request_count_note': 'HTTP counts include redirects; DDGS calls counted, provider internal requests not measurable.'}

    def checkpoint():
        temp = report_path.with_suffix('.tmp')
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        temp.replace(report_path)

    report['circuit_breaker'] = {'triggered': False, 'threshold': breaker.threshold}
    report['complete'] = False
    checkpoint()
    print(f'{len(companies)} companies selected (hard limit {MAX_COMPANIES}). Report: {report_path}', flush=True)
    try:
        for index, company in enumerate(companies, 1):
            print(f"[{index}/{len(companies)}] ID {company['id']} {company['company_name']}", flush=True)
            fetcher = Fetcher()
            stats = {'search_calls': 0}
            entry = {'company_id': company['id'], 'company_name': company['company_name'],
                     'tax_number': company['tax_number'], 'registration_number': company['registration_number']}
            website = None
            try:
                if company['status'] == 'VERIFIED' and company['rule_version'] == RULE_VERSION:
                    website = {'verified': True, 'status': 'VERIFIED', 'final_url': company['website'],
                               'confidence': company['confidence'], 'method': company['method'],
                               'contact_page': company['contact_page'], 'evidence': json.loads(company['evidence_json'] or '[]'),
                               'verified_scope': company['verified_scope'], 'ownership': json.loads(company['ownership_json'] or '{}')}
                else:
                    website = discover(company, fetcher, stats, breaker=breaker)
                    save_website({**website, 'company_id': company['id']})
                entry['website'] = website
                if website['status'] == 'VERIFIED':
                    emails = crawl_site({**company, 'status': 'VERIFIED', 'website': website['final_url'],
                                         'contact_page': website['contact_page'], 'ownership': website.get('ownership',{}),
                                         'verified_scope': website.get('verified_scope','')}, fetcher)
                    persist_email_result(company['id'], emails)
                    entry['email_result'] = emails
                else:
                    entry['email_result'] = {'status': 'SKIPPED', 'emails': [], 'reason': 'Website not VERIFIED'}
            except SearchInfrastructureStopped as exc:
                report['circuit_breaker'].update(
                    triggered=True, company_id=company['id'], reason=str(exc),
                    consecutive_failures=breaker.consecutive_failures)
                # Keep the interrupted company outside completed entries and DB writes.
                report.setdefault('interrupted_attempts', []).append({
                    **entry, 'http_requests': fetcher.requests,
                    'search_calls': stats['search_calls'], 'reason': str(exc)})
                report['http_requests'] += fetcher.requests
                report['search_calls'] += stats['search_calls']
                print(f'Search circuit breaker stopped batch: {exc}', flush=True)
                break
            except Exception as exc:
                entry['error'] = f'{type(exc).__name__}: {exc}'
                if website and website['status'] == 'VERIFIED':
                    persist_email_result(company['id'], {'status': 'ERROR', 'emails': [], 'errors': [entry['error']]})
                else:
                    website = {'company_id': company['id'], 'status': 'ERROR', 'errors': [entry['error']]}
                    save_website(website)
                    entry['website'] = website
                entry['email_result'] = {'status': 'ERROR', 'emails': []}
            finally:
                entry['http_requests'] = fetcher.requests
                entry['search_calls'] = stats['search_calls']
                entry['fetch_errors'] = fetcher.errors
                entry['access_blocked'] = fetcher.blocked
                fetcher.close()
            report['companies'].append(entry)
            report['http_requests'] += entry['http_requests']
            report['search_calls'] += entry['search_calls']
            checkpoint()
            print(f"  {entry['website'].get('status')} {entry['website'].get('final_url', '')} "
                  f"confidence={entry['website'].get('confidence', 0)}; "
                  f"emails={len(entry['email_result']['emails'])} ({entry['email_result']['status']}); "
                  f"HTTP={entry['http_requests']} searches={entry['search_calls']}", flush=True)
        report['complete'] = not report['circuit_breaker']['triggered']
        statuses = {}
        for entry in report['companies']:
            status = entry.get('website', {}).get('status', 'ERROR')
            statuses[status] = statuses.get(status, 0) + 1
        report['summary'] = {'processed': len(report['companies']), 'website_statuses': statuses,
                             'companies_with_emails': sum(bool(e.get('email_result', {}).get('emails')) for e in report['companies'])}
    except KeyboardInterrupt:
        report['interrupted'] = True
        print('Interrupted; committed companies retained. Rerun the same IDs to resume.', flush=True)
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        checkpoint()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--ids', type=int, nargs='+')
    parser.add_argument('--force-ids', action='store_true',
                        help='Process every explicit --ids member; intended for a fixed audited manifest')
    parser.add_argument('--retry', action='store_true', help='Retry NOT_FOUND/ERROR/REVIEW/legacy FOUND or email ERROR')
    parser.add_argument('--report')
    parser.add_argument('--search-failure-threshold', type=int, default=10)
    parser.add_argument('--resume-report', help='Resume only IDs not checkpointed in a prior report')
    args = parser.parse_args()
    if not 1 <= args.limit <= MAX_COMPANIES or (args.ids and len(set(args.ids)) > MAX_COMPANIES):
        parser.error(f'This runner permits at most {MAX_COMPANIES} companies; --limit must be 1..{MAX_COMPANIES}')
    if args.search_failure_threshold < 1:
        parser.error('--search-failure-threshold must be positive')
    report = run(args.limit, args.ids, args.retry, args.report, args.resume_report,
                 args.search_failure_threshold, args.force_ids)
    if report.get('circuit_breaker', {}).get('triggered'):
        raise SystemExit(2)  # Also stop parent scripts that invoke bounded chunks.


if __name__ == '__main__':
    main()
