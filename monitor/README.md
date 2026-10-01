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
| `MONITOR_DISCOVERY_V2` | `~/dastabase-runs/discovery-v2/` |

For later private access, set `MONITOR_HOST` to Duke's specific Tailscale/private
interface address. There is no authentication; keep access on trusted
private networking. The included Flask server is for local review; choose a WSGI
service/reverse proxy separately before deployment. No deployment is performed.

## API and display

`GET /` serves the terminal-style console. No development-phase labels remain in
its headings or metrics. Missing metrics render as N/A; unavailable activity
fields are omitted. Services report `unknown` when inspection is unavailable.

| Endpoint | Browser polling | Data |
| --- | --- | --- |
| `GET /api/live` | 1 second | `timestamp`, `server`, `processes` (PID/start-tick identity and runtime only) |
| `GET /api/status` | 2.5 seconds | `timestamp`, `database`, `active`, `workload`, `activity`, `recent`, `services` |

System and operations requests run in separate sequential polling loops, so a
slow database snapshot does not block system refresh. No overlapping requests or
catch-up bursts occur within either loop. Failed requests preserve the prior
section with an independent stale-data warning. Recent timestamps use the
browser's local timezone. A process identity combines PID and Linux start ticks,
so PID reuse cannot update an older job's runtime.

`active.jobs[]` adds `job_type`, `remaining`, `percent`, `last_completed`, and
`identity` alongside the existing PID/report/current company/runtime/breaker
fields. Internal selected/completed ID arrays and raw evidence are never returned.
`activity.entries[]` contains company ID and, when established, company name,
website status, verified domain, report reference and `usable_emails`.
`workload` is a distinct `kind: "observed_workload"` object with
`persistent_queue: false`, `state` (`active`, `idle`, `unknown`), `active_batches`,
`batch_remaining`, `database_remaining`, `unprocessed_in_active_batches`,
`remaining_after_batch`, and `estimate: true`. A future scheduler can add its own
queue object without changing the meaning of observed workload.

Snapshots are observations, not an atomic transaction across DB, processes and
files. Database `sampled_at` identifies its actual last collection time.

### Discovery v2 enrichment

The separate **DISCOVERY V2 / ENRICHMENT** card scans read-only
`results.sqlite3` files below the configured Discovery v2 directory. A genuinely
`RUNNING` run is preferred over every non-running run; otherwise the run with the newest persisted
`finished_at`, `started_at`, or `created_at` is shown. Path and run ID provide
deterministic tie-breaking.

Progress comes from `discovery_run_companies`: `COMPLETED`, `PARTIAL`, `FAILED`,
and `INELIGIBLE` are handled; `RUNNING` and `PENDING` remain pending/unprocessed.
Website and default-contact totals come directly from
`discovery_company_results`. “Companies with Serper evidence” is a distinct
company count from persisted evidence provider rows, not an API query count.
Last activity is the newest reliable persisted run/result/attempt/evidence
timestamp available.

Success is `(COMPLETED + PARTIAL) / (COMPLETED + PARTIAL + FAILED + INELIGIBLE)`;
pending and running companies are excluded. Live Activity is taken only from
persisted results for the selected Discovery v2 run. A persisted `RUNNING` state
is displayed as such without claiming that an operating-system worker is alive.

Connections use SQLite URI `mode=ro`, `PRAGMA query_only=ON`, short busy
timeouts, progress deadlines, and immediate close. Missing directories, locked
files, and incomplete schemas return an unavailable or partial card without
creating or migrating a database.

## Caching and resource budget

- System snapshots cache for 0.8 seconds, operations for 2 seconds, services for
  30 seconds. Slightly shorter server caches than browser polling prevent boundary
  timing from accidentally halving the visible refresh rate. Cache locks are
  independent, and concurrent tabs share one collection per interval.
- The live endpoint reads only Linux process metadata and system counters: no
  SQLite, report parsing, Docker, systemctl, or other subprocess calls.
- Database and WAL inode/size/nanosecond timestamps invalidate the database cache.
  Unchanged files reuse results; a 30-second fallback recollection prevents
  indefinite reuse. Failed/racing reads retry on the next operations tick.
