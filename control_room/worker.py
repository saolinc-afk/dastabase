"""Single persistent Control Room worker; foundation uses fake enrichment only."""
import argparse
import os
import socket
import threading
import time
import uuid
from pathlib import Path

from control_room.fake import FakeEnrichmentAdapter
from control_room.repository import JobRepository


def sanitized_error(exc):
    message = f'{type(exc).__name__}: {exc}'
    secret = os.environ.get('SERPER_API_KEY')
    if secret:
        message = message.replace(secret,'[REDACTED]')
    return message[:500]


def worker_identity():
    return f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}'


class Worker:
    def __init__(self, repository, adapter, worker_id=None, adapters=None, stale_seconds=30):
        self.repository = repository
        self.adapter = adapter
        self.adapters = adapters or {'FAKE':adapter}
        self.worker_id = worker_id or worker_identity()
        self.stale_seconds = stale_seconds

    def run_once(self):
        self.repository.heartbeat(self.worker_id)
        job = self.repository.claim_oldest(self.worker_id,self.stale_seconds)
        if job is None:
            return False
        stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(stop,), daemon=True)
        heartbeat.start()
        try:
            started_code = {
                'FAKE': 'FAKE_STARTED', 'DISCOVERY_V2': 'DISCOVERY_STARTED',
                'IMPORT_ENRICH': 'IMPORT_ENRICH_STARTED',
            }.get(job['execution_adapter'], 'ENRICHMENT_STARTED')
            job = self.repository.transition(job['job_id'], 'RUNNING', worker_id=self.worker_id,
                event_code=started_code,
                event_message=f'{job["execution_adapter"]} enrichment started')
            adapter = self.adapters.get(job['execution_adapter'])
            if adapter is None:
                raise ValueError(f'Unsupported execution adapter: {job["execution_adapter"]}')
            if hasattr(adapter,'worker_id'):
                adapter.worker_id = self.worker_id
            outcome = adapter.run(job, lambda processed, emails, websites, phones:
                self.repository.update_progress(job['job_id'], processed, emails,
                                                websites, phones, self.worker_id))
            target = 'PARTIAL' if outcome == 'PARTIAL' else 'COMPLETED'
            self.repository.transition(job['job_id'], target, worker_id=self.worker_id,
                event_code='ENRICHMENT_COMPLETE', event_message='enrichment complete')
        except Exception as exc:
            current = self.repository.get_job(job['job_id'])
            if current and current['status'] in ('STARTING', 'RUNNING'):
                self.repository.transition(job['job_id'], 'FAILED', worker_id=self.worker_id,
                    error_code=f'{job["execution_adapter"]}_ADAPTER_ERROR', error_message=sanitized_error(exc),
                    event_code='ENRICHMENT_FAILED', event_message='fake enrichment failed')
        finally:
            stop.set(); heartbeat.join(timeout=2)
            self.repository.heartbeat(self.worker_id)
        return True

    def _heartbeat(self, stop):
        while not stop.wait(5):
            self.repository.heartbeat(self.worker_id)

    def run_forever(self, poll_interval=1.0):
        try:
            while True:
                if not self.run_once():
                    time.sleep(max(0.05, poll_interval))
        finally:
            self.repository.heartbeat(self.worker_id, 'STOPPED')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default=os.environ.get(
        'CONTROL_ROOM_DB', str(Path.home()/'.local/share/dastabase-control/control_room.sqlite3')))
    parser.add_argument('--poll-interval', type=float,
                        default=float(os.environ.get('CONTROL_ROOM_WORKER_POLL', '1')))
    parser.add_argument('--fake-delay', type=float,
                        default=float(os.environ.get('CONTROL_ROOM_FAKE_DELAY', '0.25')))
    configured_canonical = os.environ.get('CONTROL_ROOM_CANONICAL_DB')
    parser.add_argument('--canonical', default=configured_canonical,
                        required=configured_canonical is None,
                        help='Explicit canonical Lite database (or CONTROL_ROOM_CANONICAL_DB)')
    parser.add_argument('--storage-root', default=os.environ.get('CONTROL_ROOM_STORAGE_ROOT',
                        str(Path.home()/'.local/share/dastabase-control')))
    parser.add_argument('--real-discovery-max-companies',type=int,default=int(os.environ.get(
                        'CONTROL_ROOM_REAL_DISCOVERY_MAX_COMPANIES','10')))
    args = parser.parse_args(argv)
    repository = JobRepository(args.database); repository.initialize()
    try:
        fake = FakeEnrichmentAdapter(args.fake_delay)
        from control_room.discovery_adapter import DiscoveryV2JobAdapter
        from control_room.import_enrich_adapter import ImportEnrichAdapter
        discovery = DiscoveryV2JobAdapter(repository,args.storage_root,args.canonical,
                                           max_companies=args.real_discovery_max_companies)
        result_paths = tuple(filter(None, os.environ.get(
            'CONTROL_ROOM_IMPORT_DISCOVERY_RESULTS', '').split(os.pathsep)))
        import_enrich = ImportEnrichAdapter(repository, args.canonical, result_paths,
                                             discovery_adapter=discovery,
                                             storage_root=args.storage_root)
        Worker(repository,fake,adapters={'FAKE':fake,'DISCOVERY_V2':discovery,
            'IMPORT_ENRICH':import_enrich}).run_forever(args.poll_interval)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
