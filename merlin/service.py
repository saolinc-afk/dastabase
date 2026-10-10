"""Workspace-scoped MERLIN boundary over Dastabase Import & Enrich."""
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

from control_room.import_outputs import (decode_requested_outputs,
                                         missing_persisted_outputs)
from control_room.repository import JobRepository
from control_room.uploads import store_and_parse


@dataclass(frozen=True)
class MerlinUpload:
    upload_id: str
    original_filename: str
    format: str
    row_count: int
    headers: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class MerlinJob:
    job_id: str
    status: str
    total: int
    processed: int
    stage: str | None
    created_at: str
    finished_at: str | None
    requested_outputs: tuple[str, ...]
    matched_rows: int
    review_rows: int
    complete_rows: int | None
    incomplete_rows: int | None
    progress_completed_units: int | None
    progress_total_units: int | None
    progress_stage_started_at: str | None
    last_activity_at: str | None


@dataclass(frozen=True)
class MerlinArtifact:
    artifact_id: str
    job_id: str
    kind: str
    created_at: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class MerlinDownload:
    content: bytes
    filename: str
    sha256: str


class MerlinImportEnrichService:
    """Narrow service API; it never imports the Control Room web application."""

    def __init__(self, repository: JobRepository, storage_root=None, *,
                 max_upload_bytes=20 * 1024 * 1024, max_upload_rows=5000):
        self.repository = repository
        self.storage_root = (Path(storage_root).expanduser().absolute()
                             if storage_root is not None else None)
        self.max_upload_bytes = int(max_upload_bytes)
        self.max_upload_rows = int(max_upload_rows)

    def create_workspace(self, display_name, workspace_id=None):
        return self.repository.create_workspace(display_name, workspace_id)

    def ingest_upload(self, workspace_id, file_storage):
        if self.storage_root is None:
            raise ValueError('MERLIN upload storage is not configured')
        self._require_workspace(workspace_id)
        metadata = store_and_parse(file_storage, self.storage_root,
                                   self.max_upload_bytes, self.max_upload_rows)
        try:
            stored = self.repository.create_workspace_upload(workspace_id, metadata)
        except BaseException:
            (self.storage_root / metadata['relative_path']).unlink(missing_ok=True)
            raise
        return self._upload(stored)

    def upload(self, workspace_id, upload_id):
        stored = self.repository.get_upload_for_workspace(workspace_id, upload_id)
        return self._upload(stored) if stored else None

    def queue_import(self, workspace_id, upload_id, display_name, mapping,
                     requested_outputs=None):
        self._require_workspace(workspace_id)
        if self.repository.get_upload_for_workspace(workspace_id, upload_id) is None:
            raise LookupError('Upload not found')
        stored = self.repository.create_import_enrich_job(
            upload_id, display_name, mapping, requested_outputs)
        if (stored.get('workspace_id') != workspace_id
                or stored.get('origin_surface') != 'MERLIN'):
            raise RuntimeError('Import job ownership was not preserved')
        return self._job(stored)

    def job(self, workspace_id, job_id):
        stored = self.repository.get_job_for_workspace(workspace_id, job_id)
        if not stored:
            return None
        items = (self.repository.job_items(job_id)
                 if stored['status'] in ('COMPLETED', 'PARTIAL') else None)
        return self._job(stored, items)

    def artifacts(self, workspace_id, job_id):
        return tuple(self._artifact(item) for item in
                     self.repository.artifacts_for_workspace(workspace_id, job_id)
                     if item['artifact_type'] == 'UPLOAD_RECONCILIATION')

    def artifact(self, workspace_id, artifact_id):
        stored = self.repository.get_artifact_for_workspace(workspace_id, artifact_id)
        if not stored or stored['artifact_type'] != 'UPLOAD_RECONCILIATION':
            return None
        return self._artifact(stored)

    def final_workbook(self, workspace_id, job_id):
        """Return verified final bytes without exposing an internal path."""
        if self.storage_root is None:
            raise ValueError('MERLIN upload storage is not configured')
        job = self.repository.get_job_for_workspace(workspace_id, job_id)
        if not job or job['status'] not in ('COMPLETED', 'PARTIAL'):
            return None
        artifacts = [item for item in
                     self.repository.artifacts_for_workspace(workspace_id, job_id)
                     if item['artifact_type'] == 'UPLOAD_RECONCILIATION']
        if len(artifacts) != 1:
            return None
        artifact = artifacts[0]
        root = self.storage_root.resolve()
        candidate = self.storage_root/artifact['relative_path']
        try:
            path = candidate.resolve(strict=True)
        except OSError:
            return None
        if not path.is_relative_to(root) or candidate.is_symlink() or not path.is_file():
            return None
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if (len(content) != artifact['size_bytes'] or
                not hmac.compare_digest(digest, artifact['sha256'])):
            return None
        return MerlinDownload(content, 'merlin-enriched.xlsx', digest)

    def _require_workspace(self, workspace_id):
        workspace = self.repository.get_workspace(workspace_id)
        if not workspace or workspace['status'] != 'ACTIVE':
            raise LookupError('Workspace not found')
        return workspace

    @staticmethod
    def _upload(stored):
        return MerlinUpload(stored['upload_id'], stored['original_filename'],
            stored['format'], stored['row_count'], tuple(stored['headers']), stored['status'])

    @staticmethod
    def _job(stored, items=None):
        outputs = decode_requested_outputs(stored.get('requested_outputs_json')) or ()
        complete_rows = incomplete_rows = None
        if items is not None and outputs:
            matched = [item for item in items if item['match_status'] == 'MATCHED']
            incomplete_rows = sum(bool(missing_persisted_outputs(
                item.get('enrichment'), outputs)) for item in matched)
            complete_rows = len(matched) - incomplete_rows
        return MerlinJob(stored['job_id'], stored['status'],
            stored['selected_company_count'], stored['processed_company_count'],
            stored.get('progress_stage'), stored['created_at'], stored.get('finished_at'),
            outputs,
            stored.get('import_matched_count') or 0,
            ((stored.get('import_ambiguous_count') or 0) +
             (stored.get('import_unresolved_count') or 0)),
            complete_rows, incomplete_rows,
            stored.get('progress_completed_units'), stored.get('progress_total_units'),
            stored.get('progress_stage_started_at'),
            max((value for value in (
                stored.get('progress_updated_at'), stored.get('worker_heartbeat_at'),
                stored.get('queued_at'), stored.get('created_at')) if value), default=None))

    @staticmethod
    def _artifact(stored):
        return MerlinArtifact(stored['artifact_id'], stored['job_id'],
            'ENRICHED_WORKBOOK', stored['created_at'], stored['size_bytes'], stored['sha256'])
