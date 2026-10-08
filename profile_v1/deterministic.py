"""Network-free deterministic interpretation of the frozen Profile corpus."""
import hashlib, json, re, unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from profile_v1 import DETERMINISTIC_RULE_VERSION
from profile_v1.deterministic_contracts import (
    DeterministicEvidence, DeterministicFact, DeterministicResult)
from profile_v1.store import encode, now, uid


RICH_TEXT_CHARS = 160
ORGANIZATION_TYPES = {
    'Organization','Corporation','LocalBusiness','ProfessionalService','Store',
    'Hotel','Restaurant','EducationalOrganization','GovernmentOrganization',
}
RELATIONAL_NOISE = re.compile(
    r'\b(customer|client|reference|partner|supplier|vendor|case study|job|career|vacanc(?:y|ies)|'
    r'stranka|referenc[ae]|partner|dobavitelj|zaposlitev|kariera|kunde|referenz|partner|'
    r'cliente|referenza|fornitore|partner|kupac|referenca|dobavljač)\b', re.I)
CONTEXT_NOISE = re.compile(
    RELATIONAL_NOISE.pattern+r'|\b(?:privacy|cookie|legal|terms|policy|politika zasebnosti|'
    r'datenschutz|impressum|informativa sulla privacy)\b',re.I)
TESTIMONIAL_CONTEXT = re.compile(
    r'\b(?:testimonials?|customer quotes?|customer reviews?|client reviews?|what our customers say|'
    r'customer stories|client stories|success stor(?:y|ies))\b',re.I)
TESTIMONIAL_PATH = re.compile(
    r'/(?:testimonials?|reviews?|customer-stor(?:y|ies)|client-stor(?:y|ies)|'
    r'success-stor(?:y|ies))(?:/|$)',re.I)
QUOTED_ATTRIBUTION = re.compile(r'^\s*[“\"].+[”\"]\s*[—–-]',re.I)
NOISY_PATH = re.compile(
    r'/(?:news|blog|novice|references?|projekti|suppliers?|vendors?|customers?|clients?|'
    r'case-stud(?:y|ies)|jobs?|careers?|kariera|zaposlitve|privacy|cookie|legal|terms|'
    r'datenschutz|impressum|stellenangebote|lavora-con-noi)(?:/|$)',re.I)
MANUFACTURER = re.compile(
    r'\b(?:we manufacture|our production facility manufactures|smo proizvajal(?:ec|ci)|'
    r'proizvajamo|wir fertigen|proizvodimo)\b', re.I)
PRODUCE = re.compile(
    r'\b(?:we produce|our (?:factory|plant|production facility) produces|wir produzieren|produciamo)\b',re.I)
PHYSICAL_MANUFACTURING_OBJECT = re.compile(
    r'\b(?:aluminium|aluminum|steel|metal|wood|plastic|components?|parts?|equipment|machinery|'
    r'devices?|materials?|goods|physical products?|pumps?|bearings?|valves?|assemblies|tools?)\b',re.I)
INTANGIBLE_OUTPUT = re.compile(
    r'\b(?:content|reports?|stud(?:y|ies)|media|videos?|films?|software|data|events?|'
    r'documentation|analysis|analyses|research|podcasts?|newsletters?|presentations?|services?|'
    r'consulting|consultancy|advisory|optimization|support|training|maintenance|installation|'
    r'strateg(?:y|ies))\b',re.I)
EXPORT_ACTION = re.compile(
    r'\b(?:we export|izvažamo|wir exportieren|esportiamo|izvozimo)\b',re.I)
EXPORT_DESTINATION = re.compile(
    r'\b(?:\d+\s+countries|foreign (?:markets?|customers?)|international (?:markets?|customers?)|'
    r'customers? (?:abroad|overseas)|across europe|throughout europe|to europe|'
    r'worldwide markets?|overseas markets?|export markets?)\b',re.I)
COMMERCIAL_EXPORT_OBJECT = re.compile(
    r'\b(?:products?|goods|services?|components?|parts?|equipment|machinery|devices?|materials?|'
    r'merchandise|production)\b',re.I)
NONCOMMERCIAL_EXPORT = re.compile(
    r'\b(?:data|files?|csv|reports?|software|settings|configuration|documents?|documentation|'
    r'records?|spreadsheets?|media|videos?)\b',re.I)
