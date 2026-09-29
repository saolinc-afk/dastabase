# Dastabase Control Room v0.1 foundation

This milestone proves persistent orchestration with a deterministic fake adapter.
It does not invoke Discovery v2, search providers, canonical database writes,
uploads, matching, or exports.

Runtime state defaults outside the repository to:

```text
~/.local/share/dastabase-control/control_room.sqlite3
```

Configuration:

| Variable | Default |
| --- | --- |
| `CONTROL_ROOM_DB` | `~/.local/share/dastabase-control/control_room.sqlite3` |
| `CONTROL_ROOM_CANONICAL_DB` | repository `database/dastabase_lite.db` |
| `CONTROL_ROOM_DISCOVERY_V2` | `~/dastabase-runs/discovery-v2` |
| `CONTROL_ROOM_HOST` | `127.0.0.1` |
| `CONTROL_ROOM_PORT` | `8770` |
| `CONTROL_ROOM_WORKER_POLL` | `1` second |
| `CONTROL_ROOM_FAKE_DELAY` | `0.25` seconds per progress checkpoint |
| `CONTROL_ROOM_WORKER_STALE` | `15` seconds |

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
