"""Read-only collectors. Never import database initialization or runner code."""
import json
import hashlib
from collections import defaultdict
from urllib.parse import urlsplit
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from discovery.ownership import email_attribution

ROOT = Path(__file__).resolve().parents[1]
STATUSES = ('VERIFIED', 'REVIEW', 'GROUP_REVIEW', 'NOT_FOUND', 'ERROR')
# Deliberately matches the existing outreach export's accepted rule version.
EMAIL_RULE_VERSION = 'phase2-ownership-1'
COMPANIES_SQL = '''SELECT c.*, w.id AS website_row_id, w.website,
    w.status AS website_status, w.rule_version, w.evidence_json, w.ownership_json
    FROM companies_lite c LEFT JOIN website_discovery w ON w.id=(
      SELECT id FROM website_discovery WHERE company_id=c.id ORDER BY id DESC LIMIT 1)'''
COUNTS_SQL = '''SELECT w.status, COUNT(*) FROM companies_lite c
    JOIN website_discovery w ON w.id=(SELECT id FROM website_discovery
      WHERE company_id=c.id ORDER BY id DESC LIMIT 1) GROUP BY w.status'''
EMAILS_SQL = 'SELECT * FROM email_discovery'


def obj(raw, kind):
    try:
        value = json.loads(raw)
        return value if isinstance(value, kind) else kind()
    except (ValueError, TypeError):
        return kind()


def database_metrics(path, attribution_cache=None):
    result = dict(available=False, total=None, phase1_complete=None,
                  phase1_note='No explicit Phase 1 completion flag in the schema.',
                  processed=None, remaining=None, percent=None,
                  statuses={s: None for s in STATUSES}, email_companies=None, emails=None)
    conn = None
    try:
        # No immutable=1: production writers must remain visible. No migrations.
        conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + 2
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        total = conn.execute('SELECT COUNT(*) FROM companies_lite').fetchone()[0]
        result.update(available=True, total=total)
        counts = dict(conn.execute(COUNTS_SQL).fetchall())
        processed = sum(counts.values())
        result.update(processed=processed, remaining=max(0, total-processed),
                      percent=round(100*processed/total, 1) if total else 0,
                      statuses={s: counts.get(s, 0) for s in STATUSES},
                      other_statuses=sum(v for k, v in counts.items() if k not in STATUSES))
        try:
            companies = {r['id']: dict(r) for r in conn.execute(COMPANIES_SQL)}
            emails = [dict(r) for r in conn.execute(EMAILS_SQL)]
        except sqlite3.Error:
            result['email_note'] = 'Email attribution unavailable with this schema.'
            return result
    except sqlite3.Error:
        result['note'] = 'Database unavailable, busy, or required schema missing.'
        return result
    finally:
        if conn is not None:
            conn.close()
    # All attribution evaluation happens after the SQLite connection is closed.
    grouped = defaultdict(list)
    for mail in emails:
        grouped[mail['company_id']].append(mail)
    memo = attribution_cache if attribution_cache is not None else {}
    accepted_count = company_count = 0
    malformed = False
    for company_id, mails in grouped.items():
        company = companies.get(company_id)
        signature = hashlib.sha256(repr((company, mails)).encode()).digest()
        previous = memo.get(company_id)
        if previous and previous[0] == signature:
            count, invalid = previous[1:]
        else:
            accepted = set()
            invalid = False
            if company and company['website_status'] == 'VERIFIED' and company['rule_version'] == EMAIL_RULE_VERSION:
                ownership = obj(company['ownership_json'], dict)
                pages = obj(company['evidence_json'], list)
                for mail in mails:
                    evidence = obj(mail.get('evidence_json'), dict)
                    try:
                        attribution = email_attribution(company, mail['email'], mail.get('page_url') or '',
                            evidence.get('publication', ''), ownership, pages, mail.get('page_title') or '',
                            evidence.get('contact_block'), evidence.get('visible_email', ''))
                        if attribution.get('attributable'):
                            accepted.add(mail['email'])
                    except (TypeError, ValueError, KeyError, AttributeError):
                        invalid = True
            count = len(accepted)
            memo[company_id] = (signature, count, invalid)
        accepted_count += count
        company_count += bool(count)
        malformed |= invalid
    for company_id in set(memo) - set(grouped):
        del memo[company_id]
    result.update(emails=accepted_count, email_companies=company_count)
    if malformed:
        result['email_note'] = 'Malformed provenance excluded; counts may be incomplete.'
    return result


