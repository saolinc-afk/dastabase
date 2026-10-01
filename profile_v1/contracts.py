"""Provider-neutral structured interpretation contract."""
from dataclasses import dataclass, field
from typing import Any, Protocol

CLAIM_TYPES = frozenset({
    'ACTUAL_PRIMARY_ACTIVITY', 'INDUSTRY_CATEGORY', 'PRODUCTS', 'SERVICES',
    'BUSINESS_AUDIENCE', 'CUSTOMER_TYPES', 'MANUFACTURER_SIGNAL',
    'INTERNATIONAL_SIGNAL', 'BUSINESS_DESCRIPTION',
})
ENUMS = {
    'BUSINESS_AUDIENCE': frozenset({'B2B', 'B2C', 'MIXED', 'UNKNOWN'}),
    'MANUFACTURER_SIGNAL': frozenset({'YES', 'NO', 'UNKNOWN'}),
    'INTERNATIONAL_SIGNAL': frozenset({'YES', 'NO', 'UNKNOWN'}),
}


@dataclass(frozen=True)
class Citation:
    block_id: str
    quote: str


@dataclass(frozen=True)
class CandidateClaim:
    claim_type: str
    normalized_value: Any
    display_value: str
    citations: tuple[Citation, ...]
    confidence: str = 'MEDIUM'
    ambiguity_note: str | None = None


class Interpreter(Protocol):
    version: str
    def interpret(self, company: dict, blocks: list[dict]) -> list[CandidateClaim]: ...


class EmptyInterpreter:
    version = 'empty-interpreter-1'
    def interpret(self, company, blocks):
        return []


class JsonInterpreter:
    """Deterministic fixture/manual interpreter; never calls an external service."""
    version = 'json-interpreter-1'
    def __init__(self, candidates_by_company):
        self.candidates = candidates_by_company

    def interpret(self, company, blocks):
        rows = self.candidates.get(str(company['id']), self.candidates.get(company['id'], []))
        return [CandidateClaim(
            claim_type=row['claim_type'], normalized_value=row.get('normalized_value'),
            display_value=row.get('display_value', ''), confidence=row.get('confidence', 'MEDIUM'),
            ambiguity_note=row.get('ambiguity_note'), citations=tuple(
                Citation(c['block_id'], c['quote']) for c in row.get('citations', [])))
            for row in rows]
