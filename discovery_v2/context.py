"""Visible, bounded contact context; publisher identity never licenses every contact."""
import re
from bs4 import BeautifulSoup, Comment
from discovery.ownership import entity_context, legal_conflict, legal_form, contains_name, text, name

OTHER = ('distribut', 'reseller', 'partner', 'supervisory', 'regulator', 'nadzorni',
         'informacijski pooblasc', 'website by', 'developed by', 'designed by',
         'izdelava splet', 'powered by', 'foreign branch', 'podruznica')
CONTACT = ('kontakt', 'contact', 'poklicite', 'pisite nam', 'company info', 'podatki o podjetju', 'trgovina')


def visible_soup(html):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.find_all(string=lambda s: isinstance(s, Comment)):
        node.extract()
    for node in list(soup.find_all(True)):
        if node.parent is None:
            continue
        style = re.sub(r'\s+', '', node.get('style', '').lower())
        if node.name in ('script', 'style', 'noscript', 'template', 'head') or node.has_attr('hidden') or node.get('aria-hidden') == 'true' or 'display:none' in style or 'visibility:hidden' in style:
            node.decompose()
    return soup


def contact_context(node, company, soup):
    block = node.parent
    # Stop at a small semantic unit, never an arbitrary ancestor containing headings.
    for parent in node.parents:
        if parent.name in ('body', 'html', 'main'):
            break
        content = parent.get_text(' ', strip=True)
        if len(content) > 1200:
            break
        block = parent
        if parent.name in ('p', 'li', 'address', 'article', 'section', 'footer') or (parent.name == 'div' and len(content) <= 500) or any('card' in c for c in parent.get('class', [])):
            break
    publication = block.get_text(' ', strip=True) if block else str(node)
    heading = ''
    if block:
        preceding = node.find_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        if preceding:
            heading = preceding.get_text(' ', strip=True)
    # Context is centred on the contact node; never truncate away its value.
    if len(publication) > 1600:
        publication = node.get_text(' ', strip=True) if hasattr(node, 'get_text') else str(node)
    label = text(heading + ' ' + publication)
    ancestry = ' '.join(' '.join(p.get('class', [])) + ' ' + p.get('id', '') for p in node.parents if p.name not in ('body', 'html'))
    ancestor_headings = ' '.join(h.get_text(' ', strip=True) for p in node.parents if p.name in ('section', 'article') for h in p.find_all(['h2', 'h3', 'h4', 'h5', 'h6'], recursive=False))
    boundary_label = text(heading + ' ' + ancestry + ' ' + ancestor_headings)
    foreign = any(word in boundary_label for word in OTHER)
    foreign |= any(word in text(publication) for word in OTHER if word not in ('partner', 'distribut', 'reseller'))
    foreign |= bool(legal_form(heading) and sorted(name(heading).split()) != sorted(name(company['company_name']).split()))
    page_identity = entity_context(company, soup.get_text(' ', strip=True))
    target = entity_context(company, publication + ' ' + heading) and not legal_conflict(company, publication)
    return publication, dict(target_entity=target, other_entity=foreign,
        single_entity_page=page_identity, contact_section=any(w in label for w in CONTACT),
        headings=[heading] if heading else [], explicit_company=target)


def entity_specific(company, publication):
    """A search mention must identify the legal entity, not just a brand substring."""
    normalized = ' ' + text(publication) + ' '
    legal = text(company.get('company_name', ''))
    if legal and ' ' + legal + ' ' in normalized:
        return True
    for field in ('tax_number', 'registration_number'):
        identifier = re.sub(r'\D', '', str(company.get(field) or ''))
        if len(identifier) >= 7 and re.search(r'(?<!\d)(?:SI\s*)?' + re.escape(identifier) + r'(?!\d)', publication, re.I):
            return True
    return False
