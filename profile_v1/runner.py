"""Offline Profile Activity runner and replay CLI."""
import argparse, json
from pathlib import Path

from profile_v1.contracts import CandidateClaim, Citation, EmptyInterpreter, JsonInterpreter
from profile_v1.importer import discovery_manifest, file_hash, import_company
from profile_v1.store import Store, encode, now, uid
from profile_v1.validation import validate_and_store


def serialize_candidates(candidates):
    return [{'claim_type':c.claim_type,'normalized_value':c.normalized_value,
             'display_value':c.display_value,'confidence':c.confidence,
             'ambiguity_note':c.ambiguity_note,
             'citations':[{'block_id':x.block_id,'quote':x.quote} for x in c.citations]}
            for c in candidates]


def create(discovery_db, discovery_run_id, company_ids, output):
    descriptor, companies=discovery_manifest(discovery_db,discovery_run_id,company_ids)
    store=Store(output,create=True,require_new=True)
    try: return store.create_run(descriptor,companies)
    finally: store.close()


def _publish(store, run_id, company_id, attempt_id, candidates, version):
    context={'run_id':run_id,'attempt_id':attempt_id,'company_id':company_id}
    with store.conn:
        store.conn.execute('UPDATE profile_attempts SET interpreter_output_json=? WHERE attempt_id=?',
                           (encode(serialize_candidates(candidates)),attempt_id))
        supported,rejected=validate_and_store(store,context,candidates,version)
        types={r['claim_type'] for r in store.conn.execute(
            "SELECT claim_type FROM profile_claims WHERE attempt_id=? AND status='SUPPORTED'",(attempt_id,))}
        if {'ACTUAL_PRIMARY_ACTIVITY','BUSINESS_DESCRIPTION'} <= types: profile_status='PROFILED'
        elif supported: profile_status='PARTIAL'
        elif rejected: profile_status='REVIEW'
        else: profile_status='INSUFFICIENT_EVIDENCE'
        confidences=[r[0] for r in store.conn.execute(
            "SELECT confidence FROM profile_claims WHERE attempt_id=? AND status='SUPPORTED'",(attempt_id,))]
        overall=('LOW' if 'LOW' in confidences else 'MEDIUM' if 'MEDIUM' in confidences else
                 'HIGH' if confidences else None)
        registered=store.conn.execute('SELECT registered_activity_json FROM profile_run_companies WHERE run_id=? AND company_id=?',(run_id,company_id)).fetchone()[0]
        counts=store.conn.execute('''SELECT
          (SELECT COUNT(*) FROM profile_evidence WHERE attempt_id=?),
          (SELECT COUNT(*) FROM profile_content_blocks WHERE attempt_id=?)''',(attempt_id,attempt_id)).fetchone()
        store.insert('profile_company_results',dict(result_id=uid(),run_id=run_id,attempt_id=attempt_id,
            company_id=company_id,profile_status=profile_status,registered_activity_json=registered,
            supported_claim_ids_json=encode(supported),rejected_claim_ids_json=encode(rejected),
            overall_confidence=overall,evidence_count=counts[0],block_count=counts[1],
            claim_count=len(supported),completed_at=now()))
        attempt_status={'PROFILED':'COMPLETED','PARTIAL':'PARTIAL','REVIEW':'REVIEW',
                        'INSUFFICIENT_EVIDENCE':'INSUFFICIENT_EVIDENCE'}[profile_status]
        store.conn.execute('UPDATE profile_attempts SET status=?,finished_at=? WHERE attempt_id=?',(attempt_status,now(),attempt_id))
        store.conn.execute('UPDATE profile_run_companies SET status=?,selected_attempt_id=? WHERE run_id=? AND company_id=?',
                           (attempt_status,attempt_id,run_id,company_id))
    return profile_status


def _blocks(store,attempt_id):
    return [dict(r) for r in store.conn.execute('SELECT * FROM profile_content_blocks WHERE attempt_id=? ORDER BY rowid',(attempt_id,))]


