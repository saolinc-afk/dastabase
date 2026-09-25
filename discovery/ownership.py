"""Entity identity is not publisher ownership. Shared live/stored-evidence rules.

No network calls or host blacklist here. A scoped first-party presence can live
on a shared host; group affiliation alone never licenses root-wide attribution.
"""
import json
import posixpath
import re
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit
from discovery.domain_generator import normalize_domain, normalize_url
from discovery.domain_policy import policy_for_url, BLOCK_AS_OFFICIAL, GROUP_REVIEW

OWNERSHIP_VERSION = 'ownership-1'
LEGAL = re.compile(r'\b(?:d\s*\.\s*o\s*\.\s*o\.?|d\s*\.\s*d\.?|s\s*\.\s*p\.?|s\s*\.\s*r\s*\.\s*o\.?|gmbh|a\s*\.\s*s\.?)', re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+")


def text(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', str(value or '').lower()) if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', value).split())


def name(value):
    return text(LEGAL.split(value or '', maxsplit=1)[0])


def contains_name(value, company):
    n = name(company.get('company_name', ''))
    return len(n.replace(' ', '')) >= 3 and (' '+n+' ') in (' '+text(value)+' ')


def legal_form(value):
    match = LEGAL.search(value or '')
    return text(match.group()).replace(' ', '') if match else ''


def legal_conflict(company, value):
    target = legal_form(company.get('company_name', ''))
    other = legal_form(value)
    return bool(target and other and target != other and contains_name(value, company))


def host_brand(url, company):
    """Exact normalized brand, not parent-name substring in subsidiary name."""
    label = normalize_domain(url).split('.')[0]
    return len(name(company.get('company_name', '')).replace(' ', '')) >= 3 and text(label).replace(' ', '') == name(company.get('company_name', '')).replace(' ', '')


def canonical_path(url):
    path = unquote(unquote(urlsplit(url).path)).replace('\\', '/')
    return posixpath.normpath('/'+path.lstrip('/'))


def in_scope(url, scope):
    if not normalize_url(url) or not normalize_url(scope): return False
    if normalize_domain(url) != normalize_domain(scope): return False
    base, path = canonical_path(scope), canonical_path(url)
    return base == '/' or path == base or path.startswith(base.rstrip('/')+'/')


def scope_url(url, path='/'):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, path, '', ''))


def page_type(url, title='', visible='', signals=()):
    """Structural page/host roles, intentionally independent of entity matches."""
    path = unquote(urlsplit(url).path).lower()
    label, body = text(title), text(visible)
    # Known publisher/operator policy has precedence over incidental words such
    # as "group" in a profile or travel-guide description.
    domain_policy = policy_for_url(url)
    if domain_policy and domain_policy.policy == BLOCK_AS_OFFICIAL:
        return 'THIRD_PARTY'
    if domain_policy and domain_policy.policy == GROUP_REVIEW:
        return 'GROUP'
    structural = ('/business/', '/ddv/', '/exhibitor', 'exhibitor-search', '/searchdealer', '/prodajno-mesto/')
    strong = ('business directory', 'company directory', 'company database', 'baza podjetij',
              'poslovni imenik', 'seznam davcnih zavezancev', 'iskanje davcnih zavezancev',
              'profil kompanije', 'company listing', 'exhibitor', 'supplier report')
    if any(x in path for x in structural) or any(x in label for x in strong): return 'THIRD_PARTY'
    if any(x in body for x in ('business directory', 'company directory', 'poslovni imenik', 'baza podjetij')): return 'THIRD_PARTY'
    if any(x in label for x in ('company profile', 'seller profile', 'dealer profile', 'job portal', 'news article')): return 'PROFILE'
    if '/company/' in path or '/companies/' in path or '/dealers/' in path: return 'PROFILE'
    if any(x in path for x in ('/locations/', '/skupina/', '/lesoteka-group/', '/podjetja-v-koncernu', '/standorte')) or any(x in label for x in ('group locations', 'podjetja v koncernu')): return 'GROUP'
    if 'third_party_marketplace_page' in signals: return 'THIRD_PARTY'
    if 'third_party_profile_page' in signals or 'directory_page' in signals: return 'PROFILE'
    return 'COMPANY_PAGE'


