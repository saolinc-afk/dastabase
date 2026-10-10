import csv
import hashlib
import json
import socket
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest

from discovery_v2.assertion_store import AssertionStore
from discovery_v2.human_review_benchmark import (
    build_benchmark, main, write_outputs,
)


REVIEW_HEADERS = (
    'SAMPLE_POSITION', 'company_name', 'tax_number', 'address',
    'ambiguity_category', 'ANDREJ_OFFICIAL_WEBSITE', 'ANDREJ_NOTES',
)
RAW_HEADERS = (
    'SAMPLE_POSITION', 'SAMPLE_SEED', 'SAMPLE_SOURCE',
    'canonical_company_id', 'company_name', 'tax_number',
    'registration_number', 'address', 'ambiguity_category',
    'candidate_count', 'registrable_domain_count', 'current_selected_website',
    'current_selected_domain', 'diagnostic_leader', 'leader_score',
    'leader_tier', 'margin_to_next_candidate', 'CANDIDATES_COMPACT',
    'EVIDENCE_PREVIEW',
)


def _xml(tag, attributes=None, text=None):
    node = ET.Element(tag, attributes or {})
    node.text = text
    return node


def _xlsx(path, review_rows, raw_rows):
    namespace = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    relationship = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    package = 'http://schemas.openxmlformats.org/package/2006/relationships'
    values = []
    indexes = {}
    for row in (REVIEW_HEADERS, *review_rows, RAW_HEADERS, *raw_rows):
        for value in row:
            value = str(value)
            if value not in indexes:
                indexes[value] = len(values); values.append(value)

    shared = _xml('{'+namespace+'}sst', {'count':str(len(values)),
        'uniqueCount':str(len(values))})
    for value in values:
        item = ET.SubElement(shared, '{'+namespace+'}si')
        ET.SubElement(item, '{'+namespace+'}t').text = value

    def column(index):
        result = ''
        while index >= 0:
            result = chr(index % 26 + 65) + result
            index = index // 26 - 1
        return result

    def sheet(rows):
        root = _xml('{'+namespace+'}worksheet')
        data = ET.SubElement(root, '{'+namespace+'}sheetData')
        for row_number, row in enumerate(rows, 1):
            row_node = ET.SubElement(data, '{'+namespace+'}row', {'r':str(row_number)})
            for index, value in enumerate(row):
                cell = ET.SubElement(row_node, '{'+namespace+'}c', {
                    'r':f'{column(index)}{row_number}', 't':'s'})
                ET.SubElement(cell, '{'+namespace+'}v').text = str(indexes[str(value)])
        return root

    workbook = _xml('{'+namespace+'}workbook')
    sheets = ET.SubElement(workbook, '{'+namespace+'}sheets')
    for number, name in enumerate(('REVIEW','RAW DATA'), 1):
        ET.SubElement(sheets, '{'+namespace+'}sheet', {'name':name,
            'sheetId':str(number), '{'+relationship+'}id':f'rId{number}'})
    rels = _xml('{'+package+'}Relationships')
    for number in (1,2):
        ET.SubElement(rels, '{'+package+'}Relationship', {'Id':f'rId{number}',
            'Type':relationship+'/worksheet',
            'Target':f'worksheets/sheet{number}.xml'})
    core = _xml('{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}coreProperties')
    ET.SubElement(core,
        '{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}lastModifiedBy').text = 'Andrej Test'
    modified = ET.SubElement(core, '{http://purl.org/dc/terms/}modified',
        {'{http://www.w3.org/2001/XMLSchema-instance}type':'dcterms:W3CDTF'})
    modified.text = '2026-10-10T20:04:40Z'
    content = _xml('{http://schemas.openxmlformats.org/package/2006/content-types}Types')

    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('xl/workbook.xml', ET.tostring(workbook,
            encoding='utf-8', xml_declaration=True))
        archive.writestr('xl/_rels/workbook.xml.rels', ET.tostring(rels,
            encoding='utf-8', xml_declaration=True))
        archive.writestr('xl/sharedStrings.xml', ET.tostring(shared,
            encoding='utf-8', xml_declaration=True))
        archive.writestr('xl/worksheets/sheet1.xml', ET.tostring(
            sheet((REVIEW_HEADERS, *review_rows)), encoding='utf-8', xml_declaration=True))
        archive.writestr('xl/worksheets/sheet2.xml', ET.tostring(
            sheet((RAW_HEADERS, *raw_rows)), encoding='utf-8', xml_declaration=True))
        archive.writestr('docProps/core.xml', ET.tostring(core,
            encoding='utf-8', xml_declaration=True))
        archive.writestr('[Content_Types].xml', ET.tostring(content,
            encoding='utf-8', xml_declaration=True))


