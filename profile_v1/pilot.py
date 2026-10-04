"""Deterministic frozen-pilot selection from persisted Discovery evidence."""
import argparse, hashlib, json
from pathlib import Path

from discovery.ownership import in_scope
from profile_v1.importer import file_hash, readonly
from profile_v1.runner import create
from profile_v1.store import encode

PILOT_POLICY = 'PILOT20_DISCOVERY_EVIDENCE_V1'
USABLE = {'VERIFIED','HIGH','MEDIUM'}


def select(source,run_id,count=20):
    resolved,conn=readonly(source)
    try:
        rows=[]
        query='''SELECT rc.company_id,rc.manifest_position,rc.identity_snapshot_json,
          rc.selected_attempt_id,r.result_id,r.website_status,r.official_website,r.verified_scope
          FROM discovery_run_companies rc JOIN discovery_company_results r
          ON r.run_id=rc.run_id AND r.company_id=rc.company_id AND r.attempt_id=rc.selected_attempt_id
          WHERE rc.run_id=? ORDER BY rc.manifest_position'''
        for row in conn.execute(query,(run_id,)):
            if row['website_status'] not in USABLE or not row['official_website'] or not row['verified_scope']: continue
            pages=[]
            for evidence in conn.execute("SELECT * FROM discovery_evidence WHERE attempt_id=? AND source_kind='FETCHED_PAGE'",(row['selected_attempt_id'],)):
                url=evidence['final_url'] or evidence['requested_url']
                payload=json.loads(evidence['evidence_payload_json'])
                if url and in_scope(url,row['verified_scope']) and payload.get('html'): pages.append(evidence)
            if not pages: continue
            item=dict(row); item['pages']=len(pages)
            item['content_bytes']=sum(len(json.loads(x['evidence_payload_json']).get('html','')) for x in pages)
            rows.append(item)
        if len(rows)<count: raise ValueError(f'Only {len(rows)} companies have suitable persisted Discovery evidence')
        # Alternate evidence-density bands, then stable-hash within each band. This
        # deliberately includes sparse and rich sites without inspecting outcomes.
        ranked=sorted(rows,key=lambda x:(x['content_bytes'],x['manifest_position']))
        bands=[ranked[i::4] for i in range(4)]; ordered=[]
        for band in bands:
            band.sort(key=lambda x:hashlib.sha256(f"{run_id}:{x['company_id']}".encode()).hexdigest())
        while len(ordered)<count:
            for band in bands:
                if band and len(ordered)<count: ordered.append(band.pop(0))
        companies=[{'company_id':x['company_id'],'company_name':json.loads(x['identity_snapshot_json']).get('company_name'),
          'manifest_position':x['manifest_position'],'website_status':x['website_status'],
          'official_website':x['official_website'],'fetched_page_count':x['pages'],
          'persisted_content_bytes':x['content_bytes']} for x in ordered]
        manifest={'pilot':'PROFILE_ACTIVITY_PILOT20','selection_policy':PILOT_POLICY,
          'source_database':str(Path(source).expanduser()),'source_sha256':file_hash(resolved),'discovery_run_id':run_id,
          'company_count':count,'companies':companies}
        manifest['manifest_sha256']=hashlib.sha256(encode(companies).encode()).hexdigest()
        return manifest
    finally: conn.close()


def freeze(source,run_id,output,count=20):
    path=Path(output)
    if path.exists(): raise ValueError('Pilot manifest already exists and will not be replaced')
    manifest=select(source,run_id,count); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return manifest


def create_from_manifest(manifest_path,output):
    manifest=json.loads(Path(manifest_path).read_text())
    if hashlib.sha256(encode(manifest['companies']).encode()).hexdigest()!=manifest['manifest_sha256']: raise ValueError('Pilot manifest hash mismatch')
    if file_hash(manifest['source_database'])!=manifest['source_sha256']: raise ValueError('Pilot Discovery source hash mismatch')
    return create(manifest['source_database'],manifest['discovery_run_id'],
                  [x['company_id'] for x in manifest['companies']],output)


def main(argv=None):
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest='command',required=True)
    select_p=sub.add_parser('freeze'); select_p.add_argument('--discovery-results',required=True); select_p.add_argument('--discovery-run-id',required=True); select_p.add_argument('--output',required=True); select_p.add_argument('--count',type=int,default=20)
    create_p=sub.add_parser('create'); create_p.add_argument('--manifest',required=True); create_p.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    if args.command=='freeze': print(freeze(args.discovery_results,args.discovery_run_id,args.output,args.count)['manifest_sha256'])
    else: print(create_from_manifest(args.manifest,args.output))


if __name__=='__main__': main()
