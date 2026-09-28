"""Search is scheduled evidence acquisition, independent of website guesses."""
from discovery.domain_generator import normalize_domain

LEGAL_COMPANY_CONTACT = 'LEGAL_COMPANY_CONTACT'
LEGAL_NAME_CONTACT = 'LEGAL_NAME_CONTACT'
LEGAL_COMPANY_DATABASE = 'LEGAL_COMPANY_DATABASE'
DOMAIN_CONTACT = 'DOMAIN_CONTACT'


def queries(company, use_municipality=False):
    name = company['company_name'].strip()
    location = ' ' + company['municipality'].strip() if use_municipality and company.get('municipality') else ''
    return [(LEGAL_COMPANY_CONTACT, f'Podjetje {name}{location} kontakt'),
            (LEGAL_NAME_CONTACT, f'{name}{location} kontakt'),
            (LEGAL_COMPANY_DATABASE, f'Podjetje {name}{location} bizi.si')]


def domain_query(url):
    return DOMAIN_CONTACT, f'{normalize_domain(url)} kontakt'


class DDGSearch:
    name = 'ddgs'

    def search(self, query, max_results):
        # Only the stateless provider call is reused, not legacy search strategy.
        from discovery.search_engine import search_ddg
        return search_ddg(query, max_results=max_results)
