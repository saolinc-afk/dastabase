# Dastabase Monitor

Read-only operational dashboard for Duke. No deployment, worker controls, schema
changes, database initialization, or crawler changes are included.

## Local smoke test

From the repository root (Python 3.10+):

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m monitor.app
```

Open http://127.0.0.1:8765 or run `curl http://127.0.0.1:8765/api/status`.
With the virtual environment activated, `python -m monitor.app` is equivalent.
Default bind: **127.0.0.1:8765**, debug/reloader disabled. Stop with Ctrl-C.
Only Flask is added as a direct dependency (`Flask>=3.1,<4`). Its standard
transitive dependencies are Werkzeug, Jinja2, MarkupSafe, Click, ItsDangerous,
and Blinker. Existing discovery dependencies supply the pure attribution helper.

Configuration is through server-side environment variables, never request inputs:

| Variable | Default |
| --- | --- |
| `MONITOR_HOST` | `127.0.0.1` |
| `MONITOR_PORT` | `8765` |
| `MONITOR_DB` | repository `database/dastabase_lite.db` |
| `MONITOR_LOGS` | repository `logs/` |

For later private access, set `MONITOR_HOST` to Duke's specific Tailscale/private
interface address. There is no authentication in v1; keep access on trusted
private networking. The included Flask server is for local review; choose a WSGI
service/reverse proxy separately before deployment. No deployment is performed.

## API and display

`GET /` serves the page; `GET /api/status` returns `timestamp`, `database`,
`active`, `recent`, `server`, and `services`. Missing metrics are JSON null and
render as N/A. Service state `unknown` means not detected or inaccessible, not an
error. The browser polls 10 seconds after each completed request without reloading;
one process-wide snapshot is cached for 10 seconds. Failed refreshes retain the
previous display with a stale-data warning. Snapshots are approximate observations,
not an atomic transaction spanning the database, processes and reports.

## Exact database queries and semantics

The connection is opened with `sqlite3.connect(path.as_uri() + '?mode=ro',
uri=True, timeout=0.2, isolation_level=None)` and `PRAGMA query_only=ON`.
No `immutable=1` is used, because the live database changes. Statements have a
shared two-second SQLite progress-handler deadline. Connections are closed before
email attribution runs; there are no explicit or long-lived read transactions.
SQLite may use its normal WAL coordination files if the deployment uses WAL;
the monitor issues no data/schema writes and never changes journal mode.

**Total companies:**

```sql
SELECT COUNT(*) FROM companies_lite;
```

**Phase 1 complete:** N/A. There is no explicit per-company completion marker.
`collected_at` and `financial_status` are not treated as completion flags.

**Latest website status counts:**

```sql
SELECT w.status, COUNT(*)
FROM companies_lite c
JOIN website_discovery w ON w.id=(
  SELECT id FROM website_discovery
  WHERE company_id=c.id ORDER BY id DESC LIMIT 1
)
GROUP BY w.status;
```

This matches `discovery.runner.load_companies`: only the newest discovery row per
company counts. Orphan website rows are excluded. VERIFIED, REVIEW, GROUP_REVIEW,
NOT_FOUND and ERROR are displayed separately; any legacy/other states are counted
in an additional bucket. **Phase 2 processed** is the sum of all these counts:
companies with a discovery row, not necessarily successful or fully email-crawled.
Retries do not increase this count. **Remaining** is `max(0, total - processed)`.
**Percentage** is `round(100 * processed / total, 1)`, or zero for an empty database.
This is coverage, not the runner's retry/resume eligibility or a success rate.

**Email inputs:**

```sql
SELECT c.*, w.id AS website_row_id, w.website,
       w.status AS website_status, w.rule_version, w.evidence_json, w.ownership_json
FROM companies_lite c
LEFT JOIN website_discovery w ON w.id=(
  SELECT id FROM website_discovery
  WHERE company_id=c.id ORDER BY id DESC LIMIT 1
);

SELECT * FROM email_discovery;
```

The read rows stay server-side. Like `scripts/build_outreach_export.py`, an email
is eligible only when the current website is VERIFIED and its `rule_version` is
`phase2-ownership-1`. The existing pure `discovery.ownership.email_attribution`
function re-evaluates the company, email, page URL, publication, website ownership,
website evidence pages, page title, contact block and visible email. Only
`attributable=True` survives. No HTTP requests are made. Companies with usable
emails are distinct company IDs among accepted `(company_id, email)` pairs; total
usable addresses counts those pairs (the same address at different companies
counts separately). This does not assert mailbox deliverability. Malformed proof
is excluded with a warning; absent required columns/tables yields N/A for email
counts. The rule version deliberately matches the existing export and should be
reviewed if export semantics change.

