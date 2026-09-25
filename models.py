from dataclasses import dataclass
from typing import Optional

@dataclass
class Company:
    company_name: str
    address: str
    registration_number: str
    tax_number: str
    activity: str
    phone: str
    website: str
    email: str

@dataclass
class WebsiteCache:
    company_id: int
    url: str
    cache_file: str
    http_status: int
    page_title: str
    content_type: str
    crawl_duration_ms: int
    crawled_at: str

@dataclass
class CompanyEnrichment:
    company_id: int

    website_url: Optional[str] = None
    website_title: Optional[str] = None
    website_description: Optional[str] = None

    languages: Optional[str] = None

    export_score: Optional[int] = None
    export_reason: Optional[str] = None

    family_score: Optional[int] = None
    family_reason: Optional[str] = None

    industry_keywords: Optional[str] = None

    ai_summary: Optional[str] = None
    ai_sales_angle: Optional[str] = None

    linkedin: Optional[str] = None
    facebook: Optional[str] = None
    instagram: Optional[str] = None
    youtube: Optional[str] = None

    company_size: Optional[str] = None
    revenue_per_employee: Optional[float] = None

    enrichment_version: Optional[str] = None

    last_enriched: Optional[str] = None