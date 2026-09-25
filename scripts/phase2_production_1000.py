#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path

selection_path = Path("logs/phase2_production_1000_selection.json")
selection = json.loads(selection_path.read_text())
selected = selection["selected"]
assert len(selected) == 1000

for part in range(5):
    chunk = selected[part * 200 : (part + 1) * 200]
    ids = [str(row["id"]) for row in chunk]
    report_path = Path(f"logs/phase2_production_1000_part{part + 1}.json")
    subprocess.run(
        [
            ".venv/bin/python", "-B", "-m", "discovery.runner",
            "--limit", "200", "--ids", *ids, "--report", str(report_path),
        ],
        check=True,
    )

reports = [
    json.loads(Path(f"logs/phase2_production_1000_part{i}.json").read_text())
    for i in range(1, 6)
]
entries = [entry for report in reports for entry in report.get("companies", [])]
selected_ids = {row["id"] for row in selected}
assert len(entries) == 1000
assert {entry["company_id"] for entry in entries} == selected_ids

counts = {}
retryable = non_retryable = http = searches = 0
for entry in entries:
    status = (entry.get("website") or {}).get("status", "ERROR")
    counts[status] = counts.get(status, 0) + 1
    http += entry.get("http_requests", 0)
    searches += entry.get("search_calls", 0)
    if status == "ERROR":
        text = (
            "\n".join(entry.get("fetch_errors", [])) + " "
            + str((entry.get("website") or {}).get("errors", [])) + " "
            + str(entry.get("error", ""))
        ).lower()
        if any(token in text for token in (
            "ddgsexception", "timeout", "dns error", "nodename nor servname",
            "temporarily unavailable", "connection reset", "429",
        )):
            retryable += 1
        else:
            non_retryable += 1

final = {
    "selected_count": 1000,
    "processed": len(entries),
    "selected_ids": [row["id"] for row in selected],
    "companies": entries,
    "http_requests": http,
    "search_calls": searches,
    "website_statuses": counts,
    "retryable_errors": retryable,
    "non_retryable_errors": non_retryable,
    "source_reports": [f"logs/phase2_production_1000_part{i}.json" for i in range(1, 6)],
    "complete": True,
}
Path("logs/phase2_production_1000.json").write_text(
    json.dumps(final, ensure_ascii=False, indent=2)
)
