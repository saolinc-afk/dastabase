"""Small customer-facing MERLIN web application."""
import hmac
import io
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

from control_room.repository import JobRepository
from control_room.uploads import (IMPORT_FIELDS, UploadError,
                                  suggest_import_mapping)
from merlin.service import MerlinImportEnrichService


IDENTITY_FIELDS = ('company_name', 'tax_number', 'registration_number')
FIELD_LABELS = {
    'person_name': 'Person name', 'email': 'Email', 'phone': 'Phone',
    'company_name': 'Company name', 'tax_number': 'Tax number',
    'registration_number': 'Registration number', 'address': 'Address',
    'municipality': 'Municipality',
}
OUTPUTS = (
    ('WEBSITE', 'Website'), ('EMAIL', 'Email'),
    ('PHONE', 'Phone'), ('FINANCIALS', 'Financials'),
)


def _truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def create_app(config=None):
    app = Flask(__name__)
    app.json.sort_keys = False
    app.config.update(
        SECRET_KEY=os.environ.get('MERLIN_SECRET_KEY'),
        MERLIN_DB=Path(os.environ.get('MERLIN_DB', os.environ.get(
            'CONTROL_ROOM_DB', Path.home()/'.local/share/dastabase-control/control_room.sqlite3'))),
        MERLIN_STORAGE_ROOT=Path(os.environ.get('MERLIN_STORAGE_ROOT', os.environ.get(
            'CONTROL_ROOM_STORAGE_ROOT', Path.home()/'.local/share/dastabase-control'))),
        MERLIN_WORKSPACE_ID=os.environ.get('MERLIN_WORKSPACE_ID', 'merlin-internal'),
        MERLIN_WORKSPACE_NAME=os.environ.get('MERLIN_WORKSPACE_NAME', 'MERLIN Internal'),
        UPLOAD_MAX_BYTES=int(os.environ.get('MERLIN_UPLOAD_MAX_BYTES', str(20*1024*1024))),
        UPLOAD_MAX_ROWS=int(os.environ.get('MERLIN_UPLOAD_MAX_ROWS', '5000')),
        MERLIN_ACTIVITY_STALE_SECONDS=int(os.environ.get(
            'MERLIN_ACTIVITY_STALE_SECONDS', '30')),
        HOST=os.environ.get('MERLIN_HOST', '127.0.0.1'),
        PORT=int(os.environ.get('MERLIN_PORT', '8780')),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=_truthy(os.environ.get('MERLIN_SECURE_COOKIE')),
    )
    if config:
        app.config.update(config)
    if not app.config.get('SECRET_KEY'):
        raise RuntimeError('Set MERLIN_SECRET_KEY or configure SECRET_KEY explicitly')
    app.config['MAX_CONTENT_LENGTH'] = app.config['UPLOAD_MAX_BYTES'] + 1024 * 1024

    repository = JobRepository(app.config['MERLIN_DB'])
    repository.initialize()
    service = MerlinImportEnrichService(repository, app.config['MERLIN_STORAGE_ROOT'],
        max_upload_bytes=app.config['UPLOAD_MAX_BYTES'],
        max_upload_rows=app.config['UPLOAD_MAX_ROWS'])
    workspace_id = app.config['MERLIN_WORKSPACE_ID']
    workspace = repository.get_workspace(workspace_id)
    if workspace is None:
        workspace = service.create_workspace(
            app.config['MERLIN_WORKSPACE_NAME'], workspace_id)
    if workspace['status'] != 'ACTIVE':
        raise RuntimeError('Configured MERLIN workspace is disabled')
    app.extensions['merlin_service'] = service

    def active_workspace():
        value = session.get('workspace_id')
        if not isinstance(value, str) or not value:
            value = workspace_id
            session['workspace_id'] = value
        return value

    def csrf_token():
        token = session.get('csrf_token')
        if not isinstance(token, str) or not token:
            token = secrets.token_urlsafe(32)
            session['csrf_token'] = token
        return token

    def require_upload(upload_id):
        upload = service.upload(active_workspace(), upload_id)
        if upload is None:
            abort(404)
        return upload

    def require_job(job_id):
        job = service.job(active_workspace(), job_id)
        if job is None:
            abort(404)
        return job

    def mapping_state(upload):
        mapping = suggest_import_mapping(upload.headers)
        correction_required = all(mapping.get(field) is None for field in IDENTITY_FIELDS)
        detected = [(FIELD_LABELS[field], upload.headers[index])
                    for field, index in mapping.items() if index is not None]
        return mapping, correction_required, detected

    def submitted_mapping(upload, automatic, correction_required):
        if not correction_required:
            return automatic
        mapping = dict(automatic)
        used = {index for field, index in mapping.items()
                if field not in IDENTITY_FIELDS and index is not None}
        for field in IDENTITY_FIELDS:
            raw = request.form.get(f'mapping_{field}', '')
            index = None if raw == '' else int(raw)
            if index is not None and not 0 <= index < len(upload.headers):
                raise ValueError('Choose a valid source column')
            if index is not None and index in used:
                raise ValueError('Each source column can only be used once')
            mapping[field] = index
            if index is not None:
                used.add(index)
        if all(mapping[field] is None for field in IDENTITY_FIELDS):
            raise ValueError('Choose a company name, tax number, or registration number column')
        return mapping

    def present_job(job):
        final = (job.status in ('COMPLETED', 'PARTIAL') and
                 service.final_workbook(active_workspace(), job.job_id) is not None)
        terminal = job.status in ('COMPLETED', 'PARTIAL', 'FAILED')
        if terminal:
            activity = 'terminal'
        elif job.status == 'QUEUED':
            activity = 'queued'
        else:
            try:
                last = datetime.fromisoformat(job.last_activity_at)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                age = float('inf')
            activity = ('delayed' if age > app.config['MERLIN_ACTIVITY_STALE_SECONDS']
                        else 'active')
        if job.status == 'FAILED':
            state, stage = 'failed', 'We could not finish this file.'
        elif job.status in ('COMPLETED', 'PARTIAL'):
            if final:
                if job.status == 'PARTIAL':
                    state, stage = 'partial', "Done — some information couldn't be found."
                else:
                    state, stage = 'done', 'Done.'
            else:
                state, stage = 'failed', 'We could not prepare the Excel file.'
        else:
            state = 'working'
            stage = ('Waiting to start' if activity == 'queued' else {
                'EXISTING_ENRICHMENT': 'Checking existing information',
                'DISCOVERY': 'Finding missing information',
                'EXPORT': 'Preparing your Excel file',
            }.get(job.stage, 'Identifying companies'))
        completed_units = job.progress_completed_units
        total_units = job.progress_total_units
        determinate = (type(completed_units) is int and type(total_units) is int
                       and total_units > 0 and 0 <= completed_units <= total_units)
        percent = round(100 * completed_units / total_units) if determinate else None
        return {
            'state': state, 'stage': stage, 'activity': activity,
            'completed_units': completed_units if determinate else None,
            'total_units': total_units if determinate else None,
            'percent': max(0, min(percent, 100)) if determinate else None,
            'progress_determinate': determinate,
            'last_activity_at': job.last_activity_at,
            'download_ready': state in ('done', 'partial'),
            'requested_outputs': list(job.requested_outputs),
            'matched_rows': job.matched_rows, 'review_rows': job.review_rows,
            'complete_rows': job.complete_rows,
            'incomplete_rows': job.incomplete_rows,
        }

    @app.before_request
    def session_and_csrf():
        active_workspace()
        if request.method == 'POST':
            supplied = request.form.get('_csrf', '')
            expected = csrf_token()
            if not supplied or not hmac.compare_digest(supplied, expected):
                abort(400)

    @app.after_request
    def security_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        return response

    @app.context_processor
    def template_globals():
        return {'csrf_token': csrf_token}

    @app.get('/')
    def index():
        return render_template('index.html', error=None,
            upload_max_mb=app.config['UPLOAD_MAX_BYTES']//(1024*1024))

    @app.post('/uploads')
    def upload_file():
        try:
            incoming = request.files.get('company_file')
            if incoming is None or not incoming.filename:
                raise UploadError('Choose an XLSX or CSV file.')
            upload = service.ingest_upload(active_workspace(), incoming)
            return redirect(url_for('configure_upload', upload_id=upload.upload_id), code=303)
        except (UploadError, ValueError) as exc:
            return render_template('index.html', error=str(exc),
                upload_max_mb=app.config['UPLOAD_MAX_BYTES']//(1024*1024)), 400

    @app.get('/uploads/<upload_id>')
    def configure_upload(upload_id):
        upload = require_upload(upload_id)
        mapping, correction_required, detected = mapping_state(upload)
        return render_template('configure.html', upload=upload, outputs=OUTPUTS,
            mapping=mapping, correction_required=correction_required,
            detected=detected, field_labels=FIELD_LABELS, identity_fields=IDENTITY_FIELDS,
            error=None)

    @app.post('/uploads/<upload_id>/enrich')
    def enrich_upload(upload_id):
        upload = require_upload(upload_id)
        automatic, correction_required, detected = mapping_state(upload)
        try:
            mapping = submitted_mapping(upload, automatic, correction_required)
            outputs = request.form.getlist('requested_outputs')
            job = service.queue_import(active_workspace(), upload_id,
                f'Enrich {upload.original_filename}', mapping, outputs)
            return redirect(url_for('job_page', job_id=job.job_id), code=303)
        except (TypeError, ValueError) as exc:
            return render_template('configure.html', upload=upload, outputs=OUTPUTS,
                mapping=automatic, correction_required=correction_required,
                detected=detected, field_labels=FIELD_LABELS,
                identity_fields=IDENTITY_FIELDS, error=str(exc)), 400

    @app.get('/jobs/<job_id>')
    def job_page(job_id):
        job = require_job(job_id)
        return render_template('job.html', job=present_job(job), job_id=job_id)

    @app.get('/api/jobs/<job_id>')
    def job_progress(job_id):
        job = require_job(job_id)
        return jsonify(job=present_job(job))

    @app.get('/jobs/<job_id>/download')
    def download(job_id):
        require_job(job_id)
        workbook = service.final_workbook(active_workspace(), job_id)
        if workbook is None:
            abort(404)
        return send_file(io.BytesIO(workbook.content), as_attachment=True,
            download_name=workbook.filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            max_age=0)

    @app.errorhandler(400)
    def bad_request(_error):
        return render_template('error.html', title='Request not accepted',
            message='Please return and try again.'), 400

    @app.errorhandler(404)
    def not_found(_error):
        return render_template('error.html', title='Not found',
            message='This page or file is not available.'), 404

    @app.errorhandler(413)
    def too_large(_error):
        return render_template('error.html', title='File too large',
            message='Choose a smaller XLSX or CSV file.'), 413

    return app


def main():
    app = create_app()
    app.run(host=app.config['HOST'], port=app.config['PORT'],
            debug=False, threaded=True)


if __name__ == '__main__':
    main()
