"""Small explicit module, execution and dependency contracts."""
from dataclasses import asdict, dataclass
from typing import Protocol

JOB_TYPE = "DISCOVERY_CONTACTS"
EXECUTION_MODE = "FRESH_DISCOVERY"


@dataclass(frozen=True)
class Config:
    results_per_query: int = 10
    max_candidates: int = 8
    max_http_requests: int = 36
    max_contact_pages: int = 8
    use_municipality: bool = False
    max_search_queries_per_company: int | None = None

    def __post_init__(self):
        for key, limit in (("results_per_query", 20), ("max_candidates", 30),
                           ("max_http_requests", 100), ("max_contact_pages", 20)):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= limit:
                raise ValueError(f"{key} must be between 1 and {limit}")
        if type(self.use_municipality) is not bool:
            raise ValueError("use_municipality must be boolean")
        if (self.max_search_queries_per_company is not None
                and (type(self.max_search_queries_per_company) is not int
                     or self.max_search_queries_per_company < 1)):
            raise ValueError("max_search_queries_per_company must be a positive integer")

    def as_dict(self):
        return asdict(self)


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, max_results: int): ...


class PageFetcher(Protocol):
    requests: int
    errors: list

    def fetch(self, url: str, allowed_site: str | None = None): ...
    def close(self): ...
