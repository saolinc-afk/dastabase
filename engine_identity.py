"""Shared Dastabase enrichment engine identity; no runtime or storage side effects."""
from dataclasses import dataclass


@dataclass(frozen=True)
class EngineIdentity:
    engine_name: str
    engine_version: str


ENGINE_IDENTITY = EngineIdentity(engine_name='SPARROW', engine_version='0.9.0')
