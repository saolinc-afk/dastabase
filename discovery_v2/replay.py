"""Deterministic offline replay of recorded Discovery v2 evidence."""
import json
from dataclasses import dataclass
from pathlib import Path

from discovery.domain_generator import normalize_domain, normalize_url
from discovery.ownership import in_scope
from discovery_v2 import ENGINE_VERSION, RULE_VERSION
from discovery_v2.candidates import (brand_match, eligible, fused_candidates,
                                     primary_rank)
from discovery_v2.contacts import resolve_contacts, select_default
from discovery_v2.evidence import EvidenceWriter
from discovery_v2.interfaces import evaluate_website
from discovery_v2.models import Config
from discovery_v2.ownership import validate_assessment, usable
from discovery_v2.search_resolver import (RESOLVED, assessment_from_resolution,
                                           resolve_search_evidence)
from discovery_v2.store import (APPLICATION_ID, SCHEMA_VERSION, Store, encode,
                                now, readonly, snapshot)

@dataclass
class RecordedResponse:
    url: str
    text: str
    status_code: int


class RecordedOnlyFetcher:
    """Fetcher-compatible adapter with no network implementation or fallback."""
    def __init__(self, writer, pages):
        self.writer = writer
        self.pages = pages
        self.responses = dict(pages)
        self.failures = {}
        self.errors = []
        self.missing_urls = []
        self.requests = 0

    def fetch(self, url, allowed_site=None):
        self.requests += 1
        key = normalize_url(url)
        if allowed_site and not in_scope(key, allowed_site):
            self.missing_urls.append(key)
            return None
        response = self.pages.get(key)
        if response is None:
            self.missing_urls.append(key)
            return None
        return response

    def close(self):
        pass


def _source_attempt(conn, run_id, company_row):
    if company_row['selected_attempt_id']:
        return company_row['selected_attempt_id']
    row = conn.execute("""SELECT attempt_id FROM discovery_attempts
        WHERE run_id=? AND company_id=? ORDER BY attempt_number DESC LIMIT 1""",
        (run_id, company_row['company_id'])).fetchone()
    return row['attempt_id'] if row else None


def _lineage_payload(writer, evidence_id, source_path, source_run_id, source_evidence_id):
    row = writer.evidence_rows[evidence_id]
    payload = dict(row.get('evidence_payload') or {})
    payload['offline_replay_source'] = {
        'artifact_path': str(source_path), 'run_id': source_run_id,
        'evidence_id': source_evidence_id,
    }
    row['evidence_payload'] = payload
    writer.store.conn.execute("UPDATE discovery_evidence SET evidence_payload_json=? WHERE evidence_id=?",
                              (encode(payload), evidence_id))


def _reconstruct(writer, source_conn, source_path, source_run_id, source_attempt):
    """Run current extractors over immutable raw evidence in original row order."""
    pages = {}
    source_map = {}
    rows = source_conn.execute("""SELECT rowid,* FROM discovery_evidence
        WHERE attempt_id=? ORDER BY rowid""", (source_attempt,)).fetchall()
    for stored in rows:
        row = dict(stored)
        payload = json.loads(row['evidence_payload_json'])
        kind = row['source_kind']
        if kind == 'SEARCH_RESULT':
            item = dict(payload)
            item.update(url=row['result_url'] or item.get('url', ''),
                        title=row['title'] or item.get('title', ''),
                        body=row['snippet_body'] or item.get('body', item.get('snippet', '')),
                        position=row['result_rank'])
            evidence_id = writer.search_result(item, row['query_type'] or 'RECORDED',
                row['query_text'] or '', row['provider'] or 'recorded', row['result_rank'] or 1)
        elif kind == 'FETCHED_PAGE':
            html = payload.get('html')
            if not isinstance(html, str) or not row['final_url']:
                raise ValueError('Recorded fetched page lacks replayable HTML or final URL')
            response = RecordedResponse(row['final_url'], html, row['http_status'] or 200)
            evidence_id = writer.page(row['requested_url'] or row['final_url'], response)
            pages[normalize_url(row['final_url'])] = response
            pages[row['final_url']] = response
            if row['requested_url']:
                pages.setdefault(normalize_url(row['requested_url']), response)
                pages.setdefault(row['requested_url'], response)
        elif kind == 'GENERATED_CANDIDATE':
            evidence_id = writer.generated(row['requested_url'])['evidence_id']
        else:
            fields = {key: row[key] for key in (
                'provider', 'query_type', 'query_text', 'result_rank', 'result_url',
                'result_host', 'title', 'snippet_body', 'requested_url', 'final_url',
                'http_status', 'content_hash') if row.get(key) is not None}
            evidence_id = writer.evidence(kind, row['source_class'], payload=payload, **fields)
        source_map[row['evidence_id']] = evidence_id
        _lineage_payload(writer, evidence_id, source_path, source_run_id, row['evidence_id'])
    return pages, source_map