def page_facts(company, url, title, visible, soup=None):
    """New live evidence records publisher role and explicit operator statements."""
    role = page_type(url, title, visible)
    operator = False
    publisher_scope = ''
    if soup is not None:
        # A standalone Organization about the target is NOT operator evidence.
        def walk(obj):
            nonlocal operator, publisher_scope
            if isinstance(obj, dict):
                types = obj.get('@type', [])
                if isinstance(types, str): types = [types]
                if 'WebSite' in types:
                    publisher = obj.get('publisher', {})
                    if isinstance(publisher, dict) and contains_name(publisher.get('legalName') or publisher.get('name',''), company):
                        if obj.get('url') and in_scope(url, obj['url']):
                            operator = True
                            publisher_scope = obj['url']
                for v in obj.values(): walk(v)
            elif isinstance(obj, list):
                for v in obj: walk(v)
        for script in soup.find_all('script', type='application/ld+json'):
            try: walk(json.loads(script.string or script.get_text()))
            except (ValueError, TypeError): pass
    owner_statement = bool(re.search(r'(?:website (?:is )?(?:operated|owned) by|upravljavec (?:spletne strani|spletnega mesta))\s*[:\-]?\s*'+re.escape(name(company.get('company_name','')))+r'\b', text(visible)))
    group = role == 'GROUP' or any(x in text(visible) for x in ('our subsidiaries', 'group companies', 'podjetja v skupini', 'podjetja v koncernu'))
    return {'page_type': role, 'operator_match': operator or owner_statement,
            'operator_scope': publisher_scope,
            'operator_basis': 'website_publisher_or_operator_statement' if operator or owner_statement else '',
            'group_context': group, 'legal_conflict': legal_conflict(company, title),
            'site_brand_match': contains_name(title, company), 'ownership_version': OWNERSHIP_VERSION}


def evaluate_ownership(company, pages, candidate_url='', final_url='', assessment=None, known_group=False):
    """Consume live or legacy evidence, never count repeated facts as ownership.

    An optional archived analyst assessment is a cited conservative restriction
    during offline repair, not a live company/domain-specific exception list.
    """
    pages = list(pages or [])
    final_url = final_url or (pages[0].get('page_url','') if pages else '')
    result = {'version': OWNERSHIP_VERSION, 'status': 'REVIEW', 'relationship': 'UNRESOLVED',
              'scope': '', 'reasons': [], 'entity_evidence': [], 'ownership_evidence': []}
    if assessment:
        result['archived_assessment'] = assessment
        cls = assessment.get('classification')
        if cls in ('LIKELY_THIRD_PARTY', 'UNCERTAIN'):
            result['relationship'] = 'THIRD_PARTY' if cls == 'LIKELY_THIRD_PARTY' else 'UNRESOLVED'
            result['reasons'] = [assessment.get('reason', cls)]
            return result
        known_group = known_group or cls == 'LIKELY_GROUP_OR_PARENT'
    if not pages: result['reasons']=['No saved ownership evidence']; return result
    identity = False; group = known_group; conflict = False; strong_third = False
    normalized=[]
    for p in pages:
        sig=set(p.get('entity_signals',p.get('signals',[])))
        result['entity_evidence'].append({'page_url':p.get('page_url',''), 'signals':sorted(sig-{'domain_matches_company_name'})})
        exact=bool(sig & {'tax_exact','registration_exact'})
        named=bool(p.get('name_match') or 'company_name_exact' in sig or 'organization_name_exact' in sig)
        address=bool(p.get('address_match') or 'street_address_exact' in sig)
        locality=bool(p.get('locality_match') or 'postal_locality_exact' in sig)
        identity |= (exact and (named or address and locality)) or (named and address and locality)
        facts=p.get('ownership',{})
        typ=facts.get('page_type') or page_type(p.get('page_url',''),p.get('page_title',''),signals=sig)
        strong_third |= typ=='THIRD_PARTY' or p.get('third_party',False) or 'parked_domain' in sig
        group |= typ=='GROUP' or facts.get('group_context',False)
        conflict |= facts.get('legal_conflict',False) or legal_conflict(company,p.get('page_title',''))
        normalized.append((p,facts,typ))
    if strong_third:
        result.update(relationship='THIRD_PARTY',reasons=['Directory/database/listing publisher is not the described entity']);return result
    for p,facts,typ in normalized:
        url=p.get('page_url',''); title=p.get('page_title','')
        # Dedicated matching-brand root/subdomain + branded site presentation.
        # Old domain_match flags are never reused (they allowed substrings).
        labels = normalize_domain(url).split('.')
        tenant = len(labels) >= 3 and '.'.join(labels[-2:]) not in ('co.uk','com.au','co.nz','co.za')
        dedicated = not tenant and host_brand(url,company) and contains_name(title,company)
        # A location tenant can use only the distinctive locality as hostname.
        label=text(normalize_domain(url).split('.')[0])
        company_words = name(company.get('company_name','')).split()
        title_words = text(title).split()
        local_tenant = tenant and len(label)>=4 and label in company_words and labels[-2] in company_words and all(w in title_words for w in company_words)
        operator=bool(facts.get('operator_match'))
        root=scope_url(url)
        handoff=bool(candidate_url and host_brand(candidate_url,company) and normalize_domain(candidate_url)!=normalize_domain(url) and canonical_path(url) != '/' and contains_name(title,company))
        # A provider name is not proof of a staging/tenant site's authority.
        hosting_ambiguous=any(x in normalize_domain(url).split('.') for x in ('testni','staging','preview','test'))
        if hosting_ambiguous: continue
        if typ=='PROFILE' and not (dedicated or operator or handoff): continue
        if operator or dedicated or local_tenant or handoff:
            # A redirect into a scoped microsite must not authorize the portal root.
            scope=scope_url(url, canonical_path(url).rstrip('/')+'/') if handoff or (operator and not dedicated and canonical_path(url) != '/') else root
            if operator and facts.get('operator_scope') and in_scope(url, facts['operator_scope']):
                scope = facts['operator_scope']
            if handoff and canonical_path(url).count('/')>1:
                # The exact landing subtree is safest; never guess a broader tenant.
                scope=scope_url(url,canonical_path(url).rstrip('/')+'/')
            result['ownership_evidence'].append({'page_url':url,'basis':'company_domain_handoff' if handoff else 'website_operator' if operator else 'dedicated_tenant_brand' if local_tenant else 'dedicated_domain_and_site_brand','scope':scope})
    if group:
        # An entity-specific operator statement plus an exact identifier can
        # resolve affiliation; mere group branding cannot. Archived unresolved
        # group assessments remain restrictions until new evidence is available.
        entity_operator = any(f.get('operator_match') and set(p.get('signals', [])) & {'tax_exact','registration_exact'} for p,f,t in normalized)
        if known_group or not entity_operator:
            result.update(status='GROUP_REVIEW', relationship='GROUP_PARENT',
                          source_scopes=sorted({scope_url(p.get('page_url','')) for p in pages if p.get('page_url')}),
                          reasons=['Shared parent/group representation requires entity-scoped attribution'])
            return result
    if conflict:
        result['reasons']=['Conflicting legal entity/form; continuity not established'];return result
    if identity and result['ownership_evidence']:
        scope=result['ownership_evidence'][0]['scope']
        result.update(status='VERIFIED',relationship='LEGAL_ENTITY',scope=scope,reasons=['Entity identity and distinct site-ownership evidence both present'])
    else: result['reasons']=['Entity facts alone are insufficient; site operator/scope not established']
    return result


