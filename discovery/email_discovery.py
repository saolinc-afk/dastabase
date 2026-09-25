"""Published email extraction from VERIFIED websites; no address invention."""
import re
from urllib.parse import urljoin, unquote, urlsplit
from bs4 import BeautifulSoup
from discovery.domain_generator import normalize_domain, normalize_url, same_site
from discovery.website_verifier import Fetcher, blocked_url, internal_identity_links, priority, marketplace_profile, score_page

from discovery.ownership import (in_scope, email_attribution, entity_context, contains_name,
                                 legal_form, legal_conflict, page_type, evaluate_ownership)

MAX_PAGES = 8
EMAIL_REGEX = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}")
IGNORE_EMAILS = ('example.com', 'example.org', 'noreply', 'no-reply', 'donotreply', 'sentry.io', 'wixpress.com')
CREDIT_WORDS = ('website by', 'developed by', 'designed by', 'izdelava splet', 'spletno stran izdelal', 'powered by')


def extract_emails(text):
    return {m.lower().rstrip('.') for m in EMAIL_REGEX.findall(unquote(text or ''))
            if not any(word in m.lower() for word in IGNORE_EMAILS) and not m.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.svg'))}


def mailto_emails(href):
    if not href.lower().startswith('mailto:'): return set()
    # Recipient list only: ignore subject/body/cc query content.
    return extract_emails(href[7:].split('?', 1)[0])


def email_on_company_domain(email, website):
    domain = email.rsplit('@', 1)[-1].lower()
    company_domain = normalize_domain(website)
    return bool(company_domain) and (domain == company_domain or domain.endswith('.' + company_domain))


def score_email(email, page_url, website, found_in='text'):
    score = 55
    if email_on_company_domain(email, website): score += 20
    if priority(page_url) == 0: score += 10
    if email.split('@')[0].startswith(('info', 'office', 'kontakt', 'contact', 'prodaja', 'sales')): score += 10
    if found_in == 'mailto': score += 5
    return min(score, 100)


def contact_context(node, company, soup):
    """Bounded semantic contact block, never the whole multi-entity document."""
    block = node.parent
    for parent in node.parents:
        if parent.name in ('body', 'html'): break
        if parent.name in ('section', 'article', 'address', 'footer'):
            block = parent; break
        if parent.name == 'div' and parent.find(['h2', 'h3', 'h4']):
            block = parent; break
    publication = block.get_text(' ', strip=True)[:2000] if block else str(node)
    headings = [h.get_text(' ', strip=True) for h in block.find_all(['h1','h2','h3','h4'])] if block else []
    other = any(legal_form(h) and not contains_name(h,company) for h in headings)
    target = entity_context(company, publication) and not legal_conflict(company, publication)
    # Source-page identity may support an unlabelled contact on a single-entity
    # official site. Foreign entity cards and vendor credits are excluded.
    all_headings = [h.get_text(' ',strip=True) for h in soup.find_all(['h1','h2','h3','h4'])]
    multi = any(legal_form(h) and not contains_name(h,company) for h in all_headings)
    return publication, {'target_entity': target, 'other_entity': other,
                         'single_entity_page': entity_context(company, soup.get_text(' ',strip=True)) and not multi,
                         'headings': headings}


def crawl_site(company, fetcher=None):
    if company.get('status') != 'VERIFIED':
        raise ValueError('Email discovery requires a VERIFIED company website')
    owner = company.get('ownership') or {}
    if owner.get('status') != 'VERIFIED' or not owner.get('scope'):
        raise ValueError('Email discovery requires explicit verified ownership and scope')
    scope = owner['scope']
    start = normalize_url(company['website'])
    if blocked_url(start) or not in_scope(start, scope):
        raise ValueError('Email discovery cannot use a blocked or out-of-scope website')
    owned = fetcher is None
    fetcher = fetcher or Fetcher()
    contact = normalize_url(company.get('contact_page',''))
    queue = [u for u in (contact, start) if u and in_scope(u, scope)]
    visited, found, rejected = set(), {}, []
    successful = 0; start_errors = len(fetcher.errors)
    try:
        while queue and len(visited) < MAX_PAGES:
            url = queue.pop(0)
            if url in visited or not in_scope(url,scope): continue
            visited.add(url)
            response = fetcher.fetch(url, allowed_site=scope)
            if response is None or not in_scope(response.url,scope): continue
            visited.add(response.url)
            signals = score_page(company,response)
            if signals.get('third_party') or signals.get('ownership',{}).get('legal_conflict'):
                fetcher.errors.append(f'Third-party/profile or conflicting email source rejected: {response.url}')
                continue
            soup = BeautifulSoup(response.text,'html.parser')
            for node in soup(['script','style','noscript','template']): node.decompose()
            title = soup.title.get_text(' ',strip=True) if soup.title else ''
            successful += 1
            candidates=[]
            for a in soup.find_all('a',href=True):
                pub, block = contact_context(a,company,soup)
                displayed=extract_emails(a.get_text(' ',strip=True))
                for email in mailto_emails(a['href']):
                    candidates.append((email,'mailto',pub,block,next(iter(displayed)) if len(displayed)==1 else ''))
            for node in soup.find_all(string=True):
                for email in extract_emails(str(node)):
                    # A displayed address different from its href is handled as
                    # one conflicting publication, not recovered via text scan.
                    parent=node.find_parent('a')
                    if parent and parent.get('href','').lower().startswith('mailto:'): continue
                    pub,block=contact_context(node,company,soup)
                    candidates.append((email,'text',pub,block,''))
            for email,method,pub,block,displayed in candidates:
                attribution=email_attribution(company,email,response.url,pub,owner,[signals],title,block,displayed)
                if not attribution['attributable']:
                    rejected.append({'email':email,'page_url':response.url,'reason':attribution['reason']});continue
                candidate={'email':email,'confidence':score_email(email,response.url,scope,method),
                           'website':scope,'page_url':response.url,'page_title':title,'found_in':method,
                           'evidence':{'publication':pub,'contact_block':block,'attribution':attribution,
                                       'visible_email':displayed,'ownership_scope':scope,
                                       'verification':'Published address only; deliverability not tested'}}
                if email not in found or candidate['confidence']>found[email]['confidence']: found[email]=candidate
            queue.extend(u for u in internal_identity_links(response.url,soup) if in_scope(u,scope) and u not in visited and u not in queue)
        return {'emails':sorted(found.values(),key=lambda x:(-x['confidence'],x['email'])),
                'rejected':sorted({(r['email'],r['page_url'],r['reason']) for r in rejected}),
                'pages_attempted':sorted(visited),'pages_fetched':successful,'errors':fetcher.errors[start_errors:],
                'status':'DONE' if found else 'ERROR' if successful==0 or fetcher.errors[start_errors:] else 'NO_EMAIL'}
    finally:
        if owned: fetcher.close()


def main():
    raise SystemExit('Use the bounded runner: python -m discovery.runner --limit 20')


if __name__ == '__main__':
    main()
