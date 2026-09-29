"""Deterministic no-network enrichment adapter for foundation demonstrations."""
import time


class FakeEnrichmentAdapter:
    def __init__(self, delay=0.25, fail_at=None, sleeper=time.sleep):
        self.delay = max(0, float(delay))
        self.fail_at = fail_at
        self.sleeper = sleeper

    def run(self, job, progress):
        total = job['selected_company_count']
        step = max(1, total//10)
        processed = 0
        while processed < total:
            processed = min(total, processed+step)
            if self.fail_at is not None and processed >= self.fail_at:
                raise RuntimeError('Deterministic fake adapter failure')
            progress(processed, processed//2, processed*3//4, processed//3)
            self.sleeper(self.delay)