def run(results_db, run_id, interpreter=None):
    interpreter=interpreter or EmptyInterpreter(); store=Store(results_db)
    try:
        runrow=store.conn.execute('SELECT * FROM profile_runs WHERE run_id=?',(run_id,)).fetchone()
        if not runrow: raise ValueError('Unknown Profile run')
        descriptor=json.loads(runrow['source_descriptor_json'])
        if file_hash(descriptor['database_path']) != descriptor['sha256']: raise ValueError('Discovery source hash mismatch')
        with store.conn: store.conn.execute("UPDATE profile_runs SET status='RUNNING',started_at=COALESCE(started_at,?) WHERE run_id=?",(now(),run_id))
        rows=store.conn.execute("SELECT * FROM profile_run_companies WHERE run_id=? AND status='PENDING' ORDER BY manifest_position",(run_id,)).fetchall()
        for row in rows:
            company=json.loads(row['identity_snapshot_json']); attempt=store.start_attempt(run_id,row['company_id'])
            try:
                import_company(store,run_id,attempt,company,descriptor['database_path'],descriptor['run_id'],row['discovery_attempt_id'],row['discovery_result_id'])
                candidates=interpreter.interpret(company,_blocks(store,attempt))
                _publish(store,run_id,row['company_id'],attempt,candidates,interpreter.version)
            except Exception as error:
                with store.conn:
                    store.conn.execute("UPDATE profile_attempts SET status='FAILED',finished_at=?,diagnostics_json=? WHERE attempt_id=?",(now(),encode({'error':str(error)}),attempt))
                    store.conn.execute("UPDATE profile_run_companies SET status='FAILED' WHERE run_id=? AND company_id=?",(run_id,row['company_id']))
                raise
        _finish_run(store,run_id)
    finally: store.close()


def _finish_run(store,run_id):
    statuses=[r[0] for r in store.conn.execute('SELECT status FROM profile_run_companies WHERE run_id=?',(run_id,))]
    status='COMPLETED' if statuses and all(x=='COMPLETED' for x in statuses) else 'PARTIAL'
    with store.conn: store.conn.execute('UPDATE profile_runs SET status=?,finished_at=? WHERE run_id=?',(status,now(),run_id))


def replay(results_db, run_id, interpreter=None):
    """Create new attempts solely from the Profile artifact's copied corpus."""
    store=Store(results_db)
    try:
        rows=store.conn.execute('SELECT * FROM profile_run_companies WHERE run_id=? ORDER BY manifest_position',(run_id,)).fetchall()
        if not rows: raise ValueError('Unknown or empty Profile run')
        for row in rows:
            old=row['selected_attempt_id']
            if not old: raise ValueError('Profile company has no selected attempt to replay')
            company=json.loads(row['identity_snapshot_json']); attempt=store.start_attempt(run_id,row['company_id'])
            evidence_map={}; block_map={}
            with store.conn:
                for source in store.conn.execute('SELECT * FROM profile_evidence WHERE attempt_id=? ORDER BY rowid',(old,)).fetchall():
                    values=dict(source); evidence_map[source['evidence_id']]=values['evidence_id']=uid()
                    values.update(attempt_id=attempt,run_id=run_id); store.insert('profile_evidence',values)
                for source in store.conn.execute('SELECT * FROM profile_content_blocks WHERE attempt_id=? ORDER BY rowid',(old,)).fetchall():
                    values=dict(source); block_map[source['block_id']]=values['block_id']=uid()
                    values.update(attempt_id=attempt,run_id=run_id,evidence_id=evidence_map[source['evidence_id']]); store.insert('profile_content_blocks',values)
            if interpreter is None:
                raw=json.loads(store.conn.execute('SELECT interpreter_output_json FROM profile_attempts WHERE attempt_id=?',(old,)).fetchone()[0])
                candidates=[CandidateClaim(x['claim_type'],x.get('normalized_value'),x.get('display_value',''),
                    tuple(Citation(block_map[c['block_id']],c['quote']) for c in x.get('citations',[])),
                    x.get('confidence','MEDIUM'),x.get('ambiguity_note')) for x in raw]
                version='recorded-replay-1'
            else:
                candidates=interpreter.interpret(company,_blocks(store,attempt)); version=interpreter.version
            _publish(store,run_id,row['company_id'],attempt,candidates,version)
        _finish_run(store,run_id)
    finally: store.close()


def main(argv=None):
    parser=argparse.ArgumentParser(prog='python -m profile_v1.runner'); sub=parser.add_subparsers(dest='command',required=True)
    create_p=sub.add_parser('create'); create_p.add_argument('--discovery-results',required=True); create_p.add_argument('--discovery-run-id',required=True); create_p.add_argument('--company-id',type=int,action='append',required=True); create_p.add_argument('--output',required=True)
    run_p=sub.add_parser('run'); run_p.add_argument('--results',required=True); run_p.add_argument('--run-id',required=True); run_p.add_argument('--interpretations')
    replay_p=sub.add_parser('replay'); replay_p.add_argument('--results',required=True); replay_p.add_argument('--run-id',required=True)
    args=parser.parse_args(argv)
    if args.command=='create': print(create(args.discovery_results,args.discovery_run_id,args.company_id,args.output))
    elif args.command=='run':
        interpreter=JsonInterpreter(json.loads(Path(args.interpretations).read_text())) if args.interpretations else EmptyInterpreter(); run(args.results,args.run_id,interpreter)
    else: replay(args.results,args.run_id)


if __name__=='__main__': main()