EXPORT_SHARE = re.compile(r'\b\d{1,3}% of (?:our )?production is exported\b',re.I)
INTERNATIONAL_SUPPLY = re.compile(
    r'\bwe supply (?:customers|clients) (?:across|throughout|in) europe\b',re.I)
B2B = re.compile(r'\b(?:b2b|business[- ]to[- ]business|za podjetja|poslovnim kupcem)\b',re.I)
B2C = re.compile(r'\b(?:b2c|business[- ]to[- ]consumer|za potrošnike|končnim kupcem)\b',re.I)
PRODUCT_PATH = re.compile(r'/(?:products?|izdelki|proizvodi|produkte|prodotti)(?:/|$)',re.I)
PRODUCT_HEADING = re.compile(r'\b(?:products?|izdelki|proizvodi|produkte|prodotti)\b',re.I)
SERVICE_PATH = re.compile(r'/(?:services?|storitve|leistungen|servizi|usluge)(?:/|$)',re.I)
SERVICE_HEADING = re.compile(r'\b(?:services?|storitve|leistungen|servizi|usluge)\b',re.I)
NEGATION = re.compile(
    r"\b(?:not|never|no|neither|without|do not|does not|don't|is not|isn't|aren't|"
    r"ne|ni|nismo|brez|nicht|kein|keine|non|senza|nije|bez)\b",re.I)


def _identity(value):
    text=unicodedata.normalize('NFKD',str(value or '')).casefold()
    return ''.join(char for char in text if char.isalnum())


def _language(value):
    value=str(value or '').strip().replace('_','-').lower()
    return value if value and value!='x-default' else None


def _page_url(evidence): return evidence.get('final_url') or evidence.get('requested_url')


def _ref(evidence,block=None,locator=None,quote=None,value=None):
    return DeterministicEvidence(evidence['evidence_id'],block['block_id'] if block else None,
      _page_url(evidence),locator or (block['source_locator'] if block else None),quote,value)


def _unknown(field,rule,value=None):
    return DeterministicFact(field,value,'UNKNOWN','UNKNOWN',rule)


def _corpus_hash(evidence,blocks):
    frozen=[]
    for row in evidence:
        frozen.append({'kind':row['source_kind'],'class':row['source_class'],
          'requested_url':row['requested_url'],'final_url':row['final_url'],
          'content_hash':row['content_hash'],'origin_evidence_id':row['origin_evidence_id']})
    block_values=[{'evidence_origin':row['origin_evidence_id'],'block_type':row['block_type'],
      'source_locator':row['source_locator'],'text_hash':row['text_hash'],'text':row['text']}
      for row in blocks]
    payload={'evidence':sorted(frozen,key=encode),'blocks':sorted(block_values,key=encode)}
    return hashlib.sha256(encode(payload).encode()).hexdigest()


def _status(evidence,blocks):
    first={row['evidence_id'] for row in evidence if row['source_class']=='FIRST_PARTY'}
    if first:
        substantive=[row for row in blocks if row['evidence_id'] in first and
          row['block_type'] in {'HEADING','PARAGRAPH','LIST_ITEM'}]
        chars=sum(len(row['text']) for row in substantive)
        return 'RICH_FIRST_PARTY' if len(substantive)>=2 and chars>=RICH_TEXT_CHARS else 'THIN_FIRST_PARTY'
    if any(row['source_class']=='CANDIDATE' for row in evidence): return 'CANDIDATE_ONLY'
    if any(row['source_class']=='SEARCH_SNIPPET' for row in evidence): return 'SEARCH_ONLY'
    return 'NO_EVIDENCE'


def _pages(evidence):
    pages=[]
    for row in evidence:
        if row['source_class']!='FIRST_PARTY' or not row['html']: continue
        key=row['content_hash'] or hashlib.sha256(row['html'].encode()).hexdigest()
        try: soup=BeautifulSoup(row['html'],'html.parser')
        except Exception: continue
        url=_page_url(row) or ''
        parsed=urlsplit(url)
        pages.append((len([x for x in parsed.path.split('/') if x]),
          0 if parsed.scheme=='https' else 1,url,key,row,soup))
    output=[]; seen=set()
    for _,_,_,key,row,soup in sorted(pages,key=lambda item:item[:4]):
        if key in seen: continue
        seen.add(key); output.append((row,soup))
    return output


