#!/usr/bin/env python3
"""Offline, evidence-only repair for the 468 retry-980 VERIFIED rows.

This never calls DDGS or HTTP.  It is deliberately a one-batch operation: the
selection is derived from the immutable retry reports, and every mutation is
journaled with before/after snapshots in ``discovery_repair_audit``.
"""
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database_lite as db
from discovery.ownership import evaluate_ownership, email_attribution, page_type

RUN = ROOT / 'logs/phase2_production_retry980_20260922_093027'
OUT = ROOT / 'logs/structural_ownership_20260922_155138'
BATCH = 'structural-ownership-20260922-155138'


def load_targets():
    targets = {}
    for path in sorted(RUN.glob('part*.json')):
        for row in json.loads(path.read_text())['companies']:
            site = row.get('website') or {}
            if site.get('status') == 'VERIFIED':
                targets[int(row['company_id'])] = site
    return targets


def load_assessments():
    data = json.loads((RUN / 'flagged59_analysis.json').read_text())
    return {int(x['company_id']): {
        'classification': x['classification'],
        'reason': x['strongest_evidence_against_first_party_ownership'],
        'audit_flag': x['original_audit_flag'],
    } for x in data['records']}


def row_dict(row):
    return dict(row) if row else {}


def audit(conn, company_id, table, record_id, action, reason, before, after):
    conn.execute('''INSERT INTO discovery_repair_audit
                 (batch_id,company_id,table_name,record_id,action,reason,before_json,after_json,created_at)
                 VALUES (?,?,?,?,?,?,?,?,?)''',
                 (BATCH, company_id, table, record_id, action, reason,
                  json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
                  datetime.now(timezone.utc).isoformat()))


def safe_evidence(raw):
    try:
        result = json.loads(raw or '[]')
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []


def safe_object(raw):
    try:
        result = json.loads(raw or '{}')
        return result if isinstance(result, dict) else {}
    except json.JSONDecodeError:
        return {}