def safe_file(path, logs):
    """Only regular files within the configured logs tree are eligible."""
    try:
        path = Path(path).resolve()
        if path.is_relative_to(logs.resolve()) and stat.S_ISREG(path.stat().st_mode):
            return path
    except (OSError, ValueError):
        pass
    return None


def bounded_read(path, limit=32_000_000, tail=False):
    with path.open('rb') as handle:
        size = os.fstat(handle.fileno()).st_size
        if size > limit and not tail:
            raise ValueError('oversize')
        if tail:
            handle.seek(max(0, size-limit))
        return handle.read(limit).decode('utf-8', errors='replace')


def parse_report(data):
    """Accept runner checkpoints, not arbitrary repair/summary JSON files."""
    if not isinstance(data, dict) or not isinstance(data.get('selected_ids'), list) or not isinstance(data.get('companies'), list):
        return None
    selected = data['selected_ids']
    entries = data['companies']
    if any(type(i) is not int for i in selected) or any(not isinstance(e, dict) or type(e.get('company_id')) is not int for e in entries):
        return None
    breaker = data.get('circuit_breaker')
    triggered = isinstance(breaker, dict) and breaker.get('triggered') is True
    state = 'circuit-breaker' if triggered else 'complete' if data.get('complete') is True else 'interrupted' if data.get('interrupted') is True else 'incomplete'
    timestamp = data.get('finished_at') or data.get('started_at')
    try:
        timestamp = datetime.fromisoformat(timestamp).isoformat()
    except (TypeError, ValueError):
        timestamp = None
    selected_ids = list(dict.fromkeys(selected))
    completed_ids = list(dict.fromkeys(e['company_id'] for e in entries))
    if len(selected_ids) != len(selected) or len(completed_ids) != len(entries) or not set(completed_ids) <= set(selected_ids):
        return None
    remaining = len(selected_ids) - len(completed_ids)
    activity = [activity_entry(e, data.get('rule_version')) for e in entries[-10:]][::-1]
    return dict(processed=len(entries), selected=len(selected), remaining=remaining,
                percent=round(100*len(entries)/len(selected), 1) if selected else 0,
                status=state, circuit_breaker=triggered if isinstance(breaker, dict) and type(breaker.get('triggered')) is bool else None,
                timestamp=timestamp, selected_ids=selected_ids, completed_ids=completed_ids,
                activity=activity, last_completed=activity[0] if activity else None)


def activity_entry(entry, rule_version):
    """Project only established runner fields; never expose arbitrary report content."""
    result = {'company_id': entry['company_id']}
    if isinstance(entry.get('company_name'), str) and entry['company_name'].strip():
        result['company_name'] = entry['company_name'][:200]
    website = entry.get('website')
    if isinstance(website, dict):
        status = website.get('status')
        if status in STATUSES or status == 'FOUND':
            result['status'] = status
        # Only a verified domain is described as official. Never return URL credentials/query.
        if status == 'VERIFIED':
            try:
                url = urlsplit(website.get('final_url') or '')
                if url.scheme in ('http', 'https') and url.hostname:
                    result['domain'] = url.hostname
            except (ValueError, TypeError):
                pass
    email_result = entry.get('email_result')
    if rule_version == EMAIL_RULE_VERSION and isinstance(email_result, dict):
        mails = email_result.get('emails')
        if isinstance(mails, list):
            proven = []
            for mail in mails:
                proof = mail.get('evidence') if isinstance(mail, dict) else None
                attribution = proof.get('attribution') if isinstance(proof, dict) else None
                if not isinstance(attribution, dict) or attribution.get('attributable') is not True or not isinstance(mail.get('email'), str):
                    break
                proven.append(mail['email'])
            else:
                result['usable_emails'] = len(set(proven))
    # The runner has no per-entry timestamp. Do not manufacture one from mtime.
    return result


def runner_args(argv):
    """Whitelist Python module and numeric/path flags; never return raw argv."""
    if not argv or not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(argv[0]).name):
        return None
    try:
        index = argv.index('-m')
        if any(arg in ('-c', '--') or not arg.startswith('-') for arg in argv[1:index]):
            return None
        module = argv[index+1]
    except (ValueError, IndexError):
        return None
    if module not in ('discovery.runner', 'worker.runner'):
        return None
    options = {}
    for i, arg in enumerate(argv[index+2:], index+2):
        key, sep, value = arg.partition('=')
        if key in ('--report', '--resume-report', '--limit'):
            options[key] = value if sep else (argv[i+1] if i+1 < len(argv) else '')
    limit = options.get('--limit', '20' if module == 'discovery.runner' else '')
    return dict(module=module, limit=int(limit) if limit.isdecimal() else None,
                report=options.get('--resume-report') or options.get('--report'))


