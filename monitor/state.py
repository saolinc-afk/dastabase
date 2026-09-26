"""Bounded in-memory caches and projections for the read-only operations console."""
import copy
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from monitor.metrics import bounded_read, database_metrics, parse_report


def stamp():
    return datetime.now(timezone.utc).isoformat()


class SnapshotCache:
    """One collector call per interval per app process, including concurrent clients."""
    def __init__(self, seconds):
        self.seconds = seconds
        self.at = float('-inf')
        self.value = None
        self.lock = threading.Lock()

    def get(self, collect):
        with self.lock:
            if time.monotonic() - self.at >= self.seconds:
                started = time.monotonic()
                value = collect()
                self.value = value
                self.at = started
            return self.value


def fingerprint(path):
    info = path.stat()
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class DatabaseCache:
    def __init__(self):
        self.signature = None
        self.value = None
        self.at = float('-inf')
        self.attribution = {}

    def collect(self, path):
        path = Path(path)
        def signature():
            values = []
            for file in (path, Path(str(path)+'-wal')):
                try:
                    values.append(fingerprint(file))
                except OSError:
                    values.append(None)
            return tuple(values)
        before = signature()
        if self.value is not None and before == self.signature and time.monotonic()-self.at < 30:
            return self.value
        value = database_metrics(path, self.attribution)
        value['sampled_at'] = stamp()
        self.value = value
        # Retry partial/failed reads and writes racing a snapshot on the next status tick.
        self.signature = before if value['available'] and not value.get('note') and before == signature() else None
        self.at = time.monotonic()
        return value


def report_fields(value):
    # Discard bulky HTTP/ownership evidence during parsing, before it accumulates
    # across all companies. Retain only the fields needed by parse_report.
    allowed = {'selected_ids', 'companies', 'rule_version', 'complete', 'interrupted',
               'circuit_breaker', 'triggered', 'started_at', 'finished_at', 'company_id',
               'company_name', 'website', 'status', 'final_url', 'email_result', 'emails',
               'email', 'evidence', 'attribution', 'attributable'}
    return {k: v for k, v in value.items() if k in allowed and not (k == 'evidence' and isinstance(v, list))}


class ReportCache:
    """Keep only projected summaries for up to 64 reports, never raw evidence blobs."""
    def __init__(self):
        self.entries = OrderedDict()

    def read(self, path):
        key = str(path)
        sig = fingerprint(path)
        cached = self.entries.get(key)
        if cached and cached[0] == sig:
            self.entries.move_to_end(key)
            if cached[2]:
                raise ValueError('Malformed or oversized report')
            return copy.deepcopy(cached[1])
        malformed = False
        try:
            summary = parse_report(json.loads(bounded_read(path), object_hook=report_fields))
        except ValueError:
            summary = None
            malformed = True
        if fingerprint(path) != sig:
            return None  # A non-atomic old writer changed the file during the read.
        self.entries[key] = (sig, summary, malformed)
        self.entries.move_to_end(key)
        while len(self.entries) > 64:
            self.entries.popitem(last=False)
        if malformed:
            raise ValueError('Malformed or oversized report')
        return copy.deepcopy(summary)


def company_lookup(path, ids):
    """Small parameterized ID lookup; no evidence, email addresses or full-table scans."""
    ids = sorted({i for i in ids if type(i) is int})
    if not ids:
        return {}
    conn = None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True,
                               isolation_level=None, timeout=0.2)
        conn.execute('PRAGMA query_only=ON')
        deadline = time.monotonic()+1
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        result = {}
        for offset in range(0, len(ids), 400):
            batch = ids[offset:offset+400]
            placeholders = ','.join('?' for _ in batch)
            rows = conn.execute(f'''SELECT c.id, c.company_name,
                EXISTS(SELECT 1 FROM website_discovery w WHERE w.company_id=c.id)
                FROM companies_lite c WHERE c.id IN ({placeholders})''', batch)
            result.update({id_: {'name': name, 'processed': bool(processed)} for id_, name, processed in rows})
        return result
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()


def workload(database, active, companies):
    jobs = active['jobs']
    visible = active.get('available', False) and not active.get('inaccessible_processes')
    result = dict(kind='observed_workload', persistent_queue=False,
                  state='active' if jobs else 'idle' if visible else 'unknown',
                  active_batches=len(jobs), batch_remaining=None,
                  database_remaining=database.get('remaining'), unprocessed_in_active_batches=None,
                  remaining_after_batch=None, estimate=True)
    if not jobs:
        if visible:
            result.update(batch_remaining=0, unprocessed_in_active_batches=0,
                          remaining_after_batch=database.get('remaining'))
        return result
    if not visible or companies is None or any('selected_ids' not in j or 'completed_ids' not in j for j in jobs):
        return result
    pending = set()
    for job in jobs:
        pending.update(set(job['selected_ids'])-set(job['completed_ids']))
    result['batch_remaining'] = sum(j['remaining'] for j in jobs)
    if not pending <= companies.keys():
        return result
    uncovered = sum(not companies[id_]['processed'] for id_ in pending)
    result['unprocessed_in_active_batches'] = uncovered
    if result['database_remaining'] is not None:
        result['remaining_after_batch'] = max(0, result['database_remaining']-uncovered)
    return result


def operation_details(database, active, recent, db_path):
    jobs = active['jobs']
    # Prefer active reports; while idle, use the most recently modified runner report.
    sources = [j for j in jobs if j.get('report') and 'activity' in j]
    if not sources:
        sources = recent['jobs'][:1]
    activity = []
    # Interleave workers without claiming a global chronology that reports cannot prove.
    for index in range(10):
        for source in sources:
            events = source.get('activity', [])
            if index < len(events):
                activity.append({**events[index], 'report': source.get('report') or source.get('name')})
            if len(activity) == 10:
                break
        if len(activity) == 10:
            break
    ids = {e['company_id'] for e in activity}
    for job in jobs:
        ids.update(set(job.get('selected_ids', []))-set(job.get('completed_ids', [])))
        if job.get('current_company'):
            ids.add(job['current_company']['id'])
        if job.get('last_completed'):
            ids.add(job['last_completed']['company_id'])
    companies = company_lookup(db_path, ids)
    for job in jobs:
        current = job.get('current_company')
        if current and not current.get('name') and companies:
            current['name'] = (companies.get(current['id']) or {}).get('name')
        last = job.get('last_completed')
        if last and not last.get('company_name') and companies:
            last['company_name'] = (companies.get(last['company_id']) or {}).get('name')
    for event in activity:
        if not event.get('company_name') and companies:
            name = (companies.get(event['company_id']) or {}).get('name')
            if name:
                event['company_name'] = name
    return workload(database, active, companies), dict(
        entries=activity, source='active' if any('activity' in j for j in jobs) else 'recent',
        note='Checkpoint order per report; completion times are not recorded. Email counts reflect attribution at processing time.')


def public_jobs(active, recent):
    hidden = {'selected_ids', 'completed_ids', 'activity'}
    return ({**active, 'jobs': [{k: v for k, v in j.items() if k not in hidden} for j in active['jobs']]},
            {**recent, 'jobs': [{k: v for k, v in j.items() if k not in hidden | {'last_completed'}} for j in recent['jobs']]})
