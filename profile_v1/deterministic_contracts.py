"""Stable contracts for deterministic Profile facts and their provenance."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DeterministicEvidence:
    profile_evidence_id: str | None
    block_id: str | None = None
    page_url: str | None = None
    source_locator: str | None = None
    exact_quote: str | None = None
    metadata_value: str | None = None


@dataclass(frozen=True)
class DeterministicFact:
    field_name: str
    value: Any
    state: str
    confidence: str
    rule_id: str
    evidence: tuple[DeterministicEvidence, ...] = ()


@dataclass(frozen=True)
class DeterministicResult:
    corpus_hash: str
    corpus_status: str
    facts: tuple[DeterministicFact, ...]