def log_progress(path):
    text = bounded_read(path, 65536, tail=True)
    starts = list(re.finditer(r'^\d+ companies selected .*?Report: (.+)$', text, re.M))
    if starts:
        text = text[starts[-1].start():]
    matches = list(re.finditer(r'^\[(\d+)/(\d+)\] ID (\d+)(?: ([^\r\n]*))?$', text, re.M))
    result = {'circuit_breaker': 'Search circuit breaker stopped batch:' in text}
    if matches:
        match = matches[-1]
        # A following result line means this company is no longer known to be active.
        if not re.search(r'^  .*; emails=', text[match.end():], re.M) and not result['circuit_breaker']:
            result['current_company'] = {'id': int(match[3]), 'name': (match[4] or '')[:200]}
    reports = re.findall(r'^\d+ companies selected .*?Report: (.+)$', text, re.M)
    if reports:
        result['report_path'] = reports[-1]
    return result


def active_jobs(logs, proc=Path('/proc'), report_reader=None, details=True):
    jobs = []
    available = (proc / 'uptime').exists()
    if not available:
        return {'available': False, 'jobs': jobs, 'note': 'Linux /proc process inspection unavailable.'}
    inaccessible = 0
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            argv = (entry / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            parsed = runner_args(argv)
            if not parsed:
                continue
            cwd = (entry / 'cwd').resolve(strict=True)
            # Avoid matching another checkout with the same module name.
            if cwd != ROOT:
                continue
            job = dict(pid=int(entry.name), module=parsed['module'], limit=parsed['limit'],
                       report=None, selected=None, processed=None, runtime_seconds=None,
                       current_company=None, circuit_breaker=None)
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            uptime = float((proc / 'uptime').read_text().split()[0])
            job['runtime_seconds'] = max(0, round(uptime - int(fields[19])/os.sysconf('SC_CLK_TCK')))
            job['identity'] = f"{entry.name}:{fields[19]}"
            job['job_type'] = 'WEBSITE + EMAIL DISCOVERY' if parsed['module'] == 'discovery.runner' else 'DASTABASE WORKER'
            if not details:
                jobs.append(job)
                continue
            progress = {}
            for fd in ('1', '2'):
                path = safe_file(entry / 'fd' / fd, logs)
                if path and path.stat().st_mtime >= time.time()-job['runtime_seconds']-2:
                    candidate = log_progress(path)
                    if candidate.get('current_company') or candidate.get('report_path') or candidate.get('circuit_breaker'):
                        progress = candidate
                        break
            report_arg = parsed['report'] or progress.get('report_path')
            report = safe_file(cwd / report_arg, logs) if report_arg else None
            if parsed['report'] and progress.get('report_path'):
                logged_report = safe_file(cwd / progress['report_path'], logs)
                if logged_report != report:
                    progress = {}
            if report:
                job['report'] = str(report.relative_to(logs.resolve()))
                try:
                    summary = report_reader(report) if report_reader else parse_report(json.loads(bounded_read(report)))
                    if summary:
                        job.update({k: summary[k] for k in ('selected', 'processed', 'remaining', 'percent', 'circuit_breaker', 'last_completed', 'selected_ids', 'completed_ids', 'activity')})
                except (OSError, ValueError):
                    pass
            job['current_company'] = progress.get('current_company')
            if progress.get('circuit_breaker'):
                job['circuit_breaker'] = True
            current = job['current_company']
            if current and (job['circuit_breaker'] or current['id'] in job.get('completed_ids', []) or
                            ('selected_ids' in job and current['id'] not in job['selected_ids'])):
                job['current_company'] = None
            jobs.append(job)
        except PermissionError:
            inaccessible += 1
        except (OSError, ValueError, IndexError):
            continue  # Process exited during inspection.
    return dict(available=True, jobs=jobs, inaccessible_processes=inaccessible)


def recent_jobs(logs, active, report_reader=None):
    candidates = []
    skipped = 0
    try:
        for path in logs.rglob('*.json'):
            safe = safe_file(path, logs)
            if safe:
                try:
                    candidates.append((safe.stat().st_mtime, safe))
                except OSError:
                    pass
    except OSError:
        return dict(jobs=[], skipped=0, note='Logs unavailable.')
    running = {j['report'] for j in active['jobs'] if j['report']}
    results = []
    for mtime, path in sorted(candidates, reverse=True)[:500]:
        try:
            summary = report_reader(path) if report_reader else parse_report(json.loads(bounded_read(path)))
            if summary is None:
                continue
            summary = dict(summary)
            name = str(path.relative_to(logs.resolve()))
            if name in running and not summary['circuit_breaker']:
                summary['status'] = 'running'
            # A dead process alone is insufficient to distinguish killed vs old checkpoint.
            summary.update(name=name, timestamp=summary['timestamp'] or datetime.fromtimestamp(mtime, timezone.utc).isoformat())
            results.append(summary)
            if len(results) == 20:
                break
        except (OSError, ValueError):
            skipped += 1
    return dict(jobs=results, skipped=skipped)


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=2,
                                check=False, env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'LC_ALL': 'C'})
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def service_metrics():
    # Only list formatted status metadata; never inspect env/config or execute in containers.
    output = command(['docker', 'ps', '-a', '--format', '{{json .}}'])
    containers = []
    if output is not None:
        for line in output.splitlines():
            value = obj(line, dict)
            if value:
                containers.append(value)
    services = []
    for label, pattern, unit in (
        ('Minecraft / Crafty', r'crafty|minecraft|itzg', None),
        ('PostgreSQL', r'postgres|postgis', 'postgresql.service'),
        ('NocoDB', r'nocodb', None),
        ('Tailscale', r'tailscale', 'tailscaled.service')):
        matches = [c for c in containers if re.search(pattern, c.get('Names', '')+' '+c.get('Image', ''), re.I)]
        states = []
        for c in matches:
            state, status = c.get('State', ''), c.get('Status', '')
            states.append('unhealthy' if '(unhealthy)' in status else 'starting' if '(health: starting)' in status else 'running' if state == 'running' else 'stopped')
        if states:
            status = 'unhealthy' if 'unhealthy' in states else 'stopped' if 'stopped' in states else 'starting' if 'starting' in states else 'running'
            source = 'Docker name/image match (container state)'
        else:
            native = command(['systemctl', 'show', '--property=ActiveState', '--value', unit]) if unit else None
            status = {'active': 'active', 'inactive': 'stopped', 'failed': 'failed',
                      'activating': 'starting'}.get(native.strip() if native else '', 'unknown')
            source = 'systemd unit state' if status != 'unknown' else 'Not detected or inspection unavailable'
        services.append(dict(name=label, status=status, source=source))
    return services


