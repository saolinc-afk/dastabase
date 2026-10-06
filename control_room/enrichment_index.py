"""Read-only canonical and accepted-enrichment index for Import & Enrich."""
from collections import defaultdict

from discovery.domain_generator import normalize_domain
from discovery_v2.evidence import public_email_domain

from control_room.matching import (normalize_email, normalize_name, normalize_phone,
                                   normalize_registration, normalize_tax, normalize_text,
                                   canonical_name_aliases)
from control_room.knowledge_repository import KnowledgeRepository


def _domain(value):
    return normalize_domain(value or '')


class EnrichmentIndex:
    """Immutable in-memory projection; every SQLite input is opened read-only."""

    def __init__(self, canonical_path, discovery_results=(), *, precedence='newest',
                 run_ids=None, knowledge=None):
        if precedence not in ('newest', 'input-order'):
            raise ValueError('precedence must be newest or input-order')
        self.knowledge = knowledge or KnowledgeRepository(canonical_path, discovery_results,
                                                          precedence=precedence, run_ids=run_ids)
        self.canonical_path, self.companies = (self.knowledge.canonical_path,
                                               self.knowledge.companies)
        self.discovery_results = self.knowledge.discovery_results
        self.by_id = {company['id']: company for company in self.companies}
        self.enrichment = self.knowledge.as_enrichment()
        self.historical_domain_candidates = self.knowledge.historical_domain_candidates
        self.historical_email_candidates = self.knowledge.historical_email_candidates
        self._build_indexes()

    @staticmethod
    def _add(index, value, company_id):
        if value:
            index[value].add(company_id)

    def _build_indexes(self):
        self.tax = defaultdict(set)
        self.registration = defaultdict(set)
        self.names = defaultdict(set)
        self.addresses = defaultdict(set)
        self.municipalities = defaultdict(set)
        self.domains = defaultdict(set)
        self.emails = defaultdict(set)
        self.phones = defaultdict(set)
        self.aliases = defaultdict(set)
        self.alias_rules = defaultdict(set)
        for company in self.companies:
            company_id = company['id']
            self._add(self.tax, normalize_tax(company['tax_number']), company_id)
            self._add(self.registration, normalize_registration(company['registration_number']), company_id)
            self._add(self.names, normalize_name(company['company_name']), company_id)
            for alias, rule, preserved in canonical_name_aliases(company['company_name']):
                if alias != normalize_name(company['company_name']):
                    self._add(self.aliases, alias, company_id)
                    self.alias_rules[(alias, company_id)].add((rule, preserved))
            self._add(self.addresses, normalize_text(company['address']), company_id)
            self._add(self.municipalities, normalize_text(company['municipality']), company_id)
            enrichment = self.enrichment[company_id]
            website_domain = _domain(enrichment.get('website'))
            self._add(self.domains, website_domain, company_id)
            for item in enrichment.get('accepted_emails', []):
                email = normalize_email(item['value'])
                if not email:
                    continue
                domain = email.rsplit('@', 1)[1]
                if public_email_domain(domain):
                    continue
                self._add(self.emails, email, company_id)
                # An explicitly attributed external mailbox is useful exact-email
                # evidence, but its provider domain does not identify the company.
                if domain == website_domain:
                    self._add(self.domains, domain, company_id)
            self._add(self.phones, normalize_phone(enrichment.get('default_phone')), company_id)

    def company(self, company_id):
        company = self.by_id[company_id]
        return {**company, 'enrichment': self.enrichment[company_id]}

    def has_reusable_enrichment(self, company_id):
        record = self.enrichment[company_id]
        return bool(record.get('website') or record.get('default_email') or
                    record.get('default_phone'))
