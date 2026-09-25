#!/bin/sh
set -eu

selection="logs/phase2_production_1000_selection.json"
part=0
while [ "$part" -lt 5 ]; do
    start=$((part * 200))
    end=$(((part + 1) * 200))
    chunk_ids=$(.venv/bin/python -B -c "import json; s=json.load(open('$selection'))['selected']; print(' '.join(str(x['id']) for x in s[$start:$end]))")
    .venv/bin/python -B -m discovery.runner \
        --limit 200 --ids $chunk_ids \
        --report "logs/phase2_production_1000_part$((part + 1)).json"
    part=$((part + 1))
done

.venv/bin/python -B - <<'PY'
import json
from pathlib import Path

reports = [json.loads(Path(f'logs/phase2_production_1000_part{i}.json').read_text()) for i in range(1, 6)]
entries = [entry for report in reports for entry in report.get('companies', [])]
selected = json.loads(Path('logs/phase2_production_1000_selection.json').read_text())['selected']
assert len(entries) == 1000 and {e['company_id'] for e in entries} == {e['id'] for e in selected}
counts = {}
retryable = non_retryable = 0
http = searches = 0
for entry in entries:
    status = (entry.get('website') or {}).get('status', 'ERROR')
    counts[status] = counts.get(status, 0) + 1
    http += entry.get('http_requests', 0)
    searches += entry.get('search_calls', 0)
    if status == 'ERROR':
        text = ('\n'.join(entry.get('fetch_errors', [])) + ' ' +
                str((entry.get('website') or {}).get('errors', [])) + ' ' + str(entry.get('error', ''))).lower()
        if any(token in text for token in ('ddgsexception', 'timeout', 'dns error', 'nodename nor servname', 'temporarily unavailable')):
            retryable += 1
        else:
            non_retryable += 1
report = {
    'selected_count': 1000,
    'processed': len(entries),
    'selected_ids': [e['id'] for e in selected],
    'companies': entries,
    'http_requests': http,
    'search_calls': searches,
    'website_statuses': counts,
    'retryable_errors': retryable,
    'non_retryable_errors': non_retryable,
    'source_reports': [f'logs/phase2_production_1000_part{i}.json' for i in range(1, 6)],
    'complete': True,
}
Path('logs/phase2_production_1000.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
PY
