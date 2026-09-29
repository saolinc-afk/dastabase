"""Single persistent Control Room worker; foundation uses fake enrichment only."""
import argparse
import os
import socket
import time
import uuid
from pathlib import Path

from control_room.fake import FakeEnrichmentAdapter
from control_room.repository import JobRepository


def worker_identity():
    return f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}'


class Worker:
    def __init__(self, repository, adapter, worker_id=None):
        self.repository = repository
        self.adapter = adapter
        self.worker_id = worker_id or worker_identity()

    def run_once(self):
        self.repository.heartbeat(self.worker_id)
        job = self.repository.claim_oldest(self.worker_id)
        if job is None:
            return False
        try:
            job = self.repository.transition(job['job_id'], 'RUNNING', worker_id=self.worker_id,
                event_code='FAKE_STARTED', event_message='fake enrichment started')
            self.adapter.run(job, lambda processed, emails, websites, phones:
                self.repository.update_progress(job['job_id'], processed, emails,
                                                websites, phones, self.worker_id))
            self.repository.transition(job['job_id'], 'COMPLETED', worker_id=self.worker_id,
                event_code='ENRICHMENT_COMPLETE', event_message='enrichment complete')
        except Exception as exc:
            current = self.repository.get_job(job['job_id'])
            if current and current['status'] in ('STARTING', 'RUNNING'):
                self.repository.transition(job['job_id'], 'FAILED', worker_id=self.worker_id,
                    error_code='FAKE_ADAPTER_ERROR', error_message=str(exc),
                    event_code='ENRICHMENT_FAILED', event_message='fake enrichment failed')
        finally:
            self.repository.heartbeat(self.worker_id)
        return True

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
    args = parser.parse_args(argv)
    repository = JobRepository(args.database); repository.initialize()
    try:
        Worker(repository, FakeEnrichmentAdapter(args.fake_delay)).run_forever(args.poll_interval)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
