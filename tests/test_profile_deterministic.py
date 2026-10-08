import hashlib, json, sqlite3

import pytest

from profile_v1 import APPLICATION_ID, DETERMINISTIC_RULE_VERSION, SCHEMA_VERSION
from profile_v1.blocks import extract_blocks
from profile_v1.contracts import CandidateClaim, Citation
from profile_v1.deterministic import evaluate_and_store, extract
from profile_v1.runner import _publish, _publish_deterministic_only, replay
from profile_v1.semantic import LiveInterpreter
import profile_v1.store as store_module
from profile_v1.store import Store, uid


def fixture_corpus(tmp_path,pages=(),extra=(),registered_activity=None):
    source=tmp_path/'source.sqlite3'; source.write_bytes(b'frozen discovery source')
    descriptor={'database_path':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
      'run_id':'discovery-run'}
    company={'id':7,'company_name':'ALFA d.o.o.','tax_number':'SI123','registration_number':'456'}
    store=Store(tmp_path/'profile.sqlite3',create=True,require_new=True)
    run_id=store.create_run(descriptor,[{'identity':company,'registered_activity':registered_activity,
      'discovery_attempt_id':'discovery-attempt','discovery_result_id':'discovery-result',
      'accepted_website':'https://alfa.si'}])
    attempt=store.start_attempt(run_id,7)
    for index,(url,html) in enumerate(pages,1):
        add_evidence(store,run_id,attempt,index,'FIRST_PARTY',url,html)
    for index,(source_class,url,html) in enumerate(extra,len(pages)+1):
        add_evidence(store,run_id,attempt,index,source_class,url,html)
    return store,source,run_id,attempt,company


def add_evidence(store,run_id,attempt,index,source_class,url,html=None):
    evidence_id=f'evidence-{index}'; content_hash=hashlib.sha256((html or url).encode()).hexdigest()
    kind='DISCOVERY_SEARCH_RESULT' if source_class=='SEARCH_SNIPPET' else 'DISCOVERY_FETCHED_PAGE'
    values={'evidence_id':evidence_id,'run_id':run_id,'attempt_id':attempt,'company_id':7,
      'source_kind':kind,'source_class':source_class,'requested_url':url,'final_url':url,
      'title':'Search title' if source_class=='SEARCH_SNIPPET' else None,
      'snippet_body':'ALFA is a manufacturer serving B2B export markets.' if source_class=='SEARCH_SNIPPET' else None,
      'html':html,'content_hash':content_hash,'observed_at':'now','payload_json':'{}',
      'origin_database':'discovery.sqlite3','origin_run_id':'discovery-run',
      'origin_attempt_id':'discovery-attempt','origin_evidence_id':f'origin-{index}',
      'origin_content_hash':content_hash}
    with store.conn:
        store.insert('profile_evidence',values)
        if source_class=='FIRST_PARTY' and html:
            for block in extract_blocks(values):
                store.insert('profile_content_blocks',{'run_id':run_id,'attempt_id':attempt,
                  'company_id':7,**block,'evidence_id':evidence_id,'source_url':url})


def facts(store,attempt):
    return {row['field_name']:{**dict(row),'value':json.loads(row['value_json'])}
      for row in store.conn.execute('SELECT * FROM profile_deterministic_facts WHERE attempt_id=?',(attempt,))}


def rich_html(language='sl'):
    return f'''<!doctype html><html lang="{language}"><head><title>ALFA Exact Title</title>
      <meta name="description" content="Exact publisher description">
      <meta property="og:site_name" content="ALFA"><meta property="og:locale" content="sl_SI">
      <link rel="canonical" href="https://alfa.si/"><link rel="alternate" hreflang="en" href="https://alfa.si/en/">
      <script type="application/ld+json">{{"@graph":[
        {{"@type":"Organization","legalName":"ALFA d.o.o.","url":"https://alfa.si"}},
        {{"@type":"Product","name":"Widget","manufacturer":{{"@type":"Organization","name":"ALFA d.o.o."}}}},
        {{"@type":"Service","name":"Installation","provider":{{"@type":"Organization","name":"ALFA d.o.o."}}}}]}}</script></head><body>
      <h1>Industrial systems</h1><p>We manufacture industrial systems for demanding applications.</p>
      <p>We export our products to international markets and work B2B with distributors.</p>
      <p>Our engineering team develops durable equipment and provides technical support.</p></body></html>'''