def _review_after_missing(assessment, missing):
    return {**assessment, 'verified': False, 'usable': False, 'status': 'REVIEW', 'verified_scope': None,
            'relationship': 'AMBIGUOUS',
            'ownership': {'status': 'REVIEW', 'scope': None, 'relationship': 'AMBIGUOUS',
                          'reasons': ['OFFLINE_EVIDENCE_MISSING'], 'ownership_evidence': []},
            'p1a': {**assessment.get('p1a', {}), 'verified': False, 'status': 'REVIEW',
                    'rule_id': None, 'confidence_rule_id': None, 'verification_scope': None,
                    'blockers': sorted(set(assessment.get('p1a', {}).get('blockers', [])) |
                                       {'OFFLINE_EVIDENCE_MISSING'}),
                    'offline_missing_urls': sorted(set(missing))}}


def replay_company(store, run_id, company, source_conn, source_path, source_run_id,
                   source_attempt, config):
    attempt_id = store.start_attempt(run_id, company['id'])
    context = {'run_id': run_id, 'company_id': company['id'], 'attempt_id': attempt_id}
    writer = EvidenceWriter(store, context, company)
    pages, source_map = _reconstruct(writer, source_conn, source_path, source_run_id,
                                     source_attempt)
    fetcher = RecordedOnlyFetcher(writer, pages)
    candidates = fused_candidates(company, writer.observations, writer.evidence_rows)
    resolution = resolve_search_evidence(company, writer.observations,
                                         writer.evidence_rows)
    diagnostics = {'mode': 'OFFLINE_REPLAY', 'network_disabled': True,
                   'source_attempt_id': source_attempt, 'source_evidence_map': source_map,
                   'website_candidates': [], 'missing_urls': [],
                   'search_evidence_resolution': resolution}
    selected = None
    terminal_hosts = set()
    evaluated_urls = set()
    evaluated_count = 0
    for bundle in candidates:
        for observation in bundle['observations']:
            url = normalize_url(observation['normalized_value'])
            host = normalize_domain(url)
            if not url or url in evaluated_urls or host in terminal_hosts:
                continue
            evaluated_urls.add(url)
            if not eligible(company, url, observation.get('value', {}).get('title', ''),
                            observation.get('value', {}).get('body', '')):
                diagnostics['website_candidates'].append({'url': url,
                    'observation_id': observation['observation_id'], 'status': 'INELIGIBLE'})
                continue
            if (observation['extraction_method'] != 'domain_guess'
                    and not brand_match(company, url)
                    and not observation.get('value', {}).get('identity_match')):
                continue
            if evaluated_count >= config.max_candidates:
                continue
            before = len(fetcher.missing_urls)
            assessment = evaluate_website(company, url, fetcher)
            missing = fetcher.missing_urls[before:]
            recorded_host = any(normalize_domain(recorded_url) == host
                                for recorded_url in pages)
            if (not usable(assessment) and missing and not recorded_host
                    and resolution['status'] == RESOLVED
                    and host == resolution['candidate_domain']):
                assessment = assessment_from_resolution(resolution)
            elif missing:
                assessment = _review_after_missing(assessment, missing)
            else:
                # An unavailable offline hypothesis must not consume the host slot
                # or budget before a recorded path candidate on that host is seen.
                evaluated_count += 1
            if usable(assessment) and not validate_assessment(
                    company, url, assessment, writer, fetcher):
                raise ValueError('Invalid P1A ownership authorization during replay')
            diagnostics['website_candidates'].append({'url': url,
                'observation_id': observation['observation_id'],
                'domain_evidence': bundle['evidence'], 'status': assessment['status'],
                'assessment': assessment})
            if usable(assessment):
                candidate_priority = primary_rank(
                    company, observation, assessment,
                    writer.evidence_rows.get(observation['evidence_id']))
                if (selected is None or candidate_priority < primary_rank(
                        company, selected[0], selected[1],
                        writer.evidence_rows.get(selected[0]['evidence_id']))):
                    selected = (observation, assessment, assessment['verified_scope'])
                if assessment['status'] == 'VERIFIED':
                    terminal_hosts.add(host)
    diagnostics['missing_urls'] = sorted(set(fetcher.missing_urls))
    owner = selected[1]['ownership'] if selected else None
    contacts = resolve_contacts(company, writer.observations, owner, fetcher.responses)
    evidence_ids = []
    if selected:
        evidence_ids = [selected[0]['evidence_id']]
        evidence_ids.extend(selected[1]['p1a']['supporting_evidence_ids'])
        evidence_ids.extend(eid for (url, _), eid in writer.pages.items()
                            if in_scope(url, selected[2]))
        evidence_ids = list(dict.fromkeys(evidence_ids))
    result = dict(
        website_status=selected[1]['status'] if selected else ('REVIEW' if any(
            c['status'] == 'REVIEW' for c in diagnostics['website_candidates']) else 'NOT_FOUND'),
        official_website=selected[2] if selected else None,
        verified_scope=selected[2] if selected else None,
        website_observation_id=selected[0]['observation_id'] if selected else None,
        website_evidence_ids_json=encode(evidence_ids),
        website_assessment_json=encode({'mode': 'OFFLINE_REPLAY',
            'network_disabled': True, 'candidates': diagnostics['website_candidates']}),
        default_email_contact_id=select_default(contacts, 'EMAIL'),
        default_phone_contact_id=select_default(contacts, 'PHONE'),
        contact_outcome='FOUND' if any(c['attribution_status'] == 'ATTRIBUTED'
                                      for c in contacts) else 'NO_ATTRIBUTED_CONTACT')
    store.progress(attempt_id, diagnostics, {'search_calls': 0, 'http_requests': 0})
    store.complete(context, contacts, result)