@pytest.fixture
def reviewed(tmp_path):
    categories = ('ONE_CLEAR_EVIDENCE_LEADER',
                  'NO_MEANINGFUL_IDENTITY_SUPPORT',
                  'ONE_MODERATE_LEADER', 'THIRD_PARTY_NOISE_DOMINATES')
    review_rows = (
        ('1','Company One','SI1','Address 1',categories[0],
         'https://official-one.si/path', 'težko najti — exact comment'),
        ('2','Company Two','SI2','Address 2',categories[1],
         '', 'nisem našel www'),
        ('3','Company Three','SI3','Address 3',categories[2],
         'group.example', 'del korporacije; dve spletni strani'),
        ('4','Company Four','SI4','Address 4',categories[3],
         'easy-miss.si', 'lahko najti'),
    )
    raw_rows = []
    for position, category in enumerate(categories, 1):
        leader = ('official-one.si' if position == 1 else
                  'group.example' if position == 3 else f'noise-{position}.example')
        candidates = f'{leader} | url=https://{leader}/ | score=100'
        raw_rows.append((str(position),'seed','DERIVED_STRATIFIED',str(100+position),
            f'Company {position}',f'SI{position}',f'REG{position}',f'Address {position}',
            category,'1','1','','',leader,'100',category,'20',candidates,
            f'[{leader}] SEARCH title=stored title | snippet=stored snippet'))
    path = tmp_path/'reviewed.xlsx'
    _xlsx(path, review_rows, raw_rows)
    return path


def test_deterministic_import_exact_values_mapping_and_classification(reviewed):
    _, source, assertions, benchmark = build_benchmark(reviewed, expected_size=4)
    _, again_source, again_assertions, again_benchmark = build_benchmark(
        reviewed, expected_size=4)
    assert source == again_source
    assert assertions == again_assertions
    assert benchmark == again_benchmark
    assert [row['canonical_company_id'] for row in assertions] == [101,102,103,104]
    assert assertions[0]['asserted_value'] == 'https://official-one.si/path'
    assert assertions[0]['review_comment'] == 'težko najti — exact comment'
    assert assertions[0]['authority_level'] == 'HUMAN_VALIDATED'
    assert assertions[0]['authority_rank'] == 400
    assert assertions[1]['assertion_status'] == 'LIKELY_NO_OFFICIAL_WEBSITE'
    assert assertions[2]['assertion_status'] == 'COMPLEX_RELATIONSHIP'
    assert benchmark[0]['benchmark_outcome'] == 'CORRECT_DOMAIN_ALREADY_IN_STORED_EVIDENCE'
    assert benchmark[3]['benchmark_outcome'] == 'OFFICIAL_EXISTS_BUT_SEARCH_MISSED'
    assert all(row['candidate_preview_complete'] == 'Y' for row in benchmark)
    assert json.loads(assertions[0]['evidence_references_json'] if isinstance(
        assertions[0]['evidence_references_json'], str) else json.dumps(
            assertions[0]['evidence_references_json']))[0]['sheet'] == 'REVIEW'


def test_mapping_mismatch_rejected(tmp_path, reviewed):
    _, _, assertions, benchmark = build_benchmark(reviewed, expected_size=4)
    assert len(assertions) == len(benchmark) == 4
    review_rows = (('1','Company','WRONG','Address 1','ONE_CLEAR_EVIDENCE_LEADER',
                    'one.si',''),)
    raw_rows = (('1','seed','source','1','Company','SI1','REG1','Address 1',
        'ONE_CLEAR_EVIDENCE_LEADER','1','1','','','one.si','1','tier','1',
        'one.si | score=1','preview'),)
    broken = tmp_path/'broken.xlsx'; _xlsx(broken, review_rows, raw_rows)
    with pytest.raises(ValueError, match='tax_number mapping mismatch'):
        build_benchmark(broken, expected_size=1)