def test_rich_deterministic_profile_and_exact_provenance(tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/',rich_html())],
      registered_activity=['C25.110'])
    before=source.read_bytes(); result_id,result=evaluate_and_store(store,run_id,attempt,7)
    values=facts(store,attempt)
    assert result.corpus_status=='RICH_FIRST_PARTY'
    assert values['site_title']['value']=='ALFA Exact Title'
    assert values['meta_description']['value']=='Exact publisher description'
    assert values['declared_languages']['value']==['en','sl','sl-si']
    assert values['structured_organizations']['value'][0]['identity_match'] in {'EXACT_NAME','EXACT_IDENTIFIER'}
    assert values['manufacturer_signal']['value']=='EXPLICIT'
    assert values['international_signal']['value']=='EXPLICIT'
    assert values['product_presence_signal']['value']=='STRUCTURED'
    assert values['service_presence_signal']['value']=='STRUCTURED'
    assert values['explicit_audience_markers']['value']==['B2B']
    assert values['observed_activity_codes']['value']==['C25.110']
    stored=store.conn.execute('SELECT * FROM profile_deterministic_results').fetchone()
    assert stored['rule_version']==DETERMINISTIC_RULE_VERSION
    assert stored['corpus_hash']==result.corpus_hash and stored['fact_count']==11
    refs=store.conn.execute('SELECT * FROM profile_deterministic_fact_evidence').fetchall()
    assert any(row['origin_result_id']=='discovery-result' and row['origin_evidence_id']=='origin-1' for row in refs)
    blocks={row['block_id']:row['text'] for row in store.conn.execute('SELECT block_id,text FROM profile_content_blocks')}
    assert all(not row['exact_quote'] or not row['block_id'] or row['exact_quote'] in blocks[row['block_id']] for row in refs)
    assert source.read_bytes()==before
    store.close()


def test_identical_corpus_replay_has_identical_facts_and_hash(tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/',rich_html())])
    _,first=evaluate_and_store(store,run_id,attempt,7); _publish_deterministic_only(store,run_id,7,attempt)
    first_values=[tuple(row) for row in store.conn.execute('''SELECT field_name,value_json,state,confidence,rule_id
      FROM profile_deterministic_facts WHERE attempt_id=? ORDER BY field_name''',(attempt,))]
    path=store.path; store.close(); replay(path,run_id,deterministic_only=True)
    store=Store(path); selected=store.conn.execute('SELECT selected_attempt_id FROM profile_run_companies').fetchone()[0]
    second=store.conn.execute('SELECT corpus_hash,rule_version FROM profile_deterministic_results WHERE attempt_id=?',(selected,)).fetchone()
    second_values=[tuple(row) for row in store.conn.execute('''SELECT field_name,value_json,state,confidence,rule_id
      FROM profile_deterministic_facts WHERE attempt_id=? ORDER BY field_name''',(selected,))]
    assert selected!=attempt and second['corpus_hash']==first.corpus_hash
    assert second['rule_version']==DETERMINISTIC_RULE_VERSION and second_values==first_values
    store.close()


@pytest.mark.parametrize(('language','expected'),[
  ('sl','sl'),('en','en'),('de-DE','de-de'),('it','it'),('hr_HR','hr-hr')])
def test_explicit_html_languages(language,expected,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('https://alfa.si/',f'<html lang="{language}"><body><p>Company text.</p></body></html>')])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)['declared_languages']['value']==[expected]
    store.close()


def test_foreign_language_and_global_word_do_not_imply_international(tmp_path):
    html='<html lang="de"><body><h1>Unternehmen</h1><p>Global platform cookie provider.</p></body></html>'
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/de/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['declared_languages']['value']==['de']
    assert values['international_signal']['state']=='UNKNOWN'
    store.close()