def _meta(soup,key,attribute='name'):
    wanted=key.casefold()
    for node in soup.find_all('meta'):
        if str(node.get(attribute) or '').casefold()==wanted:
            value=node.get('content')
            if value is not None and str(value).strip(): return str(value).strip()
    return None


def _metadata(pages,blocks_by_locator):
    records=[]; refs=[]; titles=[]; descriptions=[]; language_values={}; language_refs=[]
    for evidence,soup in pages:
        page={'page_url':_page_url(evidence)}; page_refs=[]
        title=soup.title.get_text(' ',strip=True) if soup.title else evidence.get('title')
        description=_meta(soup,'description')
        canonical=None
        for link in soup.find_all('link'):
            rel=[str(value).casefold() for value in (link.get('rel') or [])]
            if 'canonical' in rel and link.get('href'): canonical=str(link['href']).strip(); break
        values=(('title',title,'title'),('meta_description',description,'meta[name=description]'),
          ('canonical_url',canonical,'link[rel=canonical]'),
          ('og_title',_meta(soup,'og:title','property'),'meta[property=og:title]'),
          ('og_description',_meta(soup,'og:description','property'),'meta[property=og:description]'),
          ('og_site_name',_meta(soup,'og:site_name','property'),'meta[property=og:site_name]'),
          ('og_locale',_meta(soup,'og:locale','property'),'meta[property=og:locale]'))
        for key,value,locator in values:
            if value:
                block=blocks_by_locator.get((evidence['evidence_id'],locator))
                page[key]=value; reference=_ref(evidence,block=block,locator=locator,value=value)
                page_refs.append(reference)
                if key=='title': titles.append((value,reference))
                elif key=='meta_description': descriptions.append((value,reference))
        html_lang=soup.html.get('lang') if soup.html else None
        if html_lang:
            page['html_lang']=str(html_lang).strip(); normalized=_language(html_lang)
            if normalized:
                reference=_ref(evidence,locator='html[lang]',value=str(html_lang).strip())
                language_values.setdefault(normalized,None); language_refs.append(reference); page_refs.append(reference)
        hreflang=[]
        for link in soup.find_all('link'):
            raw=link.get('hreflang'); normalized=_language(raw)
            if not normalized: continue
            item={'language':str(raw).strip()}
            if link.get('href'): item['href']=str(link['href']).strip()
            hreflang.append(item); reference=_ref(evidence,locator='link[hreflang]',value=str(raw).strip())
            language_values.setdefault(normalized,None); language_refs.append(reference); page_refs.append(reference)
        if hreflang: page['hreflang']=sorted(hreflang,key=encode)
        locale=page.get('og_locale'); normalized=_language(locale)
        if normalized:
            language_values.setdefault(normalized,None)
            reference=_ref(evidence,locator='meta[property=og:locale]',value=locale)
            language_refs.append(reference)
        if len(page)>1: records.append(page); refs.extend(page_refs)
    return records,refs,titles,descriptions,sorted(language_values),language_refs


def _objects(value):
    if isinstance(value,list):
        for item in value: yield from _objects(item)
    elif isinstance(value,dict):
        yield value
        for nested in value.values():
            if isinstance(nested,(dict,list)): yield from _objects(nested)


def _types(value):
    value=value.get('@type') if isinstance(value,dict) else None
    return [value] if isinstance(value,str) else [x for x in value or [] if isinstance(x,str)]


def _identifier_values(value):
    values=[]
    for key in ('taxID','vatID','identifier'):
        item=value.get(key)
        if isinstance(item,(str,int)): values.append(str(item))
        elif isinstance(item,dict) and isinstance(item.get('value'),(str,int)): values.append(str(item['value']))
    return values


def _match_organization(value,company):
    names=[value.get('legalName'),value.get('name')]
    target=_identity(company.get('company_name'))
    if target and any(_identity(name)==target for name in names if name): return 'EXACT_NAME'
    identifiers={_identity(company.get('tax_number')),_identity(company.get('registration_number'))}-{''}
    if identifiers and identifiers.intersection(_identity(item) for item in _identifier_values(value)):
        return 'EXACT_IDENTIFIER'
    return 'OTHER' if any(names) or _identifier_values(value) else 'UNKNOWN'


