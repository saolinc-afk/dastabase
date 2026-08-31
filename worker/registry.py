from __future__ import annotations

from worker.models import EnrichmentAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], EnrichmentAdapter] = {}

    def register(self, adapter: EnrichmentAdapter) -> None:
        for task_type in adapter.task_types:
            key = (adapter.source, task_type)
            if key in self._adapters:
                raise ValueError(f"Adapter already registered for {key}")

            self._adapters[key] = adapter

    def get(self, source: str, task_type: str) -> EnrichmentAdapter | None:
        return self._adapters.get((source, task_type))