- Attribution results are memoized per company using a SHA-256 fingerprint of all
  company/email inputs. Only changed inputs are re-evaluated. The memo holds small
  digests and counts, not evidence blobs; deleted companies are evicted.
- Report metadata invalidates an LRU of at most 64 projected reports. Unchanged
  checkpoints are not decoded again. Bulky evidence is discarded while decoding;
  full report objects are never retained in the cache. Existing 32 MB file limits,
  64 KiB log tails, newest-500 candidate bound and 20-report display remain.
- Name/workload lookups are parameterized and limited to IDs in the observed
  reports; queries are chunked at 400 IDs with a one-second deadline. SQLite
  aggregation retains its two-second query deadline and short-lived connection.
- No new dependencies, frameworks, websocket servers, background workers,
  persistent queues, schema changes or indexes are added.

Local timings on this Mac with the existing 6,561-company database: initial
attribution collection about 3.8 seconds; a forced database reread with unchanged
attribution inputs about 0.18 seconds; unchanged warm operations about 10 ms;
system endpoint below 1 ms. These are local observations, not Duke benchmarks.
The initial collection can be slower than the polling interval; requests never
pile up to compensate. Use one WSGI process to avoid duplicating caches.

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
in an additional bucket. **Processed** is the sum of all these counts:
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
Remaining is `selected - processed`; percentage is `100 * processed / selected`
(rounded to one decimal, zero for an empty batch). Duplicate IDs or entries outside
the selection make a report invalid. The CLI limit can differ from actual selected
count after filtering or resume.
Without a report, these values are N/A, not inferred from a database delta.

For a worker's actual stdout/stderr regular file beneath logs, the last 64 KiB is
read. A runner `Report:` startup line can identify its default report. The latest
`[i/n] ID id name` line supplies a current company only while there is no following
result or breaker line. Pipes and arbitrary files are not read. A truncated startup
line or unavailable output leaves report/current company unknown. The checkpoint's
breaker flag or the runner's exact breaker log marker establishes breaker status.
Current-company data is suppressed if already checkpointed, outside the selection,
a breaker fired, the log predates the process start, or its startup report conflicts
with the explicit report flag. Appended logs are scoped after their latest runner
startup line. A missing log name can be looked up by the explicitly observed ID in
`companies_lite`; selected IDs alone never imply that a company is currently running.
An uncheckpointed log line is only last-observed activity, not proof of liveness.

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

## Activity and workload semantics

The activity feed projects the last ten completed `companies` entries in reverse
checkpoint order from active reports, falling back to the newest recent report
when active report data is unavailable. Multiple active reports are interleaved;
no global chronology is claimed. The latest completed entry also supplies each
job's last-result field. An absent company name is looked up by ID with:

```sql
SELECT c.id, c.company_name,
       EXISTS(SELECT 1 FROM website_discovery w WHERE w.company_id=c.id)
FROM companies_lite c WHERE c.id IN (?, ...);
```

The actual runner does not record per-company completion times. Neither report
mtime nor batch `finished_at` is presented as an event time. VERIFIED results may
show only their official hostname; URL credentials, path, query and fragment are
never exposed. Other candidates are not labelled official. Activity email counts
are historical **at processing time**, not the current database total: the report
must use `phase2-ownership-1`, and every email must have explicit stored
`evidence.attribution.attributable: true`. Missing/old/malformed proof omits the
count. Empty proven-version result lists yield zero. No addresses are returned.
Current database totals still use the full live outreach attribution checks.

Workload calculation:

1. Each observed batch's remaining IDs are `selected_ids - completed_ids`.
2. `batch_remaining` sums each batch's remaining count (actual work, including
   retry/overlapping tasks).
3. Pending IDs are unioned across workers. The lookup above counts those that
   still have **no** discovery result as `unprocessed_in_active_batches`.
4. `remaining_after_batch = max(0, database_remaining - unprocessed_in_active_batches)`.

This avoids subtracting retry jobs or overlapping IDs twice. It is an estimate
assuming all active batches finish and may span slightly different observations.
Missing reports, missing IDs, inaccessible processes or DB lookups yield N/A rather
than an invented queue. When process inspection succeeds and no worker exists,
the UI says "No active batch" and shows unprocessed companies. When process
inspection is unavailable it explicitly says visibility is unavailable.

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

