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
| `CONTROL_ROOM_CANONICAL_DB` | repository `database/dastabase_lite.db` |
| `CONTROL_ROOM_DISCOVERY_V2` | `~/dastabase-runs/discovery-v2` |
| `CONTROL_ROOM_HOST` | `127.0.0.1` |
| `CONTROL_ROOM_PORT` | `8770` |
| `CONTROL_ROOM_WORKER_POLL` | `1` second |
| `CONTROL_ROOM_FAKE_DELAY` | `0.25` seconds per progress checkpoint |
| `CONTROL_ROOM_WORKER_STALE` | `15` seconds |
| `CONTROL_ROOM_UPLOAD_MAX_BYTES` | `20971520` (20 MiB) |
| `CONTROL_ROOM_UPLOAD_MAX_ROWS` | `5000` |

Run from the repository root in two terminals using the same database setting.

Terminal A:

```sh
export CONTROL_ROOM_DB="$HOME/.local/share/dastabase-control/control_room.sqlite3"
.venv/bin/python -m control_room.app
```

Terminal B:

```sh
export CONTROL_ROOM_DB="$HOME/.local/share/dastabase-control/control_room.sqlite3"
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
`data_only=True`. Upload-created jobs continue to use the fake adapter.
