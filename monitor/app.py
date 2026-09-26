"""Run with python -m monitor.app; local access by default."""
import os
from pathlib import Path

from flask import Flask, jsonify, render_template
from monitor.metrics import ROOT, ServerMetrics, active_jobs, recent_jobs, service_metrics
from monitor.state import (DatabaseCache, ReportCache, SnapshotCache, operation_details,
                           public_jobs, stamp)


def create_app(config=None):
    app = Flask(__name__)
    app.json.sort_keys = False
    app.config.update(DB_PATH=Path(os.environ.get('MONITOR_DB', ROOT/'database/dastabase_lite.db')),
                      LOGS_PATH=Path(os.environ.get('MONITOR_LOGS', ROOT/'logs')),
                      CACHE_SECONDS=2, LIVE_CACHE_SECONDS=0.8, SERVICE_CACHE_SECONDS=30)
    if config:
        app.config.update(config)
    status_cache = SnapshotCache(app.config['CACHE_SECONDS'])
    live_cache = SnapshotCache(app.config['LIVE_CACHE_SECONDS'])
    services = SnapshotCache(app.config['SERVICE_CACHE_SECONDS'])
    database = DatabaseCache()
    reports = ReportCache()
    server = ServerMetrics()
    logs = Path(app.config['LOGS_PATH'])

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'"
        return response

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/live')
    def live():
        def collect():
            processes = active_jobs(logs, details=False)
            return dict(timestamp=stamp(), server=server.collect(),
                        processes={**processes, 'jobs': [
                            {k: j.get(k) for k in ('pid', 'identity', 'module', 'job_type', 'runtime_seconds')}
                            for j in processes['jobs']]})
        return jsonify(live_cache.get(collect))

    @app.get('/api/status')
    def status():
        def collect():
            db = database.collect(app.config['DB_PATH'])
            active = active_jobs(logs, report_reader=reports.read)
            recent = recent_jobs(logs, active, report_reader=reports.read)
            workload, activity = operation_details(db, active, recent, app.config['DB_PATH'])
            active, recent = public_jobs(active, recent)
            return dict(timestamp=stamp(), database=db, active=active, workload=workload,
                        activity=activity, recent=recent, services=services.get(service_metrics))
        return jsonify(status_cache.get(collect))

    return app


if __name__ == '__main__':
    create_app().run(host=os.environ.get('MONITOR_HOST', '127.0.0.1'),
                     port=int(os.environ.get('MONITOR_PORT', '8765')), debug=False, threaded=True)
