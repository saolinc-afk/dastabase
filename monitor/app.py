"""Run with python -m monitor.app; local access by default."""
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template
from monitor.metrics import ROOT, ServerMetrics, active_jobs, database_metrics, recent_jobs, service_metrics


def create_app(config=None):
    app = Flask(__name__)
    app.json.sort_keys = False
    app.config.update(DB_PATH=Path(os.environ.get('MONITOR_DB', ROOT/'database/dastabase_lite.db')),
                      LOGS_PATH=Path(os.environ.get('MONITOR_LOGS', ROOT/'logs')), CACHE_SECONDS=10)
    if config:
        app.config.update(config)
    lock = threading.Lock()
    cache = {'at': float('-inf'), 'data': None}
    server = ServerMetrics()

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'"
        return response

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/status')
    def status():
        with lock:
            if time.monotonic()-cache['at'] >= app.config['CACHE_SECONDS']:
                logs = Path(app.config['LOGS_PATH'])
                active = active_jobs(logs)
                cache['data'] = dict(timestamp=datetime.now(timezone.utc).isoformat(),
                    database=database_metrics(app.config['DB_PATH']), active=active,
                    recent=recent_jobs(logs, active), server=server.collect(), services=service_metrics())
                cache['at'] = time.monotonic()
            return jsonify(cache['data'])

    return app


if __name__ == '__main__':
    create_app().run(host=os.environ.get('MONITOR_HOST', '127.0.0.1'),
                     port=int(os.environ.get('MONITOR_PORT', '8765')), debug=False)