Local validation (2026-09-26): **151 tests passed**, including **20 monitor tests**.
`git diff --check` passed. No dependencies were added in this iteration.
Browser checks include the actual local API plus a browser-only active-worker
fixture (no runner invoked, no database writes) for changing progress, runtime,
activity, metric bars, phone layout and independent stale-refresh warnings.
Native Linux process inspection uses a synthetic `/proc` tree in tests. Duke has
not been accessed or deployed to as part of this iteration.

## Product identity and presentation polish

`engine_identity.py` is the sole authoritative identity source: an immutable
`EngineIdentity` value with `engine_name="SPARROW"` and `engine_version="0.9.0"`.
It has no runtime/storage side effects and is reusable by future product surfaces.
For now only the monitor header consumes it: **ENGINE · SPARROW 0.9.0**. This is
the Dastabase enrichment engine. APIs, database, exports and provenance formats
are unchanged. Removing the header's READ ONLY badge does not alter security.

The active job displays `Current: ID · name` and `Last: ID · name · result`, using
only existing observed fields. Missing current company stays N/A. All batch
counts, progress, PID, runtime, report and breaker information remain.

The JavaScript `resultLabel` helper maps internal `ERROR` to `UNRESOLVED`,
`GROUP_REVIEW` to `GROUP REVIEW`, and `NOT_FOUND` to `NOT FOUND` for display.
The summary uses the matching title-case labels. Status colors still derive from
the original status. API objects, report values and diagnostic text are untouched.
No technical reason classifications were added: the current safe API projection
does not provide them. A future detail view can extend presentation separately.

## Subtle frontend moments

`monitor/static/easter_eggs.js` is isolated from the backend. Its controller receives
already-fetched operations snapshots; it has no fetch, storage, external assets,
subprocesses, database access or polling loop. The normal 1s/2.5s polling and
cached service checks remain unchanged. Its optional observer cannot fail the
operations refresh. A fixed-height, muted text strip below the status row neither
covers content nor shifts the grid when text appears. No sounds, focus changes,
flashes, animation, or screen-reader announcements are introduced.

Exact rules (all state is in memory for the current page session):

- **Spaceship:** an observed active batch's completed count is exactly 42, with a
  valid report reference and selected/processed counts. Once per report path.
  Skipping from 41 to 43 does not trigger it. This is a processed count, not a
  company ID or the inferred company currently in flight.
- **Cookie:** a previously observed active batch crosses from below 100 to at
  least 100 completed companies, once per report path. Alternatively, a previously
  observed active batch below 100 appears in recent reports explicitly complete
  at 100/100 after its worker exits. Historical completed batches on page load do
  not trigger cookies. Count regressions never create new milestones.
- **Rare motifs:** only when an active checkpoint advances, that worker's breaker
  is explicitly not triggered and its last result is not ERROR, with process
  visibility available. Evaluation occurs at most once a minute, and only after
  20 minutes since page initialization or the last rare motif. One random draw:
  dinosaur 0.05%, skis 0.20%, dove 1%, sailboat 1%; otherwise nothing (97.75%).
  The dove is displayed alone. Skis use rarity, not season detection.
- **All motifs:** displayed for six seconds, with a two-minute global cooldown.
  Hidden pages suppress display. Milestones suppressed by visibility/cooldown are
  consumed, not queued for a later surprise. At most one message is shown per
  snapshot. Each actual display schedules just one expiration timeout.

The report path is the session batch key, surviving worker PID changes or resumes.
Reusing the same report path intentionally stays suppressed for the page session;
without a reliable report reference, batch moments are omitted. Reloading/opening
another page creates a new session; no persistence or cross-tab coordination is
needed. Clock, random draw and expiry scheduler can be injected for deterministic
browser tests. Most snapshots do only small map/count checks, with no randomness
or timer creation.

Polish validation (2026-09-26): **159 tests passed** in the complete suite,
including deterministic Chromium tests for identity, compact company rendering,
unchanged internal ERROR values, presentation labels, milestone deduplication,
regressing counts, hidden/cooldown suppression, rare motifs and read-only routes.
Desktop/390px phone fixture previews were visually checked. `git diff --check`
passed. No production processes/services were contacted; no deployment, commit or
push was performed.