def _target_heading(text,company):
    generic={'doo','dno','dd','ltd','limited','llc','inc','corp','corporation','company','gmbh','spa'}
    tokens=[token for token in re.findall(r'\w+',str(company.get('company_name') or '').casefold())
      if len(token)>=3 and token not in generic]
    normalized=' '.join(str(text or '').casefold().split())
    for token in tokens:
        escaped=re.escape(token)
        if (re.fullmatch(rf'what {escaped} does',normalized) or
                re.fullmatch(rf'about {escaped}',normalized) or
                re.fullmatch(rf'{escaped}(?:[’\']s)? (?:company|products?|services?|manufacturing|'
                  r'operations|capabilities)',normalized) or
                re.fullmatch(rf'{escaped} at a glance',normalized)):
            return True
    return False


def _heading_level(block):
    match=re.match(r'h([1-6])\b',str(block.get('source_locator') or ''),re.I)
    return int(match.group(1)) if match else None


def _noise_contexts(blocks,evidence_by_id,company):
    noisy={evidence_id for evidence_id,evidence in evidence_by_id.items()
      if NOISY_PATH.search(urlsplit(_page_url(evidence) or '').path)}
    testimonial_pages={evidence_id for evidence_id,evidence in evidence_by_id.items()
      if TESTIMONIAL_PATH.search(urlsplit(_page_url(evidence) or '').path)}
    testimonial_pages.update(block['evidence_id'] for block in blocks
      if block['block_type']=='TITLE' and TESTIMONIAL_CONTEXT.search(block['text']))
    noisy_blocks=set(); active={evidence_id:evidence_id in testimonial_pages for evidence_id in evidence_by_id}
    section_level={evidence_id:None for evidence_id in evidence_by_id}
    for block in blocks:
        evidence_id=block['evidence_id']
        if block['block_type'] in {'TITLE','HEADING'}:
            if CONTEXT_NOISE.search(block['text']) or TESTIMONIAL_CONTEXT.search(block['text']):
                active[evidence_id]=True
                if block['block_type']=='HEADING': section_level[evidence_id]=_heading_level(block)
            elif block['block_type']=='HEADING' and active.get(evidence_id,False) and (
                    evidence_id not in testimonial_pages and _target_heading(block['text'],company) and
                    (section_level.get(evidence_id) is None or
                     (_heading_level(block) is not None and
                      _heading_level(block)<=section_level[evidence_id]))):
                active[evidence_id]=False
                section_level[evidence_id]=None
        if active.get(evidence_id,False) or QUOTED_ATTRIBUTION.search(block['text']):
            noisy_blocks.add(block['block_id'])
    return noisy,noisy_blocks


def _structured(blocks,evidence_by_id,company,noisy):
    organizations=[]; org_refs=[]; product_refs=[]; service_refs=[]; manufacturer_refs=[]
    for block in blocks:
        if block['block_type']!='STRUCTURED_DATA': continue
        evidence=evidence_by_id.get(block['evidence_id'])
        if not evidence or evidence['source_class']!='FIRST_PARTY' or evidence['evidence_id'] in noisy: continue
        try: root=json.loads(block['text'])
        except (TypeError,ValueError): continue
        for item in _objects(root):
            types=_types(item); type_set=set(types)
            if type_set.intersection(ORGANIZATION_TYPES):
                identity_match=_match_organization(item,company)
                if not identity_match.startswith('EXACT'): continue
                record={'types':types,'identity_match':identity_match}
                for source,target in (('name','name'),('legalName','legal_name'),('url','url'),
                  ('taxID','tax_id'),('vatID','vat_id'),('identifier','identifier')):
                    if source in item and isinstance(item[source],(str,int)): record[target]=item[source]
                if record not in organizations:
                    organizations.append(record); org_refs.append(_ref(evidence,block,quote=block['text']))
            if 'Product' in type_set:
                manufacturer=item.get('manufacturer')
                if isinstance(manufacturer,dict) and _match_organization(manufacturer,company).startswith('EXACT'):
                    product_refs.append(_ref(evidence,block,quote=block['text']))
                    manufacturer_refs.append(_ref(evidence,block,quote=block['text']))
            if 'Service' in type_set:
                provider=item.get('provider')
                if isinstance(provider,dict) and _match_organization(provider,company).startswith('EXACT'):
                    service_refs.append(_ref(evidence,block,quote=block['text']))
    organizations.sort(key=encode)
    return organizations,org_refs,product_refs,service_refs,manufacturer_refs