def test_negated_markers_do_not_create_positive_signals(tmp_path):
    html='''<html><body><p>We do not manufacture these products.</p>
      <p>We manufacture no products.</p><p>We do not export goods.</p>
      <p>We export no goods.</p><p>This is not B2B.</p><p>B2B is not our market.</p></body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['manufacturer_signal']['state']=='UNKNOWN'
    assert values['international_signal']['state']=='UNKNOWN'
    assert values['explicit_audience_markers']['state']=='UNKNOWN'
    store.close()


def test_manufacturer_relational_and_noisy_page_false_positives(tmp_path):
    html='<html><body><h1>Reference</h1><p>Our customer manufactures industrial pumps.</p></body></html>'
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/reference/customer/',html)])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)['manufacturer_signal']['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize(('url','heading','text'),[
  ('https://alfa.si/supplier/','Supplier profile','ACME is a manufacturer of bearings.'),
  ('https://alfa.si/vendor/','Vendor profile','ACME is a manufacturer of bearings.'),
  ('https://alfa.si/about/','Customer reference','ACME is a manufacturer of bearings.'),
  ('https://alfa.si/case-study/','Case study','We manufacture pumps for this customer.'),
  ('https://alfa.si/jobs/','Jobs','We manufacture pumps and export goods B2B.'),
  ('https://alfa.si/careers/','Careers','We manufacture pumps and export goods B2C.'),
  ('https://alfa.si/privacy/','Privacy policy','This policy covers our international sales platform and B2B data.'),
  ('https://alfa.si/legal/','Legal notice','International markets and B2C terms apply.')])
def test_noisy_or_third_party_contexts_fail_closed(url,heading,text,tmp_path):
    html=f'<html><body><h1>{heading}</h1><p>{text}</p></body></html>'
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[(url,html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    for field in ('manufacturer_signal','international_signal','explicit_audience_markers'):
        assert values[field]['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize('text',[
  'Our analysts monitor international markets.',
  'This privacy notice applies to the international sales platform.',
  'Wir beobachten Entwicklungen auf internationalen Märkten.',
  'Analizziamo i mercati internazionali.',
  'Global operations support customers worldwide.',
])
def test_bare_global_or_international_wording_does_not_prove_exports(text,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('https://alfa.si/',f'<html><body><p>{text}</p></body></html>')])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)['international_signal']['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize('text',[
  'We export data in CSV format.',
  'We export customer records as CSV files.',
  'We export quarterly reports to 20 countries.',
  'We export software configuration files to international markets.',
  'We export passwords to 20 countries.',
])
def test_noncommercial_export_language_does_not_imply_international_sales(text,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('https://alfa.si/docs/',f'<html><body><p>{text}</p></body></html>')])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)['international_signal']['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize('text',[
  'We export our products to 20 countries.',
  '80% of our production is exported.',
  'We supply customers across Europe.',
  'We export industrial equipment to foreign markets.',
])
def test_explicit_commercial_geographic_exports_are_retained(text,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('https://alfa.si/',f'<html><body><p>{text}</p></body></html>')])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)['international_signal']['state']=='EXPLICIT'
    store.close()


@pytest.mark.parametrize('text',[
  'We produce market reports for executives.',
  'We produce website content for clients.',
  'We produce accounting software.',
  'We produce economic studies and analysis.',
  'We produce media and video for campaigns.',
  'We produce customer data documentation.',
  'We produce industry events.',
])
def test_intangible_production_does_not_imply_manufacturing(text,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('https://alfa.si/',f'<html><body><p>{text}</p></body></html>')])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)['manufacturer_signal']['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize('text',[
  'We produce industrial consulting services.',
  'We produce factory optimization services.',
  'We manufacture consulting services for executives.',
  'We produce engineering advisory services for factories.',
  'Our factory produces technical support services.',
  'We manufacture maintenance services for industrial plants.',
  'We produce production optimization consulting.',
])
def test_service_objects_do_not_imply_manufacturing(text,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('https://alfa.si/',f'<html><body><p>{text}</p></body></html>')])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)['manufacturer_signal']['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize('text',[
  'We produce industrial equipment strategy for manufacturers.',
  'Our factory produces equipment strategy.',
  'We produce product strategy.',
  'We produce manufacturing strategy.',
  'We produce production strategy.',
  'We produce equipment consulting.',
  'We manufacture equipment advisory.',
  'We produce equipment optimization services.',
  'Our factory produces factory consulting.',
  'We manufacture manufacturing support services.',
  'We produce industrial equipment strategy services.',
])
def test_intangible_compound_objects_do_not_imply_manufacturing(text,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('https://alfa.si/',f'<html><body><p>{text}</p></body></html>')])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)['manufacturer_signal']['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize('text',[
  'We manufacture industrial pumps.',
  'We produce aluminium components.',
  'We produce industrial equipment.',
  'Our factory produces equipment.',
  'We manufacture equipment for factories.',
  'We produce industrial equipment for manufacturers.',
  'Our factory produces steel parts.',
  'Our production facility manufactures medical devices.',
])
def test_explicit_physical_manufacturing_is_retained(text,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('https://alfa.si/',f'<html><body><p>{text}</p></body></html>')])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)['manufacturer_signal']['state']=='EXPLICIT'
    store.close()


@pytest.mark.parametrize(('quote','field'),[
  ('We manufacture precision bearings.','manufacturer_signal'),
  ('We sell B2B to industrial customers.','explicit_audience_markers'),
])
def test_customer_testimonial_first_person_quote_is_not_attributed_to_target(quote,field,tmp_path):
    html=f'''<html><body><h1>Testimonials</h1><p>“{quote}” — Customer quote</p></body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/testimonials/',html)])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)[field]['state']=='UNKNOWN'
    store.close()