class ServerMetrics:
    def __init__(self):
        self.previous_cpu = None

    def collect(self):
        result = dict(cpu_percent=None, ram_used=None, ram_total=None, disk_used=None,
                      disk_total=None, uptime_seconds=None, load=None, temperature_c=None)
        try:
            values = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            total, idle = sum(values), values[3]+values[4]
            if self.previous_cpu:
                dt, di = total-self.previous_cpu[0], idle-self.previous_cpu[1]
                if dt > 0:
                    result['cpu_percent'] = round(100*(dt-di)/dt, 1)
            self.previous_cpu = total, idle
        except (OSError, ValueError, IndexError):
            pass
        try:
            mem = {line.split(':')[0]: int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()}
            result.update(ram_total=mem['MemTotal'], ram_used=mem['MemTotal']-mem['MemAvailable'])
        except (OSError, ValueError, KeyError):
            pass
        try:
            disk = shutil.disk_usage('/')
            result.update(disk_used=disk.used, disk_total=disk.total)
        except OSError:
            pass
        try:
            result['uptime_seconds'] = float(Path('/proc/uptime').read_text().split()[0])
        except (OSError, ValueError, IndexError):
            pass
        try:
            result['load'] = list(os.getloadavg())
        except OSError:
            pass
        temps = []
        for path in Path('/sys/class/thermal').glob('thermal_zone*'):
            try:
                if re.search(r'cpu|x86_pkg|soc', (path/'type').read_text(), re.I):
                    temp = float((path/'temp').read_text())/1000
                    if -20 < temp < 150:
                        temps.append(temp)
            except (OSError, ValueError):
                pass
        result['temperature_c'] = max(temps) if temps else None
        return result