def entity_context(company, value):
    # An address such as ``info@alfa.si`` is not a textual identification of
    # ALFA.  Remove addresses before looking for a company name so same-domain
    # mail never becomes its own attribution proof.
    prose = EMAIL_RE.sub(' ', value or '')
    if contains_name(prose,company): return True
    for key in ('tax_number','registration_number'):
        v=re.sub(r'\D','',str(company.get(key) or ''))
        if len(v)>=7 and re.search(r'(?<!\d)(?:SI\s*)?'+re.escape(v)+r'(?!\d)', value or '',re.I): return True
    return False


def email_attribution(company, email, source_url, publication, ownership, pages=(), page_title='', block=None, visible_email=''):
    """Same-domain membership is only supporting evidence, never the decision."""
    result={'version':OWNERSHIP_VERSION,'attributable':False,'source_url':source_url,'entity_id':company.get('id')}
    status=ownership.get('status'); scope=ownership.get('scope','')
    if visible_email and visible_email.lower()!=email.lower():
        return {**result,'reason':'Displayed email and link target disagree'}
    if status not in ('VERIFIED','GROUP_REVIEW'):
        return {**result,'reason':'Website ownership unresolved or third-party'}
    if page_type(source_url,page_title)=='THIRD_PARTY':
        return {**result,'reason':'Publisher/listing email source'}
    # An explicit block is supplied only from a bounded DOM contact section.
    explicit=entity_context(company,publication) and not legal_conflict(company,publication)
    if block is not None:
        explicit=bool(block.get('target_entity') and not block.get('other_entity'))
        if block.get('other_entity'): return {**result,'reason':'Contact block belongs to another entity'}
    if status=='GROUP_REVIEW':
        if not any(in_scope(source_url, s) for s in ownership.get('source_scopes', [])):
            return {**result,'reason':'Email source outside evidenced group site'}
        if any(x in text(publication) for x in ('website by','developed by','designed by','izdelava splet','powered by')):
            return {**result,'reason':'Agency/vendor publication context'}
        return {**result,'attributable':explicit,'reason':'Explicit target-entity contact block' if explicit else 'Group email lacks an explicit target-entity association'}
    if not scope or not in_scope(source_url,scope):
        return {**result,'reason':'Email source outside verified tenant/path scope'}
    if any(x in text(publication) for x in ('website by','developed by','designed by','izdelava splet','powered by')):
        return {**result,'reason':'Agency/vendor publication context'}
    if legal_conflict(company,publication): return {**result,'reason':'Different legal entity in contact context'}
    # Legacy page signals can corroborate a source only at that exact URL.
    exact_page=any(p.get('page_url','').rstrip('/')==source_url.rstrip('/') and (p.get('name_match') or 'company_name_exact' in p.get('signals',[])) and not p.get('third_party') for p in pages)
    same_mail_host=normalize_domain(email.rsplit('@',1)[-1])==normalize_domain(scope)
    accepted=explicit or (same_mail_host and (exact_page or (block or {}).get('single_entity_page',False)))
    return {**result,'attributable':accepted,'scope':scope,'reason':'Target entity contact context on verified scope' if accepted else 'Same-domain email without sufficient source-entity context'}