def test_target_prose_after_testimonial_section_remains_usable(tmp_path):
    html='''<html><body><h1>About ALFA</h1><h2>Testimonials</h2>
      <p>“We manufacture customer bearings and sell B2B.” — Customer quote</p>
      <h2>What ALFA does</h2><p>We manufacture industrial pumps and work B2B with distributors.</p>
      </body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/about/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['manufacturer_signal']['state']=='EXPLICIT'
    assert values['explicit_audience_markers']['value']==['B2B']
    manufacturer_blocks=[row[0] for row in store.conn.execute('''SELECT b.text
      FROM profile_deterministic_facts f JOIN profile_deterministic_fact_evidence fe ON fe.fact_id=f.fact_id
      JOIN profile_content_blocks b ON b.block_id=fe.block_id
      WHERE f.attempt_id=? AND f.field_name='manufacturer_signal' ''',(attempt,))]
    assert manufacturer_blocks==['We manufacture industrial pumps and work B2B with distributors.']
    store.close()


def test_generic_heading_does_not_end_testimonial_path_exclusion(tmp_path):
    html='''<html><body><h1>How ACME grew</h1>
      <p>“We manufacture customer bearings and sell B2B.” — Jane, ACME CEO</p>
      </body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/success-story/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['manufacturer_signal']['state']=='UNKNOWN'
    assert values['explicit_audience_markers']['state']=='UNKNOWN'
    store.close()


def test_attributed_customer_quote_is_excluded_without_testimonial_path_or_label(tmp_path):
    html='''<html><body><h1>Community</h1>
      <p>“We manufacture precision bearings and sell B2B.” — Jane, ACME CEO</p>
      </body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/about/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['manufacturer_signal']['state']=='UNKNOWN'
    assert values['explicit_audience_markers']['state']=='UNKNOWN'
    store.close()


def test_nested_customer_heading_does_not_end_testimonial_exclusion(tmp_path):
    html='''<html><body><h1>About ALFA</h1><h2>Testimonials</h2>
      <h3>ACME d.o.o.</h3><p>We manufacture bearings and sell B2B.</p>
      </body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/about/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['manufacturer_signal']['state']=='UNKNOWN'
    assert values['explicit_audience_markers']['state']=='UNKNOWN'
    store.close()


def test_multiple_customer_and_person_headings_remain_inside_testimonials(tmp_path):
    html='''<html><body><h1>About ALFA</h1><h2>Customer stories</h2>
      <h3>ACME d.o.o.</h3><p>We manufacture bearings.</p>
      <h3>Jane Novak, Chief Executive Officer</h3><p>We sell B2B to industrial customers.</p>
      <h3>BETA Ltd.</h3><p>Our factory produces steel valves.</p>
      </body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/about/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['manufacturer_signal']['state']=='UNKNOWN'
    assert values['explicit_audience_markers']['state']=='UNKNOWN'
    store.close()


def test_customer_heading_that_mentions_target_does_not_end_testimonial_exclusion(tmp_path):
    html='''<html><body><h1>About ALFA</h1><h2>Testimonials</h2>
      <h3>Why ACME chose ALFA</h3><p>We manufacture bearings and sell B2B.</p>
      </body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/about/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['manufacturer_signal']['state']=='UNKNOWN'
    assert values['explicit_audience_markers']['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize(('url','internal_heading'),[
  ('https://alfa.si/testimonials/','ALFA products'),
  ('https://alfa.si/reviews/','Our products'),
  ('https://alfa.si/customer-stories/','Solutions'),
  ('https://alfa.si/success-stories/','ALFA products'),
])
def test_dedicated_customer_feedback_pages_never_escape_testimonial_context(
        url,internal_heading,tmp_path):
    html=f'''<html><body><h1>Testimonials</h1><h2>{internal_heading}</h2>
      <h3>ACME d.o.o.</h3><p>“We manufacture bearings and sell B2B.”</p>
      </body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[(url,html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['manufacturer_signal']['state']=='UNKNOWN'
    assert values['explicit_audience_markers']['state']=='UNKNOWN'
    store.close()


def test_ordinary_product_page_target_prose_remains_usable(tmp_path):
    html='''<html><body><h1>Our products</h1>
      <p>We manufacture industrial equipment and sell B2B.</p></body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/products/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['manufacturer_signal']['state']=='EXPLICIT'
    assert values['explicit_audience_markers']['value']==['B2B']
    store.close()