def replay(source_results, source_run_id, results):
    source_path = Path(source_results).resolve(strict=True)
    destination = Path(results).resolve()
    if destination == source_path:
        raise ValueError('Replay destination must differ from source artifact')
    if destination.exists():
        raise ValueError('Replay destination must be a new database')
    before = snapshot(source_path, 'offline-replay:' + source_run_id)
    source = readonly(source_path)
    store = None
    sidecar = Path(str(destination) + '.replay.json')
    owns_destination = False
    owns_sidecar = False
    if sidecar.exists():
        source.close()
        raise ValueError('Replay lineage destination must be a new file')
    try:
        if (source.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or
                source.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
            raise ValueError('Source is not a compatible Discovery v2 result database')
        source_run = source.execute('SELECT * FROM discovery_runs WHERE run_id=?',
                                    (source_run_id,)).fetchone()
        if source_run is None:
            raise ValueError('Unknown source run')
        source_companies = source.execute("""SELECT * FROM discovery_run_companies
            WHERE run_id=? ORDER BY manifest_position""", (source_run_id,)).fetchall()
        if not source_companies:
            raise ValueError('Source run has no companies')
        companies = [json.loads(row['identity_snapshot_json']) for row in source_companies]
        source_config = Config(**json.loads(source_run['config_json']))
        store = Store(destination, source=source_path, create=True, require_new=True)
        owns_destination = True
        run_id = store.create_run(before, companies, source_config)
        with store.conn:
            for source_row in source_companies:
                store.conn.execute("""UPDATE discovery_run_companies
                    SET eligibility_status=?,eligibility_reason=?,status=?
                    WHERE run_id=? AND company_id=?""",
                    (source_row['eligibility_status'], source_row['eligibility_reason'],
                     'INELIGIBLE' if source_row['eligibility_status'] == 'INELIGIBLE' else 'PENDING',
                     run_id, source_row['company_id']))
            store.conn.execute("UPDATE discovery_runs SET status='RUNNING',started_at=? WHERE run_id=?",
                               (now(), run_id))
        for source_row, company in zip(source_companies, companies):
            if source_row['eligibility_status'] == 'INELIGIBLE':
                continue
            source_attempt = _source_attempt(source, source_run_id, source_row)
            if source_attempt is None:
                raise ValueError(f'Company {source_row["company_id"]} has no recorded attempt')
            replay_company(store, run_id, company, source, source_path, source_run_id,
                           source_attempt, source_config)
        with store.conn:
            store.conn.execute("UPDATE discovery_runs SET status='COMPLETED',finished_at=? WHERE run_id=?",
                               (now(), run_id))
        after = snapshot(source_path, 'offline-replay:' + source_run_id)
        if after != before:
            raise ValueError('Source artifact changed during replay')
        lineage = {'replay_mode': 'OFFLINE_REPLAY', 'network_disabled': True,
            'source_artifact_path': str(source_path), 'source_sha256': before['sha256'],
            'source_size_bytes': before['size_bytes'], 'source_run_id': source_run_id,
            'destination_path': str(destination), 'destination_run_id': run_id,
            'engine_version': ENGINE_VERSION, 'rule_version': RULE_VERSION,
            'replayed_at': now()}
        with sidecar.open('x', encoding='utf-8') as handle:
            owns_sidecar = True
            handle.write(encode(lineage) + '\n')
        return {'run_id': run_id, 'source_run_id': source_run_id,
                'companies': len(companies), 'status': 'COMPLETED',
                'lineage': str(sidecar)}
    except BaseException:
        if store:
            store.close()
            store = None
        # Destination creation is atomic in intent: an unsuccessful replay is
        # not a valid artifact. Leave source and unrelated files untouched.
        if owns_destination and destination.exists():
            destination.unlink()
        if owns_sidecar and sidecar.exists():
            sidecar.unlink()
        raise
    finally:
        source.close()
        if store:
            store.close()