def main():
    targets, assessments = load_targets(), load_assessments()
    if len(targets) != 468:
        raise SystemExit(f'Expected exactly 468 retry VERIFIED rows, found {len(targets)}')
    db.initialize_discovery_database()
    conn = db.get_connection()
    conn.row_factory = __import__('sqlite3').Row
    report = {'batch_id': BATCH, 'scope': 'stored evidence only; no network discovery',
              'selected_ids': sorted(targets), 'before': {}, 'after': {}, 'flagged59': [],
              'emails_before': 0, 'emails_after': 0, 'emails_removed': [], 'remaining_suspicious': []}
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            prior = conn.execute('SELECT count(*) FROM discovery_repair_audit WHERE batch_id=?', (BATCH,)).fetchone()[0]
            if prior:
                raise RuntimeError('This repair batch already has an audit trail; refusing a second mutation pass')
            marks = ','.join('?' * len(targets))
            sites = conn.execute(f'''SELECT w.*, c.company_name,c.tax_number,c.registration_number,c.address,c.municipality
                FROM website_discovery w JOIN companies_lite c ON c.id=w.company_id
                WHERE w.company_id IN ({marks})
                AND w.id IN (SELECT max(id) FROM website_discovery GROUP BY company_id)''', tuple(targets)).fetchall()
            if len(sites) != 468:
                raise RuntimeError(f'Expected 468 latest website rows, found {len(sites)}')
            report['emails_before'] = conn.execute(f'SELECT count(*) FROM email_discovery WHERE company_id IN ({marks})', tuple(targets)).fetchone()[0]
            for site in sites:
                s, company = row_dict(site), row_dict(site)
                before = row_dict(site)
                evidence = safe_evidence(site['evidence_json'])
                assessment = assessments.get(site['company_id'])
                owner = evaluate_ownership(company, evidence, targets[site['company_id']].get('candidate', {}).get('url',''),
                                           site['website'] or '', assessment=assessment,
                                           known_group=(site['relationship'] == 'GROUP_PARENT'))
                status = owner['status']
                relationship = owner['relationship']
                confidence = site['confidence'] if status == 'VERIFIED' else (40 if status == 'GROUP_REVIEW' else min(60, site['confidence'] or 0))
                scope = owner.get('scope','') if status == 'VERIFIED' else ''
                after = {'status': status, 'relationship': relationship, 'confidence': confidence,
                         'verified_scope': scope, 'ownership': owner}
                report['before'][str(site['company_id'])] = {'status': site['status'], 'confidence': site['confidence'], 'website':site['website']}
                report['after'][str(site['company_id'])] = after
                if assessment:
                    report['flagged59'].append({'company_id':site['company_id'], 'company_name':site['company_name'],
                        'before':'VERIFIED', 'after':status, 'classification':assessment['classification'],
                        'website':site['website'], 'reason':owner['reasons']})
                conn.execute('''UPDATE website_discovery SET status=?,relationship=?,confidence=?,verified_scope=?,ownership_json=?,
                              rule_version=?,checked_at=? WHERE id=?''',
                             (status, relationship, confidence, scope, json.dumps(owner,ensure_ascii=False),
                              'phase2-ownership-1', datetime.now(timezone.utc).isoformat(), site['id']))
                audit(conn, site['company_id'], 'website_discovery', site['id'], 'reclassified',
                      '; '.join(owner['reasons']), before, after)
                emails = conn.execute('SELECT * FROM email_discovery WHERE company_id=?', (site['company_id'],)).fetchall()
                for mail in emails:
                    mail_before = row_dict(mail); source = safe_object(mail['evidence_json'])
                    attribution = email_attribution(company, mail['email'], mail['page_url'] or '', source.get('publication',''),
                                                    owner, evidence, mail['page_title'] or '', source.get('contact_block'),
                                                    source.get('visible_email',''))
                    if not attribution['attributable']:
                        conn.execute('DELETE FROM email_discovery WHERE id=?', (mail['id'],))
                        audit(conn, site['company_id'], 'email_discovery', mail['id'], 'invalidated', attribution['reason'], mail_before,
                              {'attribution':attribution, 'removed':True})
                        report['emails_removed'].append({'company_id':site['company_id'], 'email':mail['email'], 'reason':attribution['reason']})
                    else:
                        updated = {**source, 'attribution': attribution, 'structural_repair_batch': BATCH}
                        conn.execute('UPDATE email_discovery SET evidence_json=?,checked_at=? WHERE id=?',
                                     (json.dumps(updated,ensure_ascii=False),datetime.now(timezone.utc).isoformat(),mail['id']))
                        audit(conn, site['company_id'], 'email_discovery', mail['id'], 'retained', attribution['reason'], mail_before,
                              {**mail_before, 'evidence_json':updated})
            # Static audit of only surviving VERIFIED rows.  A prior flagged
            # analyst assessment remains suspicious unless repaired by new evidence.
            for site in sites:
                after = report['after'][str(site['company_id'])]
                if after['status'] != 'VERIFIED':
                    continue
                typ = page_type(site['website'] or '', '', '', ())
                if site['company_id'] in assessments or typ in ('THIRD_PARTY','PROFILE','GROUP'):
                    report['remaining_suspicious'].append({'company_id':site['company_id'], 'website':site['website'], 'page_type':typ,
                                                           'flagged_before':site['company_id'] in assessments})
            report['emails_after'] = conn.execute(f'SELECT count(*) FROM email_discovery WHERE company_id IN ({marks})', tuple(targets)).fetchone()[0]
    finally:
        conn.close()
    counts = {}
    for item in report['after'].values(): counts[item['status']] = counts.get(item['status'],0)+1
    report['status_counts'] = counts
    report['companies_with_email_after'] = len({x['company_id'] for x in []})  # filled from a read-only connection below
    conn = db.get_connection()
    marks = ','.join('?' * len(targets))
    report['companies_with_email_after'] = conn.execute(f'SELECT count(DISTINCT company_id) FROM email_discovery WHERE company_id IN ({marks})', tuple(targets)).fetchone()[0]
    report['integrity_check'] = conn.execute('PRAGMA integrity_check').fetchone()[0]
    report['foreign_key_check'] = [tuple(x) for x in conn.execute('PRAGMA foreign_key_check')]
    conn.close()
    OUT.mkdir(exist_ok=True)
    (OUT/'repair_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    with (OUT/'flagged59_before_after.csv').open('w', newline='') as f:
        writer=csv.DictWriter(f, fieldnames=['company_id','company_name','website','classification','before','after','reason'])
        writer.writeheader(); writer.writerows(report['flagged59'])
    print(json.dumps({'targets':len(targets),'statuses':counts,'emails_before':report['emails_before'],
                      'emails_after':report['emails_after'],'removed':len(report['emails_removed']),
                      'suspicious':len(report['remaining_suspicious']),'integrity':report['integrity_check'],
                      'foreign_keys':len(report['foreign_key_check'])},ensure_ascii=False))


if __name__ == '__main__':
    main()
