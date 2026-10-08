"""Read-only CSV and JSON review export for Profile Activity artifacts."""
import argparse, csv, json, sqlite3
from pathlib import Path
from profile_v1 import APPLICATION_ID, SCHEMA_VERSION

REVIEW_FIELDS=['PRIMARY_ACTIVITY_CORRECT','PRODUCTS_SERVICES_CORRECT','AUDIENCE_CORRECT',
 'MANUFACTURER_CORRECT','INTERNATIONAL_CORRECT','DESCRIPTION_USEFUL_FOR_SALES',
 'UNSUPPORTED_CLAIMS','OVERALL_USEFUL','REVIEW_NOTES']
FIELDS=['company_id','company_name','official_website','profile_status','registered_activity','actual_primary_activity',
        'industry_category','products','services','business_audience','customer_types',
        'manufacturer_signal','international_signal','business_description','overall_confidence',
        'supported_claim_count','rejected_claim_count','evidence_count','block_count',
        'diagnostic_category']+REVIEW_FIELDS
MAP={'ACTUAL_PRIMARY_ACTIVITY':'actual_primary_activity','INDUSTRY_CATEGORY':'industry_category',
     'PRODUCTS':'products','SERVICES':'services','BUSINESS_AUDIENCE':'business_audience',
     'CUSTOMER_TYPES':'customer_types','MANUFACTURER_SIGNAL':'manufacturer_signal',
     'INTERNATIONAL_SIGNAL':'international_signal','BUSINESS_DESCRIPTION':'business_description'}


def load(path,run_id):
    conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True); conn.row_factory=sqlite3.Row
    try:
        version=conn.execute('PRAGMA user_version').fetchone()[0]
        if conn.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID or version not in (2,3,SCHEMA_VERSION): raise ValueError('Incompatible Profile result database')
        if not conn.execute('SELECT 1 FROM profile_runs WHERE run_id=?',(run_id,)).fetchone(): raise ValueError('Unknown Profile run')
        output=[]
        for item in conn.execute('''SELECT rc.*,r.* FROM profile_run_companies rc JOIN profile_company_results r
          ON r.attempt_id=rc.selected_attempt_id WHERE rc.run_id=? ORDER BY rc.manifest_position''',(run_id,)):
            identity=json.loads(item['identity_snapshot_json']); claims=[]; values={key:[] for key in MAP.values()}
            for claim in conn.execute("SELECT * FROM profile_claims WHERE attempt_id=? AND status='SUPPORTED' ORDER BY rowid",(item['attempt_id'],)):
                citations=[dict(x) for x in conn.execute('SELECT * FROM profile_claim_evidence WHERE claim_id=?',(claim['claim_id'],))]
                detail=dict(claim); detail['normalized_value']=json.loads(detail.pop('normalized_value_json')); detail['citations']=citations; claims.append(detail)
                values[MAP[claim['claim_type']]].append(claim['display_value'])
            rejected=len(json.loads(item['rejected_claim_ids_json']))
            row={'company_id':item['company_id'],'company_name':identity.get('company_name') or identity.get('name'),
                 'official_website':item['accepted_website'] or '',
                 'profile_status':item['profile_status'],'registered_activity':json.dumps(json.loads(item['registered_activity_json']),ensure_ascii=False) if item['registered_activity_json'] else '',
                 'overall_confidence':item['overall_confidence'] or '','evidence_count':item['evidence_count'],
                 'block_count':item['block_count'],'supported_claim_count':item['claim_count'],
                 'rejected_claim_count':rejected,'diagnostic_category':item['diagnostic_category']}
            row.update({key:' | '.join(entries) for key,entries in values.items()})
            row.update({key:'' for key in REVIEW_FIELDS})
            output.append({'summary':row,'claims':claims})
        return output
    finally: conn.close()


def export(path,run_id,csv_path,json_path=None):
    rows=load(path,run_id)
    with Path(csv_path).open('w',newline='',encoding='utf-8-sig') as handle:
        writer=csv.DictWriter(handle,fieldnames=FIELDS); writer.writeheader(); writer.writerows(x['summary'] for x in rows)
    if json_path: Path(json_path).write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def main(argv=None):
    parser=argparse.ArgumentParser(); parser.add_argument('--results',required=True); parser.add_argument('--run-id',required=True); parser.add_argument('--csv',required=True); parser.add_argument('--json')
    args=parser.parse_args(argv); export(args.results,args.run_id,args.csv,args.json)


if __name__=='__main__': main()