def _text_matches(blocks,evidence_by_id,pattern,noisy,noisy_blocks,required=None,forbidden=None):
    refs=[]
    for block in blocks:
        evidence=evidence_by_id.get(block['evidence_id'])
        if (not evidence or evidence['source_class']!='FIRST_PARTY' or
                evidence['evidence_id'] in noisy or block['block_id'] in noisy_blocks): continue
        if block['block_type'] not in {'HEADING','PARAGRAPH','LIST_ITEM'}: continue
        url=_page_url(evidence) or ''
        if NOISY_PATH.search(url) or RELATIONAL_NOISE.search(block['text']): continue
        match=pattern.search(block['text'])
        if not match: continue
        required_patterns=required if isinstance(required,tuple) else (required,) if required else ()
        if any(not item.search(block['text']) for item in required_patterns): continue
        if forbidden and forbidden.search(block['text']): continue
        window=block['text'][max(0,match.start()-48):min(len(block['text']),match.end()+48)]
        if not NEGATION.search(window):
            refs.append(_ref(evidence,block,quote=match.group(0)))
    return refs


def _page_marker(blocks,evidence_by_id,path_pattern,heading_pattern,noisy,noisy_blocks):
    refs=[]
    for block in blocks:
        evidence=evidence_by_id.get(block['evidence_id'])
        if (not evidence or evidence['source_class']!='FIRST_PARTY' or
                evidence['evidence_id'] in noisy or block['block_id'] in noisy_blocks or
                block['block_type']!='HEADING'): continue
        if not path_pattern.search(urlsplit(_page_url(evidence) or '').path): continue
        match=heading_pattern.search(block['text'])
        if match: refs.append(_ref(evidence,block,quote=match.group(0)))
    return refs


def _validate_lineage(store,run_id,attempt_id,company_id):
    attempt=store.conn.execute('''SELECT 1 FROM profile_attempts
      WHERE attempt_id=? AND run_id=? AND company_id=?''',(attempt_id,run_id,company_id)).fetchone()
    if attempt is None: raise ValueError('Deterministic run/company/attempt lineage mismatch')
    invalid_evidence=store.conn.execute('''SELECT 1 FROM profile_evidence
      WHERE attempt_id=? AND (run_id<>? OR company_id<>?) LIMIT 1''',(attempt_id,run_id,company_id)).fetchone()
    if invalid_evidence: raise ValueError('Deterministic evidence lineage mismatch')
    invalid_blocks=store.conn.execute('''SELECT 1 FROM profile_content_blocks b
      LEFT JOIN profile_evidence e ON e.evidence_id=b.evidence_id AND e.run_id=b.run_id
       AND e.attempt_id=b.attempt_id AND e.company_id=b.company_id
      WHERE b.attempt_id=? AND (b.run_id<>? OR b.company_id<>? OR e.evidence_id IS NULL)
      LIMIT 1''',(attempt_id,run_id,company_id)).fetchone()
    if invalid_blocks: raise ValueError('Deterministic block/evidence lineage mismatch')


