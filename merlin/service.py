"""Workspace-scoped MERLIN boundary over Dastabase Import & Enrich."""
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class MerlinArtifact:
    artifact_id: str
    job_id: str
    kind: str
    created_at: str
    size_bytes: int
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

    def create_workspace(self, display_name):
        return self.repository.create_workspace(display_name)

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

    def queue_import(self, workspace_id, upload_id, display_name, mapping):
        self._require_workspace(workspace_id)
        if self.repository.get_upload_for_workspace(workspace_id, upload_id) is None:
            raise LookupError('Upload not found')
        stored = self.repository.create_import_enrich_job(
            upload_id, display_name, mapping)
        if (stored.get('workspace_id') != workspace_id
                or stored.get('origin_surface') != 'MERLIN'):
            raise RuntimeError('Import job ownership was not preserved')
        return self._job(stored)

    def job(self, workspace_id, job_id):
        stored = self.repository.get_job_for_workspace(workspace_id, job_id)
        return self._job(stored) if stored else None

    def artifacts(self, workspace_id, job_id):
        return tuple(self._artifact(item) for item in
                     self.repository.artifacts_for_workspace(workspace_id, job_id)
                     if item['artifact_type'] == 'UPLOAD_RECONCILIATION')

    def artifact(self, workspace_id, artifact_id):
        stored = self.repository.get_artifact_for_workspace(workspace_id, artifact_id)
        if not stored or stored['artifact_type'] != 'UPLOAD_RECONCILIATION':
            return None
        return self._artifact(stored)

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
    def _job(stored):
        return MerlinJob(stored['job_id'], stored['status'],
            stored['selected_company_count'], stored['processed_company_count'],
            stored.get('progress_stage'), stored['created_at'], stored.get('finished_at'))

    @staticmethod
    def _artifact(stored):
        return MerlinArtifact(stored['artifact_id'], stored['job_id'],
            'ENRICHED_WORKBOOK', stored['created_at'], stored['size_bytes'], stored['sha256'])