## Active processes and recent batches

Linux `/proc/[pid]/cmdline` identifies Python `-m discovery.runner` and
`-m worker.runner` processes with this repository as their actual working
directory. No PID file is trusted. Other checkouts, wrappers, script invocations,
container PID namespaces, or inaccessible processes may not be visible. Run as a
user able to read the worker processes. No sudo or permission changes are made.
Only module, PID, numeric limit, and a safe report reference are exposed. Full argv,
other flags, environment variables and raw log lines are never returned. Runtime
uses `/proc/uptime` and field 22 of `/proc/[pid]/stat`.

`--resume-report` takes precedence over `--report`. Reports must resolve to regular
files inside `MONITOR_LOGS`; paths displayed are relative to that directory.
`selected` is the length of the report's `selected_ids`, not the number of IDs
requested on argv. `processed` is the count of checkpointed `companies` entries.
The CLI limit can differ from actual selected count after filtering or resume.
Without a report, these values are N/A, not inferred from a database delta.

For a worker's actual stdout/stderr regular file beneath logs, the last 64 KiB is
read. A runner `Report:` startup line can identify its default report. The latest
`[i/n] ID id name` line supplies a current company only while there is no following
result or breaker line. Pipes and arbitrary files are not read. A truncated startup
line or unavailable output leaves report/current company unknown. The checkpoint's
breaker flag or the runner's exact breaker log marker establishes breaker status.
An uncheckpointed log line is only a last-observed activity, not proof of liveness.

Recent batches scan nested `*.json` files under logs, considering the newest 500
files and displaying up to 20 valid runner reports. Files larger than 32 MB and
malformed/unreadable JSON are skipped and counted. Repair summaries and unrelated
JSON without `selected_ids` and `companies` arrays are ignored. Timestamps prefer
`finished_at`, then `started_at`, then file modification time. States are:

- `running`: matched to a live process via its exact report path.
- `circuit-breaker`: the report explicitly says triggered (takes precedence).
- `complete`: explicit `complete: true` with no active writer observed.
- `interrupted`: explicit `interrupted: true`.
- `incomplete`: no definitive completion/interruption evidence. A missing process
  alone does not prove an interrupted run; it could be hidden by permissions.

## Server and services

CPU utilization uses differences in `/proc/stat` counters (guest time excluded,
iowait counted as idle); the first snapshot is N/A. RAM is MemTotal minus
MemAvailable from `/proc/meminfo`. Root disk uses `shutil.disk_usage('/')`, uptime
uses `/proc/uptime`, and load uses `os.getloadavg()`. CPU temperature is the maximum
plausible reading in CPU/x86_pkg/SoC-labelled `/sys/class/thermal` zones. Missing
Linux metrics are N/A, including on macOS development machines.

Docker uses only `docker ps -a --format '{{json .}}'`. Container names/images are
matched for crafty/minecraft/itzg, postgres/postgis, nocodb, and tailscale. Only
aggregate status is returned; no image, environment, mount, port or credential
metadata is exposed. Unhealthy/stopped/starting members take precedence over
running members. Without a matched container, PostgreSQL and Tailscale fall back
to `systemctl show --property=ActiveState --value` for `postgresql.service` and
`tailscaled.service`. These are observed container/unit states, not application
readiness or Tailscale authentication/connectivity checks. Docker/socket permission
failures produce unknown state. The monitor does not grant itself Docker access.

## Safeguards and tests

No mutation endpoints or controls. No user-provided command fragments, shell=True,
SQL identifiers, paths or bind settings from HTTP requests. Subprocess arguments
are fixed lists with two-second timeouts and a minimal environment. Files are
confined to the configured logs root. Responses exclude raw exceptions, report
contents, credentials, email addresses and process environments. DOM content uses
textContent, with same-origin assets and a restrictive CSP. Run unprivileged.

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Monitor tests cover latest-row semantics, attribution gates, unchanged fixture DB
bytes, missing DB not created, missing schema/proof, report states and malformed
reports, path confinement, fake Linux process/stdout association, argv filtering,
service failure states, route caching and rejection of writes. Existing browser
tests require permission to launch local Chromium. Root-level scripts named
`test_results.py`/`connect_test.py` are interactive live-browser utilities, not the
offline test suite.

Local validation (2026-09-26): full suite **141 tests passed**, including 10 monitor
tests. Chromium desktop (1280 px) and phone (390 px) smoke checks passed, including
API responses, periodic refresh, no document-wide horizontal overflow and no
JavaScript errors. Native Linux process inspection is tested with a synthetic
`/proc` tree; Duke's actual processes, service names/permissions and hardware
metrics have not been tested because deployment is intentionally deferred.