def extract(store,run_id,attempt_id,company_id):
    _validate_lineage(store,run_id,attempt_id,company_id)
    evidence=[dict(row) for row in store.conn.execute(
      'SELECT * FROM profile_evidence WHERE attempt_id=? ORDER BY rowid',(attempt_id,))]
    blocks=[dict(row) for row in store.conn.execute('''SELECT b.*,e.origin_evidence_id
      FROM profile_content_blocks b JOIN profile_evidence e ON e.evidence_id=b.evidence_id
      WHERE b.attempt_id=? ORDER BY b.rowid''',(attempt_id,))]
    company=json.loads(store.conn.execute('SELECT identity_snapshot_json FROM profile_run_companies '
      'WHERE run_id=? AND company_id=?',(run_id,company_id)).fetchone()[0])
    evidence_by_id={row['evidence_id']:row for row in evidence}; pages=_pages(evidence)
    noisy,noisy_blocks=_noise_contexts(blocks,evidence_by_id,company)
    blocks_by_locator={(row['evidence_id'],row['source_locator']):row for row in blocks}
    metadata,metadata_refs,titles,descriptions,languages,language_refs=_metadata(pages,blocks_by_locator)
    organizations,organization_refs,product_structured,service_structured,manufacturer_structured=(
      _structured(blocks,evidence_by_id,company,noisy))
    manufacturer_text=_text_matches(blocks,evidence_by_id,MANUFACTURER,noisy,noisy_blocks,
      required=PHYSICAL_MANUFACTURING_OBJECT,forbidden=INTANGIBLE_OUTPUT)
    manufacturer_text+=_text_matches(blocks,evidence_by_id,PRODUCE,noisy,noisy_blocks,
      required=PHYSICAL_MANUFACTURING_OBJECT,forbidden=INTANGIBLE_OUTPUT)
    international=_text_matches(blocks,evidence_by_id,EXPORT_ACTION,noisy,noisy_blocks,
      required=(EXPORT_DESTINATION,COMMERCIAL_EXPORT_OBJECT),forbidden=NONCOMMERCIAL_EXPORT)
    international+=_text_matches(blocks,evidence_by_id,EXPORT_SHARE,noisy,noisy_blocks)
    international+=_text_matches(blocks,evidence_by_id,INTERNATIONAL_SUPPLY,noisy,noisy_blocks)
    product_marker=_page_marker(blocks,evidence_by_id,PRODUCT_PATH,PRODUCT_HEADING,noisy,noisy_blocks)
    service_marker=_page_marker(blocks,evidence_by_id,SERVICE_PATH,SERVICE_HEADING,noisy,noisy_blocks)
    b2b=_text_matches(blocks,evidence_by_id,B2B,noisy,noisy_blocks)
    b2c=_text_matches(blocks,evidence_by_id,B2C,noisy,noisy_blocks)
    facts=[]
    facts.append(DeterministicFact('site_title',titles[0][0],'PRESENT','HIGH','DET-META-01',(titles[0][1],))
      if titles else _unknown('site_title','DET-META-01'))
    facts.append(DeterministicFact('meta_description',descriptions[0][0],'PRESENT','HIGH','DET-META-02',(descriptions[0][1],))
      if descriptions else _unknown('meta_description','DET-META-02'))
    facts.append(DeterministicFact('page_metadata',metadata,'PRESENT','HIGH','DET-META-03',tuple(metadata_refs))
      if metadata else _unknown('page_metadata','DET-META-03',[]))
    facts.append(DeterministicFact('declared_languages',languages,'DECLARED','HIGH','DET-LANG-01',tuple(language_refs))
      if languages else _unknown('declared_languages','DET-LANG-01',[]))
    facts.append(DeterministicFact('structured_organizations',organizations,'OBSERVED','MEDIUM','DET-STRUCT-01',tuple(organization_refs))
      if organizations else _unknown('structured_organizations','DET-STRUCT-01',[]))
    manufacturer_refs=manufacturer_structured+manufacturer_text
    facts.append(DeterministicFact('manufacturer_signal','EXPLICIT','EXPLICIT','HIGH','DET-MANUFACTURER-01',tuple(manufacturer_refs))
      if manufacturer_refs else _unknown('manufacturer_signal','DET-MANUFACTURER-01','UNKNOWN'))
    facts.append(DeterministicFact('international_signal','EXPLICIT','EXPLICIT','HIGH','DET-INTERNATIONAL-01',tuple(international))
      if international else _unknown('international_signal','DET-INTERNATIONAL-01','UNKNOWN'))
    product_refs=product_structured or product_marker; product_state='STRUCTURED' if product_structured else 'PAGE_MARKER'
    facts.append(DeterministicFact('product_presence_signal',product_state,product_state,
      'HIGH' if product_structured else 'MEDIUM','DET-PRODUCT-01',tuple(product_refs))
      if product_refs else _unknown('product_presence_signal','DET-PRODUCT-01','UNKNOWN'))
    service_refs=service_structured or service_marker; service_state='STRUCTURED' if service_structured else 'PAGE_MARKER'
    facts.append(DeterministicFact('service_presence_signal',service_state,service_state,
      'HIGH' if service_structured else 'MEDIUM','DET-SERVICE-01',tuple(service_refs))
      if service_refs else _unknown('service_presence_signal','DET-SERVICE-01','UNKNOWN'))
    audiences=[]; audience_refs=[]
    if b2b: audiences.append('B2B'); audience_refs.extend(b2b)
    if b2c: audiences.append('B2C'); audience_refs.extend(b2c)
    facts.append(DeterministicFact('explicit_audience_markers',audiences,'EXPLICIT','HIGH','DET-AUDIENCE-01',tuple(audience_refs))
      if audiences else _unknown('explicit_audience_markers','DET-AUDIENCE-01',[]))
    activity=store.conn.execute('SELECT registered_activity_json FROM profile_run_companies '
      'WHERE run_id=? AND company_id=?',(run_id,company_id)).fetchone()[0]
    if activity is not None:
        value=json.loads(activity); facts.append(DeterministicFact('observed_activity_codes',value,
          'OBSERVED','HIGH','DET-ACTIVITY-01',(DeterministicEvidence(None,source_locator='identity_snapshot.registered_activity',metadata_value=activity),)))
    return DeterministicResult(_corpus_hash(evidence,blocks),_status(evidence,blocks),tuple(facts))


