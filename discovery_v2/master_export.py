"""Read-only 4xxx/5xxx production master export for canonical and enrichment data."""
import argparse
import csv
import json
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path


USABLE_DISCOVERY_WEBSITES = {'VERIFIED', 'HIGH', 'MEDIUM'}
EXPECTED_TOTAL = 839
EXPECTED_SELECTED = 579
REVIEW_COLUMNS = ('email_ok','website_ok','phone_ok','default_choice_ok','wrong_entity','audit_note')
DISCOVERY_EMAIL_PARTITION = (
    'DISCOVERY_SELECTED_WITH_CURRENT_OLD_EMAIL',
    'DISCOVERY_SELECTED_WITHOUT_CURRENT_OLD_EMAIL',
    'NOT_SELECTED_WITH_CURRENT_OLD_EMAIL',
    'NOT_SELECTED_WITHOUT_CURRENT_OLD_EMAIL',
)
BASE_HEADERS = (
    'company_id','company_name','tax_number','registration_number','address','municipality',
    'revenue_2025','employees_2025','sparrow_website','sparrow_website_status',
    'sparrow_emails','sparrow_email_confidences','discovery_v2_selected','discovery_v2_batch',
    'discovery_v2_run_status','discovery_website','discovery_website_status',
    'discovery_contact_outcome','discovery_default_email','discovery_email_role',
    'discovery_email_attribution_status','discovery_default_phone','discovery_run_id',
    'discovery_completed_at','new_discovery_email','best_email','best_email_source',
    'best_website','best_website_source','best_phone','best_phone_source',
)
CANONICAL_REQUIRED = {
    'companies_lite': {'id','company_name','tax_number','registration_number','address','municipality',
                       'revenue_2025','employees_2025'},
    'website_discovery': {'id','company_id','website','status'},
    'email_discovery': {'id','company_id','email','confidence'},
}
DISCOVERY_REQUIRED = {
    'discovery_runs': {'run_id'},
    'discovery_run_companies': {
        'run_id', 'company_id', 'status', 'selected_attempt_id', 'manifest_position'
    },
    'discovery_company_results': {'result_id','run_id','attempt_id','company_id','official_website',
        'website_status','contact_outcome','default_email_contact_id','default_phone_contact_id',
        'completed_at'},
    'discovery_contacts': {'contact_id','run_id','attempt_id','company_id','contact_type',
        'normalized_value','attribution_status','roles_json'},
}


def readonly(path):
    resolved=Path(path).expanduser().resolve(strict=True)
    conn=sqlite3.connect(resolved.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=.5)
    conn.row_factory=sqlite3.Row; conn.execute('PRAGMA query_only=ON')
    return resolved,conn


