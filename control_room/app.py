"""Dastabase Control Room foundation web application."""
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from engine_identity import ENGINE_IDENTITY
from monitor.metrics import database_metrics, discovery_v2_metrics
from control_room.repository import JobRepository

ROOT = Path(__file__).resolve().parents[1]


def present_job(job):
    if job is None:
        return None
    result = dict(job)
    total = result['selected_company_count']
    result['percent'] = round(100*result['processed_company_count']/total, 1) if total else 0
    result['display_id'] = f'[JOB:{result["job_number"]:04d}]'
    start = result.get('started_at') or result.get('created_at')
    end = result.get('finished_at')
    try:
        result['duration_seconds'] = max(0, int((datetime.fromisoformat(end) if end else
            datetime.now(datetime.fromisoformat(start).tzinfo))-datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        result['duration_seconds'] = None
    return result


def create_app(config=None):
    app = Flask(__name__)
    app.json.sort_keys = False
    app.config.update(
        CONTROL_DB=Path(os.environ.get('CONTROL_ROOM_DB',
            Path.home()/'.local/share/dastabase-control/control_room.sqlite3')),
        CANONICAL_DB=Path(os.environ.get('CONTROL_ROOM_CANONICAL_DB',
            ROOT/'database/dastabase_lite.db')),
        DISCOVERY_V2_PATH=Path(os.environ.get('CONTROL_ROOM_DISCOVERY_V2',
            Path.home()/'dastabase-runs/discovery-v2')),
        WORKER_STALE_SECONDS=float(os.environ.get('CONTROL_ROOM_WORKER_STALE', '15')),
        HOST=os.environ.get('CONTROL_ROOM_HOST', '127.0.0.1'),
        PORT=int(os.environ.get('CONTROL_ROOM_PORT', '8770')),
    )
    if config:
        app.config.update(config)
    repository = JobRepository(app.config['CONTROL_DB'])
    repository.initialize()
    app.extensions['control_repository'] = repository

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'")
        return response

    @app.context_processor
    def globals_():
        return {'engine': ENGINE_IDENTITY}

    @app.template_filter('clock')
    def clock(value):
        try:
            return datetime.fromisoformat(value).astimezone().strftime('%H:%M:%S')
        except (TypeError, ValueError):
            return 'N/A'

    @app.template_filter('duration')
    def duration(value):
        if value is None:
            return 'N/A'
        minutes, seconds = divmod(int(value), 60)
        hours, minutes = divmod(minutes, 60)
        return f'{hours:02d}:{minutes:02d}:{seconds:02d}'

    @app.get('/')
    def overview():
        database = database_metrics(app.config['CANONICAL_DB'])
        discovery = discovery_v2_metrics(app.config['DISCOVERY_V2_PATH'])
        job = present_job(repository.current_job())
        events = repository.events(job['job_id'], 8) if job else []
        return render_template('overview.html', page='overview', database=database,
            discovery=discovery, job=job, events=events,
            worker=repository.worker_status(app.config['WORKER_STALE_SECONDS']))

    @app.route('/enrich', methods=['GET', 'POST'])
    def enrich():
        error = None
        if request.method == 'POST':
            try:
                count = int(request.form.get('company_count', ''))
                job = repository.create_job(request.form.get('display_name'), count)
                return redirect(url_for('job_detail', job_id=job['job_id']), code=303)
            except (TypeError, ValueError) as exc:
                error = str(exc)
        return render_template('enrich.html', page='enrich', error=error), 400 if error else 200

    @app.get('/jobs')
    def jobs():
        return render_template('jobs.html', page='jobs',
                               jobs=[present_job(job) for job in repository.list_jobs()])

    @app.get('/jobs/<job_id>')
    def job_detail(job_id):
        job = present_job(repository.get_job(job_id))
        if job is None:
            abort(404)
        return render_template('job_detail.html', page='jobs', job=job,
                               events=repository.events(job_id))

    @app.get('/data')
    def data():
        return render_template('data.html', page='data')

    @app.get('/api/jobs/<job_id>')
    def job_progress(job_id):
        job = present_job(repository.get_job(job_id))
        if job is None:
            abort(404)
        return jsonify(job=job, events=repository.events(job_id, 30),
            worker=repository.worker_status(app.config['WORKER_STALE_SECONDS']))

    @app.get('/api/current-job')
    def current_job():
        job = present_job(repository.current_job())
        return jsonify(job=job, events=repository.events(job['job_id'], 8) if job else [],
            worker=repository.worker_status(app.config['WORKER_STALE_SECONDS']))

    return app


def main():
    app = create_app()
    app.run(host=app.config['HOST'], port=app.config['PORT'], debug=False, threaded=True)


if __name__ == '__main__':
    main()