def evaluate_and_store(store,run_id,attempt_id,company_id):
    result=extract(store,run_id,attempt_id,company_id); result_id=uid()
    company_row=store.conn.execute('SELECT discovery_result_id FROM profile_run_companies '
      'WHERE run_id=? AND company_id=?',(run_id,company_id)).fetchone()
    descriptor=json.loads(store.conn.execute('SELECT source_descriptor_json FROM profile_runs WHERE run_id=?',(run_id,)).fetchone()[0])
    with store.conn:
        store.insert('profile_deterministic_results',{'deterministic_result_id':result_id,
          'run_id':run_id,'attempt_id':attempt_id,'company_id':company_id,
          'rule_version':DETERMINISTIC_RULE_VERSION,'corpus_hash':result.corpus_hash,
          'corpus_status':result.corpus_status,'fact_count':len(result.facts),
          'evidence_count':len({ref.profile_evidence_id for fact in result.facts for ref in fact.evidence if ref.profile_evidence_id}),
          'completed_at':now()})
        for fact in result.facts:
            fact_id=uid(); store.insert('profile_deterministic_facts',{'fact_id':fact_id,
              'deterministic_result_id':result_id,'run_id':run_id,'attempt_id':attempt_id,
              'company_id':company_id,'field_name':fact.field_name,'value_json':encode(fact.value),
              'state':fact.state,'confidence':fact.confidence,'rule_id':fact.rule_id,
              'rule_version':DETERMINISTIC_RULE_VERSION})
            seen_refs=set()
            for ref in fact.evidence:
                ref_key=(ref.profile_evidence_id,ref.block_id,ref.page_url,ref.source_locator,
                  ref.exact_quote,ref.metadata_value)
                if ref_key in seen_refs: continue
                seen_refs.add(ref_key); source=None
                if ref.profile_evidence_id:
                    source=store.conn.execute('''SELECT * FROM profile_evidence
                      WHERE evidence_id=? AND attempt_id=? AND run_id=? AND company_id=?''',
                      (ref.profile_evidence_id,attempt_id,run_id,company_id)).fetchone()
                    if source is None: raise ValueError('Deterministic fact references evidence outside its attempt')
                    if ref.block_id:
                        block=store.conn.execute('''SELECT 1 FROM profile_content_blocks
                          WHERE block_id=? AND evidence_id=? AND attempt_id=?
                          AND run_id=? AND company_id=?''',
                          (ref.block_id,ref.profile_evidence_id,attempt_id,run_id,company_id)).fetchone()
                        if block is None: raise ValueError('Deterministic fact block/evidence mismatch')
                store.insert('profile_deterministic_fact_evidence',{'fact_id':fact_id,
                  'profile_evidence_id':ref.profile_evidence_id,'block_id':ref.block_id,
                  'origin_database':source['origin_database'] if source else descriptor.get('database_path'),
                  'origin_run_id':source['origin_run_id'] if source else descriptor.get('run_id'),
                  'origin_attempt_id':source['origin_attempt_id'] if source else None,
                  'origin_result_id':company_row['discovery_result_id'],'origin_evidence_id':source['origin_evidence_id'] if source else None,
                  'origin_content_hash':source['origin_content_hash'] if source else None,
                  'page_url':ref.page_url,'source_locator':ref.source_locator,
                  'exact_quote':ref.exact_quote,'metadata_value':ref.metadata_value})
    return result_id,result
