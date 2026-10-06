"""Dastabase Control Room foundation web application."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

from engine_identity import ENGINE_IDENTITY
from monitor.metrics import database_metrics, discovery_v2_metrics
from control_room.repository import JobRepository
from control_room.matching import (CanonicalMatcher, normalize_name, normalize_registration,
                                   normalize_tax, normalize_text, read_companies)
from control_room.uploads import FIELDS, UploadError, store_and_parse, suggest_mapping

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
    configured_canonical = os.environ.get('CONTROL_ROOM_CANONICAL_DB')
    app.config.update(
        CONTROL_DB=Path(os.environ.get('CONTROL_ROOM_DB',
            Path.home()/'.local/share/dastabase-control/control_room.sqlite3')),
        CANONICAL_DB=Path(configured_canonical) if configured_canonical else None,
        DISCOVERY_V2_PATH=Path(os.environ.get('CONTROL_ROOM_DISCOVERY_V2',
            Path.home()/'dastabase-runs/discovery-v2')),
        WORKER_STALE_SECONDS=float(os.environ.get('CONTROL_ROOM_WORKER_STALE', '15')),
        HOST=os.environ.get('CONTROL_ROOM_HOST', '127.0.0.1'),
        PORT=int(os.environ.get('CONTROL_ROOM_PORT', '8770')),
        CONTROL_STORAGE_ROOT=Path(os.environ.get('CONTROL_ROOM_STORAGE_ROOT',
            Path.home()/'.local/share/dastabase-control')),
        UPLOAD_MAX_BYTES=int(os.environ.get('CONTROL_ROOM_UPLOAD_MAX_BYTES', str(20*1024*1024))),
        UPLOAD_MAX_ROWS=int(os.environ.get('CONTROL_ROOM_UPLOAD_MAX_ROWS', '5000')),
        REAL_DISCOVERY_MAX_COMPANIES=int(os.environ.get(
            'CONTROL_ROOM_REAL_DISCOVERY_MAX_COMPANIES','10')),
    )
    if config:
        app.config.update(config)
    if app.config['CANONICAL_DB'] is None:
        raise RuntimeError('Set CONTROL_ROOM_CANONICAL_DB or configure CANONICAL_DB explicitly')
    repository = JobRepository(app.config['CONTROL_DB'])
    repository.initialize()
    app.extensions['control_repository'] = repository

    def canonical():
        return read_companies(app.config['CANONICAL_DB'])

    def require_upload(upload_id):
        upload = repository.get_upload(upload_id)
        if upload is None:
            abort(404)
        return upload

    def mapped_rows(upload, mapping):
        result = []
        for stored in repository.upload_rows(upload['upload_id']):
            values = stored['original_values']
            value = lambda field: values[mapping[field]] if mapping.get(field) is not None else ''
            result.append({'row_number': stored['row_number'],
                'normalized_name': normalize_name(value('company_name')),
                'normalized_tax_number': normalize_tax(value('tax_number')),
                'normalized_registration_number': normalize_registration(value('registration_number')),
                'normalized_address': normalize_text(value('address')),
                'normalized_municipality': normalize_text(value('municipality'))})
        return result

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
        return render_template('enrich.html', page='enrich', error=error,
            upload_max_mb=app.config['UPLOAD_MAX_BYTES']//(1024*1024)), 400 if error else 200

    @app.post('/uploads')
    def upload_file():
        try:
            incoming = request.files.get('company_file')
            if incoming is None or not incoming.filename:
                raise UploadError('Choose a CSV or XLSX file')
            metadata = store_and_parse(incoming, app.config['CONTROL_STORAGE_ROOT'],
                app.config['UPLOAD_MAX_BYTES'], app.config['UPLOAD_MAX_ROWS'])
            upload = repository.create_upload(metadata)
            return redirect(url_for('upload_mapping',upload_id=upload['upload_id']),code=303)
        except (UploadError,ValueError) as exc:
            return render_template('enrich.html',page='enrich',error=str(exc),
                upload_max_mb=app.config['UPLOAD_MAX_BYTES']//(1024*1024)),400

    @app.route('/uploads/<upload_id>/mapping',methods=['GET','POST'])
    def upload_mapping(upload_id):
        upload = require_upload(upload_id)
        suggestions = suggest_mapping(upload['headers'])
        error = None
        if request.method == 'POST':
            mapping = {}
            used = set()
            try:
                for field in FIELDS:
                    raw = request.form.get(field,'')
                    index = None if raw == '' else int(raw)
                    if index is not None and not 0 <= index < len(upload['headers']):
                        raise ValueError('Invalid column mapping')
                    if index is not None and index in used:
                        raise ValueError('Each source column may only be mapped once')
                    mapping[field] = index
                    if index is not None: used.add(index)
                if all(mapping[field] is None for field in ('company_name','tax_number','registration_number')):
                    raise ValueError('Map a company name, tax number, or registration number column')
                rows = mapped_rows(upload,mapping)
                matcher = CanonicalMatcher(canonical())
                matches = [matcher.match(row) for row in rows]
                repository.save_mapping_and_matches(upload_id,mapping,rows,matches)
                return redirect(url_for('upload_review',upload_id=upload_id),code=303)
            except (ValueError,sqlite3.Error) as exc:
                error = str(exc)
                suggestions = mapping
        return render_template('upload_mapping.html',page='enrich',upload=upload,
            fields=FIELDS,suggestions=suggestions,error=error),400 if error else 200

    @app.get('/uploads/<upload_id>/review')
    def upload_review(upload_id):
        upload = require_upload(upload_id)
        if upload['status'] == 'MAPPING':
            return redirect(url_for('upload_mapping',upload_id=upload_id),code=303)
        selected_filter = request.args.get('status','ALL').upper()
        allowed = {'ALL','MATCHED','AMBIGUOUS','NOT_FOUND','EXCLUDED'}
        if selected_filter not in allowed: selected_filter = 'ALL'
        rows = repository.upload_rows(upload_id,None if selected_filter == 'ALL' else selected_filter)
        candidates = repository.candidates(upload_id)
        companies = {company['id']:company for company in canonical()}
        for row in rows:
            row['input'] = dict(zip(upload['headers'],row['original_values']))
            row['candidates'] = [{**candidate,'company':companies.get(candidate['company_id'],{})}
                                 for candidate in candidates.get(row['row_number'],[])]
            row['selected_company'] = companies.get(row['selected_company_id'])
        return render_template('upload_review.html',page='enrich',upload=upload,rows=rows,
            summary=repository.upload_summary(upload_id),selected_filter=selected_filter)

    @app.post('/uploads/<upload_id>/rows/<int:row_number>')
    def upload_decision(upload_id,row_number):
        require_upload(upload_id)
        action = request.form.get('action')
        try:
            if action == 'exclude':
                repository.resolve_upload_row(upload_id,row_number)
            elif action == 'select':
                repository.resolve_upload_row(upload_id,row_number,int(request.form.get('company_id','')))
            else:
                raise ValueError('Invalid review action')
        except ValueError as exc:
            abort(400,str(exc))
        return redirect(url_for('upload_review',upload_id=upload_id),code=303)

    @app.post('/uploads/<upload_id>/rows/<int:row_number>/select/<int:company_id>')
    def upload_select_candidate(upload_id,row_number,company_id):
        require_upload(upload_id)
        try:
            repository.resolve_upload_row(upload_id,row_number,company_id)
        except ValueError as exc:
            abort(400,str(exc))
        return redirect(url_for('upload_review',upload_id=upload_id),code=303)

    @app.route('/uploads/<upload_id>/confirm',methods=['GET','POST'])
    def upload_confirm(upload_id):
        upload = require_upload(upload_id)
        if upload['status'] == 'MAPPING':
            return redirect(url_for('upload_mapping',upload_id=upload_id),code=303)
        summary = repository.upload_summary(upload_id)
        error = None
        if request.method == 'POST':
            try:
                adapter = request.form.get('execution_adapter','FAKE')
                job = repository.create_upload_job(upload_id,request.form.get('display_name'),adapter,
                    app.config['REAL_DISCOVERY_MAX_COMPANIES'],
                    live_confirmed=adapter == 'DISCOVERY_V2')
                return redirect(url_for('job_detail',job_id=job['job_id']),code=303)
            except ValueError as exc:
                error = str(exc)
        return render_template('upload_confirm.html',page='enrich',upload=upload,summary=summary,
            error=error,real_limit=app.config['REAL_DISCOVERY_MAX_COMPANIES']),400 if error else 200

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
                               events=repository.events(job_id),artifacts=repository.artifacts(job_id))

    @app.get('/jobs/<job_id>/artifacts/<artifact_id>')
    def download_artifact(job_id,artifact_id):
        artifact = repository.get_artifact(artifact_id)
        if not artifact or artifact['job_id'] != job_id or artifact['artifact_type'] not in (
                'DISCOVERY_EXPORT','UPLOAD_RECONCILIATION'):
            abort(404)
        root = Path(app.config['CONTROL_STORAGE_ROOT']).expanduser().resolve()
        candidate = root/artifact['relative_path']
        try:
            path = candidate.resolve(strict=True)
        except OSError:
            abort(404)
        if not path.is_relative_to(root) or candidate.is_symlink():
            abort(404)
        return send_file(path,as_attachment=True,download_name=path.name)

    @app.get('/data')
    def data():
        return render_template('data.html', page='data')

    @app.get('/api/jobs/<job_id>')
    def job_progress(job_id):
        job = present_job(repository.get_job(job_id))
        if job is None:
            abort(404)
        return jsonify(job=job, events=repository.events(job_id, 30),
            artifacts=repository.artifacts(job_id),
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