def columns(conn,table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def require_schema(conn,required,label):
    for table,expected in required.items():
        missing=expected-columns(conn,table)
        if missing:
            raise ValueError(f'{label}: incompatible schema; {table} missing {sorted(missing)}')


def canonical_rows(path):
    resolved,conn=readonly(path)
    try:
        require_schema(conn,CANONICAL_REQUIRED,str(resolved))
        company_columns=columns(conn,'companies_lite')
        phone_column='phone' if 'phone' in company_columns else None
        select='id,company_name,tax_number,registration_number,address,municipality,revenue_2025,employees_2025'
        if phone_column: select+=',phone AS canonical_phone'
        companies={row['id']:dict(row) for row in conn.execute(f'''SELECT {select} FROM companies_lite
            WHERE municipality GLOB '4[0-9][0-9][0-9]*'
               OR municipality GLOB '5[0-9][0-9][0-9]*' ''')}
        sites={}
        for row in conn.execute('''SELECT w.* FROM website_discovery w JOIN
            (SELECT company_id,MAX(id) id FROM website_discovery GROUP BY company_id) latest
            ON latest.id=w.id WHERE w.company_id IN (SELECT id FROM companies_lite
            WHERE municipality GLOB '4[0-9][0-9][0-9]*' OR municipality GLOB '5[0-9][0-9][0-9]*')'''):
            sites[row['company_id']]=dict(row)
        emails={company_id:[] for company_id in companies}
        for row in conn.execute('''SELECT id,company_id,email,confidence FROM email_discovery
            WHERE company_id IN (SELECT id FROM companies_lite WHERE
            municipality GLOB '4[0-9][0-9][0-9]*' OR municipality GLOB '5[0-9][0-9][0-9]*')
            ORDER BY company_id,LOWER(email),email,id'''):
            emails[row['company_id']].append(dict(row))
        return resolved,companies,sites,emails,phone_column
    finally: conn.close()


def _roles(raw,label):
    try: value=json.loads(raw)
    except (TypeError,json.JSONDecodeError) as exc: raise ValueError(f'{label}: malformed contact roles') from exc
    if not isinstance(value,list) or any(not isinstance(role,str) for role in value):
        raise ValueError(f'{label}: invalid contact roles')
    return '|'.join(value)


def discovery_batch(path,label):
    resolved,conn=readonly(path)
    try:
        require_schema(conn,DISCOVERY_REQUIRED,str(resolved))
        runs=[row[0] for row in conn.execute('SELECT run_id FROM discovery_runs ORDER BY rowid')]
        if len(runs)!=1:
            raise ValueError(f'{resolved}: expected exactly one Discovery run, found {len(runs)}')
        run_id=runs[0]; records={}
        query='''SELECT rc.company_id,rc.status AS company_status,rc.selected_attempt_id,
          result.result_id,result.official_website,result.website_status,result.contact_outcome,
          result.default_email_contact_id,result.default_phone_contact_id,result.completed_at,
          email.contact_type email_type,email.normalized_value email_value,
          email.attribution_status email_attribution,email.roles_json email_roles,
          phone.contact_type phone_type,phone.normalized_value phone_value,
          phone.attribution_status phone_attribution
          FROM discovery_run_companies rc
          LEFT JOIN discovery_company_results result ON result.run_id=rc.run_id
            AND result.company_id=rc.company_id AND result.attempt_id=rc.selected_attempt_id
          LEFT JOIN discovery_contacts email ON email.contact_id=result.default_email_contact_id
            AND email.run_id=result.run_id AND email.company_id=result.company_id
            AND email.attempt_id=result.attempt_id
          LEFT JOIN discovery_contacts phone ON phone.contact_id=result.default_phone_contact_id
            AND phone.run_id=result.run_id AND phone.company_id=result.company_id
            AND phone.attempt_id=result.attempt_id
          WHERE rc.run_id=? ORDER BY rc.manifest_position'''
        for stored in conn.execute(query,(run_id,)):
            row=dict(stored); company_id=row['company_id']; source=f'{resolved} company {company_id}'
            if company_id in records: raise ValueError(f'{source}: duplicate run-company row')
            if row['default_email_contact_id'] is not None:
                if row['email_type']!='EMAIL' or row['email_attribution']!='ATTRIBUTED':
                    raise ValueError(f'{source}: invalid persisted default email')
                email_role=_roles(row['email_roles'],source)
            else: email_role=''
            if row['default_phone_contact_id'] is not None:
                if row['phone_type']!='PHONE' or row['phone_attribution']!='ATTRIBUTED':
                    raise ValueError(f'{source}: invalid persisted default phone')
            records[company_id]={
                'discovery_v2_selected':'YES','discovery_v2_batch':label,
                'discovery_v2_run_status':row['company_status'],
                'discovery_website':row['official_website'] or '',
                'discovery_website_status':row['website_status'] or '',
                'discovery_contact_outcome':row['contact_outcome'] or '',
                'discovery_default_email':row['email_value'] or '',
                'discovery_email_role':email_role,
                'discovery_email_attribution_status':row['email_attribution'] or '',
                'discovery_default_phone':row['phone_value'] or '',
                'discovery_run_id':run_id,'discovery_completed_at':row['completed_at'] or '',
                '_has_result':row['result_id'] is not None,
            }
        return resolved,records
    finally: conn.close()


def build(source,pilot,batch100,big459,expected_total=EXPECTED_TOTAL,
          expected_selected=EXPECTED_SELECTED):
    source_path,companies,sites,emails,phone_column=canonical_rows(source)
    batches=[]
    for path,label in ((pilot,'PILOT20'),(batch100,'BATCH100'),(big459,'BIG459')):
        batches.append(discovery_batch(path,label))
    selected={}
    for path,records in batches:
        overlap=set(selected)&set(records)
        if overlap:
            raise ValueError(f'Discovery batch overlap for company IDs {sorted(overlap)}')
        selected.update(records)
    outside=sorted(set(selected)-set(companies))
    if outside:
        raise ValueError(f'Discovery selections fall outside the 4xxx/5xxx population: {outside}')
    if len(companies)!=expected_total:
        raise ValueError(f'TOTAL_45XX expected {expected_total}, found {len(companies)}')
    if len(selected)!=expected_selected:
        raise ValueError(f'DISCOVERY_SELECTED expected {expected_selected}, found {len(selected)}')
    rows=[]
    for company_id,company in companies.items():
        old=emails.get(company_id,[]); old_values=[entry['email'] for entry in old if entry['email']]
        old_joined=' | '.join(old_values)
        site=sites.get(company_id,{})
        discovery=selected.get(company_id,{})
        default_email=discovery.get('discovery_default_email','')
        default_phone=discovery.get('discovery_default_phone','')
        canonical_phone=str(company.get('canonical_phone') or '') if phone_column else ''
        discovery_status=discovery.get('discovery_website_status','')
        discovery_site=discovery.get('discovery_website','')
        sparrow_site=site.get('website') or ''
        sparrow_accepted=site.get('status')=='VERIFIED' and bool(sparrow_site)
        if discovery_site and discovery_status in USABLE_DISCOVERY_WEBSITES:
            best_site,best_site_source=discovery_site,'DISCOVERY_V2'
        elif sparrow_accepted:
            best_site,best_site_source=sparrow_site,'SPARROW_0.9'
        else: best_site,best_site_source='',''
        row={
            'company_id':company_id,'company_name':company['company_name'] or '',
            'tax_number':company['tax_number'] or '',
            'registration_number':company['registration_number'] or '',
            'address':company['address'] or '','municipality':company['municipality'] or '',
            'revenue_2025':company['revenue_2025'],'employees_2025':company['employees_2025'],
            'sparrow_website':sparrow_site,'sparrow_website_status':site.get('status') or '',
            'sparrow_emails':old_joined,
            'sparrow_email_confidences':' | '.join('' if e['confidence'] is None else str(e['confidence']) for e in old),
            'discovery_v2_selected':discovery.get('discovery_v2_selected','NO'),
            'discovery_v2_batch':discovery.get('discovery_v2_batch',''),
            'discovery_v2_run_status':discovery.get('discovery_v2_run_status',''),
            'discovery_website':discovery_site,'discovery_website_status':discovery_status,
            'discovery_contact_outcome':discovery.get('discovery_contact_outcome',''),
            'discovery_default_email':default_email,
            'discovery_email_role':discovery.get('discovery_email_role',''),
            'discovery_email_attribution_status':discovery.get('discovery_email_attribution_status',''),
            'discovery_default_phone':default_phone,
            'discovery_run_id':discovery.get('discovery_run_id',''),
            'discovery_completed_at':discovery.get('discovery_completed_at',''),
            'new_discovery_email':'YES' if not old_values and default_email else 'NO',
            'best_email':old_joined or default_email,
            'best_email_source':'SPARROW_0.9' if old_values else 'DISCOVERY_V2' if default_email else '',
            'best_website':best_site,'best_website_source':best_site_source,
            'best_phone':canonical_phone or default_phone,
            'best_phone_source':'CANONICAL' if canonical_phone else 'DISCOVERY_V2' if default_phone else '',
        }
        if phone_column: row['canonical_phone']=canonical_phone
        row.update({column:'' for column in REVIEW_COLUMNS}); row['_has_result']=discovery.get('_has_result',False)
        rows.append(row)
    rows.sort(key=lambda row:((row['municipality'] or '').casefold(),
                              (row['company_name'] or '').casefold(),row['company_id']))
    if len({row['company_id'] for row in rows})!=len(rows):
        raise ValueError('CSV population contains duplicate company IDs')
    stats=statistics(rows)
    if stats['OLD_EMAIL_COMPANIES']+stats['NO_OLD_EMAIL_COMPANIES']!=expected_total:
        raise ValueError('Old-email partition does not equal TOTAL_45XX')
    if sum(stats[key] for key in DISCOVERY_EMAIL_PARTITION)!=expected_total:
        raise ValueError('Discovery-selection/current-email partition does not equal TOTAL_45XX')
    return rows,stats,phone_column,[source_path,*[path for path,_ in batches]]


def statistics(rows):
    websites=Counter(row['discovery_website_status'] for row in rows if row['discovery_website_status'])
    selected_with_email=sum(
        row['discovery_v2_selected']=='YES' and bool(row['sparrow_emails']) for row in rows)
    selected_without_email=sum(
        row['discovery_v2_selected']=='YES' and not bool(row['sparrow_emails']) for row in rows)
    not_selected_with_email=sum(
        row['discovery_v2_selected']=='NO' and bool(row['sparrow_emails']) for row in rows)
    not_selected_without_email=sum(
        row['discovery_v2_selected']=='NO' and not bool(row['sparrow_emails']) for row in rows)
    return {
        'TOTAL_45XX':len(rows),
        'OLD_EMAIL_COMPANIES':sum(bool(row['sparrow_emails']) for row in rows),
        'NO_OLD_EMAIL_COMPANIES':sum(not bool(row['sparrow_emails']) for row in rows),
        'DISCOVERY_SELECTED':sum(row['discovery_v2_selected']=='YES' for row in rows),
        'DISCOVERY_RESULTS':sum(bool(row['_has_result']) for row in rows),
        'DISCOVERY_FAILED':sum(row['discovery_v2_run_status']=='FAILED' for row in rows),
        'NEW_DISCOVERY_EMAILS':sum(row['new_discovery_email']=='YES' for row in rows),
        'DISCOVERY_DEFAULT_PHONES':sum(bool(row['discovery_default_phone']) for row in rows),
        'DISCOVERY_USABLE_WEBSITES':sum(row['discovery_website_status'] in USABLE_DISCOVERY_WEBSITES and bool(row['discovery_website']) for row in rows),
        **{status:websites[status] for status in ('VERIFIED','HIGH','MEDIUM','REVIEW')},
        'DISCOVERY_SELECTED_WITH_CURRENT_OLD_EMAIL':selected_with_email,
        'DISCOVERY_SELECTED_WITHOUT_CURRENT_OLD_EMAIL':selected_without_email,
        'NOT_SELECTED_WITH_CURRENT_OLD_EMAIL':not_selected_with_email,
        'NOT_SELECTED_WITHOUT_CURRENT_OLD_EMAIL':not_selected_without_email,
    }


def _safe(value):
    text='' if value is None else str(value)
    return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text


def write_output(output,rows,phone_column,inputs):
    output=Path(output).expanduser().absolute(); resolved=output.resolve()
    if any(resolved==Path(path).resolve() for path in inputs):
        raise ValueError('Output CSV must differ from all SQLite inputs')
    output.parent.mkdir(parents=True,exist_ok=True)
    headers=list(BASE_HEADERS)
    if phone_column: headers.insert(headers.index('best_phone'),'canonical_phone')
    headers.extend(REVIEW_COLUMNS)
    fd,temporary=tempfile.mkstemp(prefix='.'+output.name+'.',suffix='.tmp',dir=output.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8-sig',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=headers,extrasaction='ignore'); writer.writeheader()
            for row in rows: writer.writerow({key:_safe(row.get(key,'')) for key in headers})
        os.replace(temporary,output)
    except BaseException:
        try: os.unlink(temporary)
        except OSError: pass
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True); parser.add_argument('--pilot',required=True)
    parser.add_argument('--batch100',required=True); parser.add_argument('--big459',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--expected-total',type=int,default=EXPECTED_TOTAL)
    parser.add_argument('--expected-selected',type=int,default=EXPECTED_SELECTED)
    args=parser.parse_args(argv)
    try:
        rows,stats,phone_column,inputs=build(args.source,args.pilot,args.batch100,args.big459,
            args.expected_total,args.expected_selected)
        write_output(args.output,rows,phone_column,inputs)
    except (ValueError,OSError,sqlite3.Error) as exc:
        parser.exit(2,f'error: {exc}\n')
    for key,value in stats.items(): print(f'{key}={value}')
    print(f'OUTPUT={Path(args.output).expanduser().absolute()}')
    return 0


if __name__=='__main__': raise SystemExit(main())