def test_truncated_candidate_preview_does_not_manufacture_search_miss(tmp_path):
    category = 'NO_MEANINGFUL_IDENTITY_SUPPORT'
    review_rows = (('1','Company','SI1','Address',category,
                    'human-found.si','praktično nemogoče najti brez ai'),)
    raw_rows = (('1','seed','source','1','Company','SI1','REG1','Address',category,
        '7','7','','','shown.example','1','tier','1',
        'shown.example | score=1','preview'),)
    workbook = tmp_path/'truncated.xlsx'; _xlsx(workbook, review_rows, raw_rows)
    _, _, _, benchmark = build_benchmark(workbook, expected_size=1)
    assert benchmark[0]['candidate_preview_complete'] == 'N'
    assert benchmark[0]['benchmark_outcome'] == 'HUMAN_REVIEW_UNRESOLVED'
    assert benchmark[0]['failure_mechanism'] == 'INSUFFICIENT_OR_TRUNCATED_EVIDENCE'


def test_duplicate_safe_append_only_store_and_conflict_preservation(tmp_path, reviewed):
    _, source, assertions, _ = build_benchmark(reviewed, expected_size=4)
    database = tmp_path/'assertions.sqlite3'
    with AssertionStore(database, create=True) as store:
        assert store.ingest(source, assertions) == {'inserted':4, 'skipped':0}
        assert store.ingest(source, assertions) == {'inserted':0, 'skipped':4}
        ai_source = {**source, 'source_id':'src_ai', 'source_type':'AI_ENRICHMENT',
            'source_identifier':'ai:test:1', 'actor_identifier':'model-test',
            'methodology':'AI_WEBSITE_RESOLUTION', 'methodology_version':'1'}
        ai_assertion = {**assertions[0], 'assertion_id':'assert_ai',
            'source_id':'src_ai', 'source_row_key':'job-1',
            'asserted_value':'https://conflicting.example',
            'normalized_values_json':['conflicting.example'],
            'authority_level':'AI_VALIDATED', 'authority_rank':100,
            'conflicts_with_json':[assertions[0]['assertion_id']]}
        assert store.ingest(ai_source, [ai_assertion]) == {'inserted':1, 'skipped':0}
        retained = store.assertions(101, 'OFFICIAL_WEBSITE')
        assert len(retained) == 2
        assert {row['asserted_value'] for row in retained} == {
            'https://official-one.si/path', 'https://conflicting.example'}
        assert max(row['authority_rank'] for row in retained) == 400


def test_zero_network_no_source_mutation_and_outputs(tmp_path, reviewed):
    before = hashlib.sha256(reviewed.read_bytes()).hexdigest()
    output = tmp_path/'output'
    with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
        _, source, assertions, benchmark = build_benchmark(reviewed, expected_size=4)
        paths = write_outputs(output, source, assertions, benchmark)
    assert before == hashlib.sha256(reviewed.read_bytes()).hexdigest()
    assert set(paths) == {'human_review_assertions.csv',
        'human_review_assertions.sqlite3', 'discovery_benchmark_100.csv',
        'DISCOVERY_BENCHMARK_REPORT.md', 'HUMAN_REVIEW_GROUND_TRUTH.md'}
    with (output/'human_review_assertions.csv').open(
            newline='', encoding='utf-8-sig') as handle:
        exported = list(csv.DictReader(handle))
    assert exported[0]['asserted_value'] == 'https://official-one.si/path'
    assert exported[0]['review_comment'] == 'težko najti — exact comment'
    assert json.loads(exported[0]['normalized_values_json']) == ['official-one.si']
    assert json.loads(exported[0]['evidence_references_json'])[0]['sheet'] == 'REVIEW'
    assert json.loads(exported[0]['source_metadata_json'])['reviewed_companies'] == 4
    connection = sqlite3.connect(output/'human_review_assertions.sqlite3')
    assert connection.execute('SELECT COUNT(*) FROM knowledge_assertions').fetchone()[0] == 4
    connection.close()
    with pytest.raises(ValueError, match='Output already exists'):
        write_outputs(output, source, assertions, benchmark)


def test_cli(reviewed, tmp_path):
    output = tmp_path/'cli'
    assert main(['--reviewed-xlsx',str(reviewed), '--output-dir',str(output),
                 '--expected-size','4']) == 0
    assert (output/'DISCOVERY_BENCHMARK_REPORT.md').exists()
