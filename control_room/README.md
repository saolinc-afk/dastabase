# Dastabase Control Room v0.1 foundation

This milestone proves persistent upload, mapping, matching, review, manifest
creation, and orchestration with a deterministic fake adapter. It does not
invoke Discovery v2, search providers, canonical database writes, or exports.

Runtime state defaults outside the repository to:

```text
~/.local/share/dastabase-control/control_room.sqlite3
```

Configuration:

| Variable | Default |
| --- | --- |
| `CONTROL_ROOM_DB` | `~/.local/share/dastabase-control/control_room.sqlite3` |
| `CONTROL_ROOM_STORAGE_ROOT` | `~/.local/share/dastabase-control` |
| `CONTROL_ROOM_CANONICAL_DB` | required explicit canonical Lite database for the worker |
| `CONTROL_ROOM_IMPORT_DISCOVERY_RESULTS` | ordered, `os.pathsep`-separated Discovery v2 result databases |
| `CONTROL_ROOM_DISCOVERY_V2` | `~/dastabase-runs/discovery-v2` |
| `CONTROL_ROOM_HOST` | `127.0.0.1` |
| `CONTROL_ROOM_PORT` | `8770` |
| `CONTROL_ROOM_WORKER_POLL` | `1` second |
| `CONTROL_ROOM_FAKE_DELAY` | `0.25` seconds per progress checkpoint |
| `CONTROL_ROOM_WORKER_STALE` | `15` seconds |
| `CONTROL_ROOM_UPLOAD_MAX_BYTES` | `20971520` (20 MiB) |
| `CONTROL_ROOM_UPLOAD_MAX_ROWS` | `5000` |
| `CONTROL_ROOM_REAL_DISCOVERY_MAX_COMPANIES` | `10` |

Run from the repository root in two terminals using the same database setting.

Terminal A:

```sh
export CONTROL_ROOM_DB="$HOME/.local/share/dastabase-control/control_room.sqlite3"
export CONTROL_ROOM_CANONICAL_DB="/absolute/path/to/database/dastabase_lite_18916_20261001.db"
.venv/bin/python -m control_room.app
```

Terminal B:

```sh
export CONTROL_ROOM_DB="$HOME/.local/share/dastabase-control/control_room.sqlite3"
export CONTROL_ROOM_CANONICAL_DB="/absolute/path/to/database/dastabase_lite_18916_20261001.db"
.venv/bin/python -m control_room.worker
```

Open <http://127.0.0.1:8770>, choose **ENRICH**, and create a fake job. The web
request only queues it. The independent worker claims and executes it, so closing
or reloading the browser does not affect progress.

The fake adapter performs no network requests and never opens a Discovery result
database. Its deterministic totals at completion are 50% email coverage, 75%
website coverage, and one-third phone coverage.

## Upload workflow

`ENRICH / UPLOAD` accepts CSV and XLSX files, stores them under the configured
storage root, and persists parsing, mapping, matching, review, and confirmation
state in the Control Room database. The canonical `companies_lite` database is
opened with SQLite `mode=ro` and `query_only=ON`.

Matching precedence is exact tax number, exact registration number, coherent
normalized legal name plus address/municipality, and unique exact normalized
legal name. Similar names only create `FUZZY_CANDIDATE` review options; fuzzy
similarity never auto-selects a company. Duplicate canonical company IDs remain
as separate upload rows for reconciliation. The immutable job manifest contains
the first selected row for each company ID once.

Uploaded files use generated names, mode `0600`, and live outside static files.
CSV supports UTF-8, UTF-8 BOM, Windows-1250, comma, semicolon, and tab input.
XLSX parsing uses the first visible worksheet with `read_only=True` and
`data_only=True`.

Import & Enrich reads accepted enrichment through the read-only
`KnowledgeRepository`. Discovery result databases must be listed explicitly;
the repository never scans a directory. Newer REVIEW or missing values do not
erase older accepted facts, and conflicting accepted values remain available
in snapshot conflict metadata. Registry-order precedence is also available for
audits that require caller-controlled source priority.

### Shared company knowledge

`KnowledgeRepository` is the shared, read-only company view used by Control
Room. It combines canonical identity and 2025 financial fields with strictly
accepted SPARROW 0.9 and Discovery v2 website/contact results. It does not write
to any source database, retrieve raw evidence, or perform network requests.

The stable public API is:

- `snapshot(company_id)` for a deeply read-only typed snapshot;
- `iter_snapshots(company_ids=None)` for deterministic canonical-ID iteration,
  optionally restricted to an explicit subset;
- `record(company_id)` for an independent JSON-serializable projection.

The record projection contains `company_id`, `identity` and
`identity_provenance`, `financials` and `financial_provenance`, the selected
`website`, all `emails` plus `default_email`, all `phones` plus
`default_phone`, and `conflicts`, `missing_fields`, and `stale_fields`. Each
website/contact fact contains `value`, `status`, `observed_at`, `stale`, and
`evidence_locators`.

Canonical identity and financial fields retain per-field canonical provenance.
Website is singular: accepted Discovery statuses are `VERIFIED`, `HIGH`, and
`MEDIUM`, with the existing safe SPARROW fallback. Under the default `newest`
policy, the newest accepted Discovery value wins; `input-order` lets an audit
use the explicitly supplied database order. Distinct accepted website values
remain field-labelled conflicts with evidence locators.

Emails and phones are multi-value knowledge. All accepted contacts are retained
and the first value under the same deterministic source precedence is the
explicit default. A persisted Discovery default contact is eligible when its
attribution is `ATTRIBUTED`, even when the result's website remains `REVIEW`.
Normal alternative contacts are not conflicts. SPARROW email acceptance keeps
its existing first-party domain and attribution restrictions.

Each accepted SPARROW or Discovery fact exposes structured evidence locators:
the source namespace/database and company ID, plus the applicable legacy row or
Discovery run, attempt, result, observation, evidence, and rule identifiers.
Locators identify the persisted source; they do not embed raw HTML or create a
second evidence store.

A repository instance is an immutable point-in-time view. It never live-refreshes
while a Discovery run writes new results. Construct a new repository instance
to observe newly completed results. Import & Enrich persists the selected
record into its durable job payload, and XLSX export continues to read that
payload rather than reopening live source databases.

## Discovery v2 jobs

Upload jobs explicitly select either `FAKE` or `DISCOVERY_V2` at confirmation.
The explicitly styled live start action confirms the selected live mode, and
jobs must remain within the configured company limit. Discovery execution always enables municipality context and sets
the maximum Serper query count to one per company.

Each live job uses a generated directory under `jobs/<job-id>/` containing its
immutable manifest, isolated Discovery result database, accepted Discovery CSV,
and row-preserving upload reconciliation CSV. The Control Room persists the
Discovery run ID before execution. A stale job resumes that run and refuses to
create a replacement run when results already exist.

The worker requires `SERPER_API_KEY` for live jobs. The key is passed directly
to the existing Discovery v2 Serper provider and is never persisted by Control
Room.