def test_target_section_transition_after_nested_testimonials_is_usable(tmp_path):
    customer_manufacturing='We manufacture customer bearings.'
    customer_audience='We sell B2B to industrial customers.'
    target='We manufacture industrial pumps and work B2B with distributors.'
    html=f'''<html><body><h1>About ALFA</h1><h2>Testimonials</h2>
      <h3>ACME d.o.o.</h3><p>{customer_manufacturing}</p>
      <h3>Jane Novak, Customer CEO</h3><p>{customer_audience}</p>
      <h2>What ALFA does</h2><p>{target}</p></body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/about/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['manufacturer_signal']['state']=='EXPLICIT'
    assert values['explicit_audience_markers']['value']==['B2B']
    cited=[row[0] for row in store.conn.execute('''SELECT b.text
      FROM profile_deterministic_facts f JOIN profile_deterministic_fact_evidence fe ON fe.fact_id=f.fact_id
      JOIN profile_content_blocks b ON b.block_id=fe.block_id
      WHERE f.attempt_id=? AND f.field_name IN ('manufacturer_signal','explicit_audience_markers')
      ORDER BY b.text''',(attempt,))]
    assert cited==[target,target]
    store.close()


def test_unrelated_product_manufacturer_and_organization_are_not_attributed_to_target(tmp_path):
    html='''<html><body><script type="application/ld+json">[{"@type":"Product","name":"Part",
      "manufacturer":{"@type":"Organization","name":"OTHER d.o.o."}},
      {"@type":"Service","name":"Installation",
       "provider":{"@type":"Organization","name":"OTHER d.o.o."}}]</script></body></html>'''
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/product/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert values['product_presence_signal']['state']=='UNKNOWN'
    assert values['service_presence_signal']['state']=='UNKNOWN'
    assert values['manufacturer_signal']['state']=='UNKNOWN'
    assert values['structured_organizations']['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize(('url','heading','field'),[
  ('https://alfa.si/izdelki/','Izdelki','product_presence_signal'),
  ('https://alfa.si/services/','Services','service_presence_signal')])
def test_coherent_path_and_heading_page_markers(url,heading,field,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[(url,f'<html><body><h1>{heading}</h1></body></html>')])
    evaluate_and_store(store,run_id,attempt,7)
    assert facts(store,attempt)[field]['value']=='PAGE_MARKER'
    store.close()


@pytest.mark.parametrize(('declaration','expected'),[
  ('We work B2B with distributors.',['B2B']),
  ('Our services are sold B2C.',['B2C']),
  ('We serve businesses and consumers.',[])])
def test_audience_requires_explicit_marker(declaration,expected,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('https://alfa.si/',f'<html><body><p>{declaration}</p></body></html>')])
    evaluate_and_store(store,run_id,attempt,7)
    value=facts(store,attempt)['explicit_audience_markers']
    assert value['value']==expected and value['state']==('EXPLICIT' if expected else 'UNKNOWN')
    store.close()


def test_malformed_html_jsonld_duplicate_pages_are_safe_and_deduplicated(tmp_path):
    html='<html><head><title>Broken<title><script type="application/ld+json">{bad</script></head><body><p>Text'
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('http://alfa.si/',html),('https://alfa.si/',html)])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert len(values['page_metadata']['value'])==1
    assert values['structured_organizations']['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize(('extra','expected'),[
  ((), 'NO_EVIDENCE'),
  ((('SEARCH_SNIPPET','https://directory.si/alfa',None),), 'SEARCH_ONLY'),
  ((('CANDIDATE','https://candidate.si','<html><body><p>We manufacture and export B2B.</p></body></html>'),), 'CANDIDATE_ONLY')])
def test_non_first_party_corpus_status_never_creates_company_facts(extra,expected,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,extra=extra)
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    result=store.conn.execute('SELECT corpus_status FROM profile_deterministic_results').fetchone()[0]
    assert result==expected
    for field in ('site_title','manufacturer_signal','international_signal','product_presence_signal',
      'service_presence_signal','explicit_audience_markers'):
        assert values[field]['state']=='UNKNOWN'
    store.close()


@pytest.mark.parametrize(('pages','extra','expected'),[
  ((('https://alfa.si/','<html><body><p>Welcome.</p></body></html>'),),
   (('CANDIDATE','https://candidate.si','<html><body><p>Candidate.</p></body></html>'),
    ('SEARCH_SNIPPET','https://search.si/alfa',None)), 'THIN_FIRST_PARTY'),
  ((),(('CANDIDATE','https://candidate.si','<html><body><p>Candidate.</p></body></html>'),
       ('SEARCH_SNIPPET','https://search.si/alfa',None)), 'CANDIDATE_ONLY'),
])
def test_mixed_corpus_status_precedence(pages,extra,expected,tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,pages=pages,extra=extra)
    evaluate_and_store(store,run_id,attempt,7)
    assert store.conn.execute('SELECT corpus_status FROM profile_deterministic_results').fetchone()[0]==expected
    store.close()


def test_thin_first_party_and_unknown_mean_no_qualifying_evidence(tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[
      ('https://alfa.si/','<html><body><p>Welcome.</p></body></html>')])
    evaluate_and_store(store,run_id,attempt,7); values=facts(store,attempt)
    assert store.conn.execute('SELECT corpus_status FROM profile_deterministic_results').fetchone()[0]=='THIN_FIRST_PARTY'
    assert values['manufacturer_signal']['value']=='UNKNOWN'
    assert values['manufacturer_signal']['confidence']=='UNKNOWN'
    store.close()


def test_deterministic_and_semantic_results_coexist(tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/',rich_html())])
    evaluate_and_store(store,run_id,attempt,7)
    block=store.conn.execute("SELECT block_id,text FROM profile_content_blocks WHERE text LIKE '%manufacture%'").fetchone()
    claim=CandidateClaim('ACTUAL_PRIMARY_ACTIVITY','manufacturing','Manufacturing',
      (Citation(block['block_id'],'We manufacture industrial systems'),),'HIGH')
    _publish(store,run_id,7,attempt,[claim],'fixture-semantic')
    assert store.conn.execute('SELECT COUNT(*) FROM profile_deterministic_results').fetchone()[0]==1
    assert store.conn.execute('SELECT COUNT(*) FROM profile_company_results').fetchone()[0]==1
    store.close()


def test_deterministic_path_cannot_call_provider(tmp_path,monkeypatch):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/',rich_html())])
    monkeypatch.setattr(LiveInterpreter,'call',lambda *args:pytest.fail('provider called'))
    monkeypatch.setattr('requests.sessions.Session.request',lambda *args,**kwargs:pytest.fail('network called'))
    evaluate_and_store(store,run_id,attempt,7)
    store.close()


def test_schema_two_artifact_migrates_explicitly(tmp_path):
    path=tmp_path/'profile.sqlite3'; store=Store(path,create=True,require_new=True); store.close()
    conn=sqlite3.connect(path)
    conn.executescript('''DROP TABLE profile_deterministic_fact_evidence;
      DROP TABLE profile_deterministic_facts; DROP TABLE profile_deterministic_results;
      PRAGMA user_version=2;'''); conn.close()
    store=Store(path)
    assert store.conn.execute('PRAGMA application_id').fetchone()[0]==APPLICATION_ID
    assert store.conn.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
    assert store.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='profile_deterministic_results'").fetchone()[0]==1
    store.close()


def test_schema_three_deterministic_artifact_migrates_without_data_loss(tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/',rich_html())])
    result_id,_=evaluate_and_store(store,run_id,attempt,7); path=store.path; store.close()
    conn=sqlite3.connect(path)
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'profile_%_lineage_%'").fetchall():
        conn.execute(f'DROP TRIGGER {row[0]}')
    conn.execute('PRAGMA user_version=3'); conn.commit(); conn.close()
    store=Store(path)
    assert store.conn.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
    assert store.conn.execute('SELECT deterministic_result_id FROM profile_deterministic_results').fetchone()[0]==result_id
    assert store.conn.execute('SELECT COUNT(*) FROM profile_deterministic_facts').fetchone()[0]==10
    store.close()


def test_migration_failure_rolls_back_schema_version_and_preserves_data(tmp_path,monkeypatch):
    path=tmp_path/'profile.sqlite3'; store=Store(path,create=True,require_new=True)
    with store.conn:
        store.insert('profile_runs',{'run_id':'preserved','job_type':'PROFILE_ACTIVITY','status':'PENDING',
          'engine_version':'e','rule_version':'r','source_descriptor_json':'{}','manifest_hash':'h',
          'created_at':'now','started_at':None,'finished_at':None})
    store.close(); conn=sqlite3.connect(path)
    conn.executescript('''DROP TABLE profile_deterministic_fact_evidence;
      DROP TABLE profile_deterministic_facts; DROP TABLE profile_deterministic_results;
      PRAGMA user_version=2;'''); conn.close()
    monkeypatch.setattr(store_module,'MIGRATION_3_TO_4',
      'CREATE TABLE migration_probe(value TEXT); INSERT INTO missing_table VALUES (1);')
    with pytest.raises(sqlite3.OperationalError): Store(path)
    conn=sqlite3.connect(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0]==2
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='migration_probe'").fetchone()[0]==0
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='profile_deterministic_results'").fetchone()[0]==0
    assert conn.execute("SELECT COUNT(*) FROM profile_runs WHERE run_id='preserved'").fetchone()[0]==1
    conn.close()


def test_cross_company_evaluation_and_cross_attempt_evidence_are_rejected(tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/',rich_html())])
    with store.conn:
        store.insert('profile_run_companies',{'run_id':run_id,'company_id':8,'manifest_position':1,
          'identity_snapshot_json':json.dumps({'id':8,'company_name':'BETA d.o.o.'}),
          'registered_activity_json':None,'discovery_attempt_id':'discovery-attempt-8',
          'discovery_result_id':'discovery-result-8','accepted_website':'https://beta.si','status':'PENDING',
          'selected_attempt_id':None})
    with pytest.raises(ValueError,match='lineage mismatch'):
        evaluate_and_store(store,run_id,attempt,8)
    assert store.conn.execute('SELECT COUNT(*) FROM profile_deterministic_results').fetchone()[0]==0
    with pytest.raises(sqlite3.IntegrityError,match='result lineage mismatch'):
        with store.conn:
            store.insert('profile_deterministic_results',{'deterministic_result_id':'attack',
              'run_id':run_id,'attempt_id':attempt,'company_id':8,
              'rule_version':DETERMINISTIC_RULE_VERSION,'corpus_hash':'attack',
              'corpus_status':'NO_EVIDENCE','fact_count':0,'evidence_count':0,'completed_at':'now'})
    with pytest.raises(sqlite3.IntegrityError,match='attempt lineage is immutable'):
        with store.conn:
            store.conn.execute('UPDATE profile_attempts SET company_id=8 WHERE attempt_id=?',(attempt,))
    second=store.start_attempt(run_id,7)
    evidence=store.conn.execute('SELECT evidence_id FROM profile_evidence WHERE attempt_id=?',(attempt,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError,match='block lineage mismatch'):
        with store.conn:
            store.insert('profile_content_blocks',{'block_id':uid(),'run_id':run_id,'attempt_id':second,
              'company_id':7,'evidence_id':evidence,'source_url':'https://alfa.si',
              'block_type':'PARAGRAPH','source_locator':'p:attack','text':'We manufacture pumps.',
              'text_hash':'attack','language':None})
    store.close()


def test_deterministic_results_are_immutable_per_attempt(tmp_path):
    store,source,run_id,attempt,company=fixture_corpus(tmp_path,[('https://alfa.si/',rich_html())])
    evaluate_and_store(store,run_id,attempt,7)
    with pytest.raises(sqlite3.IntegrityError): evaluate_and_store(store,run_id,attempt,7)
    assert store.conn.execute('SELECT COUNT(*) FROM profile_deterministic_results').fetchone()[0]==1
    store.close()
