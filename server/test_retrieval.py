"""Behavioral regressions for bounded federation, thesis identity, persistence and originals."""
import concurrent.futures
import copy
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from retrieval import models as m, query as q, store, providers as p, engine, jobs, files, exports, conferences, repositories, records
from retrieval.transport import Budget, Client, SourceError, public_endpoint


def paper(source='openalex', ident='W1', title='Original thesis', **kw):
    return m.make_record(source, ident, title, authors=['Alice Researcher'], **kw)


def pdf_bytes(pages=2, text='Original thesis Alice Researcher Test University'):
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    for _ in range(pages):
        page = writer.add_blank_page(612, 792)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 50 700 Td (' + text + ') Tj ET').encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'CAMPUS_RESEARCH_DIR': self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class QueryTests(Base):
    def test_boolean_precedence_and_phrases(self):
        t = q.parse('title:"machine learning" OR diffusion AND NOT image')
        self.assertEqual(t[0], 'OR')
        self.assertEqual(t[1], ('TERM', 'title', 'machine learning'))
        self.assertEqual(t[2][0], 'AND')
        self.assertIn('TITLE("machine learning")', q.compile_query(t, 'scopus'))

    def test_nested_parentheses_and_chinese(self):
        t = q.parse('(燃烧 OR detonation) AND author:Wang')
        self.assertIn('AU="Wang"', q.compile_query(t, 'wos'))
        self.assertIn('燃烧', q.compile_query(t, 'datacite'))

    def test_invalid_queries(self):
        for v in ['', 'x\nsecret', 'title:', '(x', 'x)', 'x OR', 'unknown:x', '"unclosed']:
            with self.subTest(v=v), self.assertRaises(ValueError):
                q.parse(v)

    def test_boolean_and_field_cannot_silently_drop(self):
        for v in ['a OR b', 'NOT a', 'title:alpha']:
            with self.assertRaises(ValueError):
                q.compile_query(q.parse(v), 'crossref')

    def test_default_degree_scope(self):
        for level, wanted in [('doctoral', True), ('masters', True), ('unknown', True), ('other', False)]:
            r = paper(type='thesis', thesis={'level': level, 'status': 'awarded'})
            self.assertEqual(q.matches(r, {}, True), wanted)
        self.assertFalse(q.matches(paper(type='article'), {}, True))

    def test_award_status_and_missing_fields(self):
        r = paper(type='thesis', thesis={'level': 'doctoral', 'status': 'in_progress'})
        self.assertFalse(q.matches(r, {}, True))
        self.assertTrue(q.matches(r, {'include_in_progress': True}, True))
        self.assertFalse(q.matches(r, {'from_year': 2020}, True))
        self.assertFalse(q.matches(r, {'institution': 'MIT'}, True))

    def test_filter_validation(self):
        for f in [{'arbitrary': 1}, {'from_year': True}, {'from_year': 2025, 'to_year': 2020}, {'degree': 'phd'}, {'sort': 'citations'}, {'include_unknown': 'yes'}]:
            with self.assertRaises(ValueError):
                q.filters(f)


class ModelTests(Base):
    def test_reference_lists_keep_database_provenance_after_merge(self):
        a = p.normalize_openalex({'id': 'https://openalex.org/W1', 'doi': '10.1234/a', 'title': 'Title', 'referenced_works': ['https://openalex.org/W2']})
        b = p.normalize_crossref({'DOI': '10.1234/a', 'title': ['Title'], 'reference': [{'DOI': '10.1234/b'}]})
        one = store.save_record(a, m.identity(a))
        two = store.save_record(b, m.identity(b))
        out = records.related(one['record_id'], 'references', 'crossref', 2)
        self.assertEqual(out['relations'][0]['doi'], '10.1234/b')
        self.assertEqual(two['references_by_source']['openalex'][0]['id'], 'W2')
    def test_source_id_stays_stable_when_doi_is_enriched(self):
        a = paper()
        one = store.save_record(a, m.identity(a))
        b = paper(identifiers={'doi': '10.1234/one'})
        two = store.save_record(b, m.identity(b))
        self.assertEqual(one['record_id'], two['record_id'])
        three = store.save_record(paper('crossref', 'doi', identifiers={'doi': '10.1234/one'}), 'work:doi:10.1234/one')
        self.assertEqual(three['record_id'], one['record_id'])

    def test_degree_not_inferred_from_datacite_general_type(self):
        r = p.normalize_datacite({'id': '10.1234/one', 'attributes': {'doi': '10.1234/one', 'types': {'resourceTypeGeneral': 'Dissertation', 'bibtex': 'phdthesis'}, 'titles': [{'title': 'A thesis'}]}})
        self.assertEqual(r['type'], 'thesis')
        self.assertEqual(r['thesis']['level'], 'unknown')

    def test_explicit_degrees(self):
        for v, expected in [('博士学位论文', 'doctoral'), ('碩士', 'masters'), ('Ph.D.', 'doctoral'), ('Bachelor thesis', 'other'), ('Dissertation', 'unknown')]:
            self.assertEqual(m.degree(v), expected)

    def test_openaire_classification_and_language(self):
        r = p.normalize_openaire({'id': 'o1', 'mainTitle': 'A thesis', 'language': {'code': 'eng'}, 'instances': [{'type': 'Master thesis', 'urls': ['https://dspace.mit.edu/handle/1/2']}], 'descriptions': ['Abstract']})
        self.assertEqual(r['thesis']['level'], 'masters')
        self.assertEqual(r['language'], 'eng')
        self.assertFalse(r['thesis']['classification_verified_at_origin'])

    def test_openaire_nullable_authors_pids_and_urls(self):
        r = p.normalize_openaire({'id': 'one', 'authors': None, 'pids': None, 'originalIds': None, 'instances': [{'type': 'Thesis', 'urls': None}]})
        self.assertEqual(r['authors'], [])
        self.assertEqual(r['thesis']['level'], 'unknown')

    def test_openaire_conflicting_degree_is_unknown(self):
        r = p.normalize_openaire({'id': 'o', 'instances': [{'type': 'Doctoral thesis'}, {'type': 'Master thesis'}]})
        self.assertEqual(r['thesis']['level'], 'unknown')
        self.assertTrue(r['thesis']['classification_conflict'])

    def test_exact_doi_merge_preserves_source_counts_and_abstracts(self):
        a = paper(identifiers={'doi': '10.1234/X'}, citations={'openalex': {'count': 3}}, abstract=p.abstract('One', 'openalex'))
        b = paper('crossref', '10.1234/x', identifiers={'doi': '10.1234/x'}, citations={'crossref': {'count': 7}}, abstract=p.abstract('Two', 'crossref'))
        x = store.save_record(a, m.identity(a))
        y = store.save_record(b, m.identity(b))
        self.assertEqual(x['record_id'], y['record_id'])
        self.assertEqual(y['citations']['openalex']['count'], 3)
        self.assertEqual(y['citations']['crossref']['count'], 7)
        self.assertEqual(y['abstract']['text'], 'One')
        self.assertEqual(y['abstract_variants'][0]['text'], 'Two')

    def test_thesis_derivative_same_doi_not_collapsed(self):
        a = paper(identifiers={'doi': '10.1234/x'}, type='thesis')
        b = paper('crossref', 'x', identifiers={'doi': '10.1234/x'}, type='article')
        self.assertNotEqual(m.identity(a), m.identity(b))

    def test_title_alone_does_not_merge(self):
        a = store.save_record(paper(), 'work:openalex:W1')
        b = store.save_record(paper('core', '2'), 'work:core:2')
        out = m.candidate_relations([a, b])
        self.assertNotEqual(a['record_id'], b['record_id'])
        self.assertFalse(out[0]['relations'][0]['verified'])

    def test_openalex_abstract_reconstruction_and_locations(self):
        r = p.normalize_openalex({'id': 'https://openalex.org/W1', 'title': 'A', 'abstract_inverted_index': {'learning': [1], 'machine': [0]}, 'locations': [{'pdf_url': 'https://arxiv.org/pdf/1234.56789', 'is_oa': True}], 'open_access': {'is_oa': True}})
        self.assertEqual(r['abstract']['text'], 'machine learning')
        self.assertEqual(r['access']['status'], 'open')

    def test_key_urls_refused(self):
        for u in ['https://x.test/a?api_key=private', 'https://x.test/a?X-Amz-Signature=private', 'javascript:alert(1)', 'https://u:private@x.test/a']:
            self.assertEqual(m.safe_url(u), '')


class TransportTests(Base):
    def test_ieee_inactive_account_is_actionable_and_redacted(self):
        c = Client()
        c.opener = Mock()
        c.opener.open.side_effect = urllib.error.HTTPError('https://ieeexploreapi.ieee.org/?apikey=PRIVATE', 403, 'PRIVATE', Message(), io.BytesIO(b'<h1>Developer Inactive</h1>'))
        with self.assertRaisesRegex(SourceError, 'api_developer_inactive'):
            c.get('ieee', 'https://ieeexploreapi.ieee.org/api/v1/search/articles')
        evidence = store.capability('ieee')
        self.assertEqual(evidence['error'], 'api_developer_inactive')
        self.assertNotIn('PRIVATE', str(evidence))

    def fake_response(self, body=b'{"ok":true}', mime='application/json'):
        r = io.BytesIO(body)
        r.headers = Message()
        r.headers['Content-Type'] = mime
        return r

    def test_fixed_endpoints(self):
        for u in ['http://api.openalex.org/works', 'https://api.openalex.org.evil.test/works', 'https://localhost/x', 'https://api.openalex.org:444/works']:
            with self.assertRaises(SourceError):
                public_endpoint(u)

    def test_shared_cache_and_rate_across_clients(self):
        a, b = Client(Budget()), Client(Budget())
        a.opener = Mock()
        a.opener.open.return_value = self.fake_response()
        self.assertEqual(a.get('openalex', 'https://api.openalex.org/works'), {'ok': True})
        b.opener = Mock()
        self.assertEqual(b.get('openalex', 'https://api.openalex.org/works'), {'ok': True})
        b.opener.open.assert_not_called()
        self.assertEqual(b.budget.used, 0)

    def test_html_not_json(self):
        c = Client()
        c.opener = Mock()
        c.opener.open.return_value = self.fake_response(b'<html>login</html>', 'text/html')
        with self.assertRaisesRegex(SourceError, 'expected_json'):
            c.get('openalex', 'https://api.openalex.org/works')

    def test_error_redaction_and_retry_after(self):
        c = Client(Budget(2, 2))
        c.opener = Mock()
        headers = Message()
        headers['Retry-After'] = '999999'
        c.opener.open.side_effect = urllib.error.HTTPError('https://api.openalex.org/?key=PRIVATE', 429, 'PRIVATE', headers, io.BytesIO(b'PRIVATE'))
        with self.assertRaises(SourceError) as err:
            c.get('openalex', 'https://api.openalex.org/works')
        self.assertEqual(str(err.exception), 'source_rate_limit')
        self.assertNotIn('PRIVATE', str(store.capability('openalex')))

    def test_credential_echo_refused(self):
        c = Client()
        c.opener = Mock()
        c.opener.open.return_value = self.fake_response(b'{"echo":"PRIVATE"}')
        with self.assertRaisesRegex(SourceError, 'credential_echo_refused'):
            c.get('openalex', 'https://api.openalex.org/works', secret_values=('PRIVATE',))

    def test_xml_entities_refused(self):
        c = Client()
        c.opener = Mock()
        c.opener.open.return_value = self.fake_response(b'<!DOCTYPE x [<!ENTITY z SYSTEM "file:///etc/passwd">]><x>&z;</x>', 'application/xml')
        with self.assertRaisesRegex(SourceError, 'unsafe_xml'):
            c.get('pubmed', 'https://eutils.ncbi.nlm.nih.gov/x', kind='xml')

    def test_external_pubmed_dtd_not_loaded(self):
        c = Client()
        c.opener = Mock()
        c.opener.open.return_value = self.fake_response(b'<!DOCTYPE PubmedArticleSet SYSTEM "https://example.test/dtd"><PubmedArticleSet/>', 'application/xml')
        result = c.get('pubmed', 'https://eutils.ncbi.nlm.nih.gov/x', kind='xml')
        self.assertNotIn('DOCTYPE', result)

    def test_budget_cancellation_and_requests(self):
        b = Budget(requests=1)
        b.take()
        with self.assertRaisesRegex(SourceError, 'request_budget'):
            b.take()
        with self.assertRaisesRegex(SourceError, 'cancelled'):
            Budget(cancelled=lambda: True).check()

    def test_rate_slot_reservation_is_atomic(self):
        def reserve(_):
            store.reserve_rate('test', .005, time.monotonic() + 3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(reserve, range(8)))
        with store.connect() as db:
            self.assertGreater(db.execute('SELECT next_at FROM rate WHERE provider="test"').fetchone()[0], time.time() - .1)


class FederationTests(Base):
    def test_old_and_new_registry_capabilities(self):
        c = {x['id']: x for x in p.catalogue(lambda _: '')}
        self.assertEqual(c['wanfang']['status'], 'needs_key')
        self.assertEqual(c['cnki']['status'], 'needs_institutional_api')
        self.assertFalse(c['cqvip']['queryable'])
        self.assertTrue(c['datacite']['queryable'])

    def test_partial_success_not_empty_success(self):
        def source(src, *args, **kwargs):
            if src == 'openalex':
                return p.response([paper()], 1)
            raise SourceError('authentication_required', 401)
        with patch.object(p, 'search', side_effect=source):
            out = engine.search('test', sources='openalex,datacite')
        self.assertTrue(out['ok'])
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(out['source_status']['datacite']['http_status'], 401)
        self.assertEqual(out['record_count'], 1)
        self.assertEqual(out['source_status']['openalex']['source_query'], 'test')

    def test_exact_merge_rrf_without_count_sum(self):
        def source(src, *args, **kwargs):
            return p.response([paper(src, 'id', identifiers={'doi': '10.1234/a'}, citations={src: {'count': 5}})], 1)
        with patch.object(p, 'search', side_effect=source):
            out = engine.search('test', sources='openalex,crossref')
        self.assertEqual(out['record_count'], 1)
        self.assertAlmostEqual(out['results'][0]['ranking']['score'], 2 / 61)
        self.assertEqual(len(out['results'][0]['citations']), 2)

    def test_continuation_uses_frozen_cursor_and_dedups(self):
        seen = []
        def source(src, tree, f, cursor, *args):
            seen.append(cursor)
            return p.response([paper(ident=str(cursor))], 2, 'next' if cursor is None else None)
        with patch.object(p, 'search', side_effect=source):
            one = engine.search('test', sources='openalex')
            two = engine.search(continuation_id=one['run_id'])
        self.assertEqual(seen, [None, 'next'])
        self.assertEqual(two['record_count'], 2)
        with self.assertRaises(ValueError):
            engine.search(query='changed', continuation_id=one['run_id'])

    def test_unknown_thesis_degree_separate(self):
        r = paper(type='thesis')
        with patch.object(p, 'search', return_value=p.response([r])):
            out = engine.search('test', 'theses', 'datacite')
        self.assertEqual(out['results'], [])
        self.assertEqual(len(out['unknown_degree_results']), 1)

    def test_disabled_sources_do_not_query(self):
        with patch.object(p, 'search') as call:
            out = engine.search('test', 'theses', 'cnki,cqvip,wanfang')
        call.assert_not_called()
        self.assertFalse(out['ok'])

    def test_deep_repeating_cursor_stops(self):
        checkpoints = []
        with patch.object(p, 'search', return_value=p.response([paper()], 100, 'same')):
            out = engine.deep(engine.plan('test', sources='openalex'), lambda _: '', lambda: False, checkpoints.append)
        self.assertEqual(out['source_status']['openalex:0']['error'], 'repeating_source_cursor')
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(len(out['results']), 1)

    def test_deep_cancel_preserves_snapshot(self):
        old = {'run_id': 's_test', 'results': [paper()], 'source_status': {}, 'created_at': time.time()}
        # Persisted real records have stable IDs; cancellation happens before ingestion.
        old['results'][0]['record_id'] = 'r_test'
        out = engine.deep(engine.plan('test', sources='openalex'), lambda _: '', lambda: True, lambda _: None, old)
        self.assertEqual(out['stop_reason'], 'cancelled')
        self.assertEqual(len(out['results']), 1)

    def test_variant_queries_are_recorded_and_executed(self):
        calls = []
        def source(src, tree, *args):
            calls.append(tree)
            return p.response([])
        with patch.object(p, 'search', side_effect=source):
            out = engine.deep(engine.plan('燃烧', sources='datacite', query_variants=['combustion']), lambda _: '', lambda: False, lambda _: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(out['plan']['query_variants'], ['combustion'])


class ProviderTests(Base):
    def test_core_canonical_endpoint_and_language_schema(self):
        client = self.client({'results': [{'id': 1, 'title': 'Thesis', 'yearPublished': '2024', 'documentType': ['doctoral thesis'], 'language': {'code': 'en', 'name': 'English'}}], 'totalHits': 5})
        out = p.search('core', q.parse('machine learning'), {}, '', 2, client, lambda _: 'test-credential', True)
        self.assertEqual(client.get.call_args.args[1], 'https://api.core.ac.uk/v3/search/works/')
        self.assertIn('documentType:thesis', client.get.call_args.args[2]['q'])
        self.assertEqual(out['records'][0]['language'], 'en')
        self.assertEqual(out['records'][0]['thesis']['level'], 'doctoral')
        self.assertTrue(q.matches(out['records'][0], {'from_year': 2020}, True))
        self.assertEqual(out['next_cursor'], '2')

    def test_core_normal_search_preserves_outputs_and_no_type_rewrite(self):
        client = self.client({'results': [], 'totalHits': 0})
        p.search('core', q.parse('machine learning'), {}, '', 2, client, lambda _: 'test-credential')
        self.assertEqual(client.get.call_args.args[1], 'https://api.core.ac.uk/v3/search/outputs/')
        self.assertEqual(client.get.call_args.args[2]['q'], 'machine learning')

    def test_core_work_and_output_ids_cannot_collide_without_doi(self):
        work = p.normalize_core({'id': 42, 'title': 'A thesis', 'documentType': 'thesis'}, 'works')
        output = p.normalize_core({'id': 42, 'title': 'Another thesis', 'documentType': ['thesis']})
        self.assertNotEqual(m.identity(work), m.identity(output))
        self.assertEqual(work['identifiers']['core_work'], '42')
        self.assertEqual(output['source_records']['core']['id'], '42')
        self.assertEqual(work['source_records']['core']['id'], 'work:42')

    def test_core_generic_thesis_does_not_imply_phd_and_null_fields_work(self):
        record = p.normalize_core({'id': 42, 'documentType': 'thesis', 'authors': None, 'language': None, 'sourceFulltextUrls': None}, 'works')
        self.assertEqual(record['thesis']['level'], 'unknown')
        self.assertFalse(record['thesis']['classification_verified_at_origin'])
        self.assertEqual(record['authors'], [])
        self.assertFalse(q.matches(record, {'degree': 'doctoral'}, True))
        self.assertTrue(q.matches(record, {}, True))

    def test_core_thesis_pagination_keeps_subject_filter_and_entity(self):
        client = self.client({'results': [{'id': 42, 'title': 'A thesis', 'documentType': 'thesis'}], 'totalHits': 5})
        out = p.search('core', q.parse('machine learning'), {}, '2', 2, client, lambda _: 'test-credential', True)
        params = client.get.call_args.args[2]
        self.assertEqual(params['offset'], 2)
        self.assertEqual(params['q'], '(machine learning) AND (documentType:thesis OR documentType:dissertation)')
        self.assertEqual(out['next_cursor'], '4')
        self.assertEqual(out['upstream_entity'], 'works')
        self.assertEqual(out['type_filter_mode'], 'server_side')

    def test_core_failed_works_search_does_not_silently_broaden_query(self):
        client = self.client({})
        client.get.side_effect = SourceError('upstream_http_error', 500)
        with self.assertRaises(SourceError):
            p.search('core', q.parse('machine learning'), {}, '', 2, client, lambda _: 'test-credential', True)
        self.assertEqual(client.get.call_count, 1)

    def test_keyed_optional_searches_have_working_query_compilers(self):
        for provider, payload in [('ieee', {'articles': [], 'total_records': 0}), ('semanticscholar', {'data': [], 'total': 0})]:
            c = self.client(payload)
            out = p.search(provider, q.parse('machine learning'), {}, '', 2, c, lambda _: 'test-credential')
            self.assertEqual(out['records'], [])
            self.assertIn('https://', c.get.call_args.args[1])

    def test_recommendations_are_not_citation_edges(self):
        r = store.save_record(paper(identifiers={'doi': '10.1234/a'}), 'work:doi:10.1234/a')
        c = self.client({'recommendedPapers': [{'title': 'Related', 'externalIds': {'DOI': '10.1234/b'}}]})
        out = records.related(r['record_id'], 'similar', 'semanticscholar', 2, lambda _: 'test-credential', c)
        self.assertFalse(out['relations'][0]['is_citation_edge'])
    def client(self, result):
        c = Mock()
        c.get.return_value = result
        c.budget = Budget()
        return c

    def test_openaire_uses_labels_not_vocabulary_numbers(self):
        c = self.client({'results': [], 'header': {}})
        p.search('openaire', q.parse('machine'), {'degree': 'masters'}, None, 2, c, lambda _: '', True)
        self.assertIn('instance_type:Master thesis', c.get.call_args.args[2]['filter'])

    def test_datacite_pagination(self):
        c = self.client({'data': [], 'meta': {'total': 10}, 'links': {}})
        p.search('datacite', q.parse('machine'), {}, '10', 5, c, lambda _: '', True)
        self.assertEqual(c.get.call_args.args[2]['page[number]'], 3)

    def test_epmc_cursor_is_not_integer(self):
        c = self.client({'resultList': {'result': []}, 'nextCursorMark': 'next'})
        r = p.search('europepmc', q.parse('machine'), {}, 'opaque-token', 2, c, lambda _: '')
        self.assertEqual(r['next_cursor'], 'next')

    def test_conference_filter_not_ignored_by_general_source(self):
        with self.assertRaisesRegex(SourceError, 'conference_filter_unsupported'):
            p.search('openalex', q.parse('test'), {'conferences': 'icml'}, None, 2, self.client({}), lambda _: '')

    def test_openreview_rejects_submission_not_accepted(self):
        c = self.client({'notes': [{'id': 'one', 'content': {'title': {'value': 'machine'}, 'venueid': {'value': 'ICLR.cc/2024/Conference/Rejected_Submission'}}}]})
        r = conferences.search('openreview', 'machine', {'conferences': 'iclr', 'from_year': 2024, 'to_year': 2024}, '', 2, c)
        self.assertEqual(r['records'], [])

    def test_openreview_accepted_evidence(self):
        c = self.client({'notes': [{'id': 'one', 'content': {'title': {'value': 'machine learning'}, 'venueid': {'value': 'ICLR.cc/2024/Conference'}, 'authors': {'value': ['A']}, 'pdf': {'value': '/pdf/one'}}}]})
        r = conferences.search('openreview', 'machine', {'conferences': 'iclr', 'from_year': 2024, 'to_year': 2024}, '', 2, c)
        self.assertTrue(r['records'][0]['evidence'][0]['conference_acceptance_verified'])

    def test_unregistered_conference_year_explicit(self):
        with self.assertRaisesRegex(SourceError, 'not_registered'):
            conferences.search('proceedings', 'test', {'from_year': 2026, 'to_year': 2026}, '', 2, self.client({}))

    def test_workshop_and_findings_paths_excluded(self):
        self.assertFalse(conferences.paper_path('acl', 'https://aclanthology.org/2024.findings-acl.1/', 2024, 'acl'))
        self.assertFalse(conferences.paper_path('cvf', 'https://openaccess.thecvf.com/content/CVPR2024W/html/x.html', 2024, 'cvpr'))

    def test_acl_doi_maps_to_official_anthology_id(self):
        c = self.client('unused')
        candidate = {'title': 'A Transformer Paper', 'doi': '10.18653/V1/2024.ACL-LONG.95', 'official_links': []}
        record = paper('proceedings', 'one', title=candidate['title'])
        with patch.object(conferences, 'indexed_candidates', return_value={'records': [candidate], 'has_more': False}), patch.object(conferences, 'metadata', return_value=record) as page:
            out = conferences.search('proceedings', 'transformer', {'conferences': 'acl', 'from_year': 2024, 'to_year': 2024}, '', 1, c)
        self.assertEqual(page.call_args.args[0], 'https://aclanthology.org/2024.acl-long.95/')
        self.assertEqual(len(out['records']), 1)

    def test_official_pmlr_index_parser(self):
        page = conferences.Page('https://proceedings.mlr.press/v235/')
        page.feed('<p class="title">Machine Learning</p><a href="one24a.html">abs</a>')
        self.assertEqual(page.links[0][2], 'Machine Learning')


class PersistenceTests(Base):
    def test_task_dedup_and_terminal_cancel(self):
        a = jobs.enqueue('download', {'record_id': 'r_one'})
        b = jobs.enqueue('download', {'record_id': 'r_one'})
        self.assertEqual(a['job_id'], b['job_id'])
        self.assertEqual(jobs.cancel(a['job_id'])['state'], 'cancelled')

    def test_atomic_claim_and_lease_recovery(self):
        jid = jobs.enqueue('search', {'plan': {}})['job_id']
        self.assertEqual(jobs.claim('search')['id'], jid)
        self.assertIsNone(jobs.claim('search'))
        with store.connect() as db:
            db.execute('UPDATE jobs SET lease=? WHERE id=?', (time.time() - 1, jid))
        jobs.recover()
        self.assertEqual(jobs.status(jid)['state'], 'queued')

    def test_checkpoint_keeps_run_id_on_cancel(self):
        jid = jobs.enqueue('search', {})['job_id']
        jobs.claim('search')
        snap = {'plan': {'thesis_only': False}, 'run_id': 's_one', 'results': [], 'source_status': {}, 'requests_used': 1, 'status': 'running', 'stop_reason': None}
        jobs.checkpoint(jid, snap)
        jobs.cancel(jid)
        self.assertEqual(jobs.status(jid)['result']['run_id'], 's_one')

    def test_job_input_limits(self):
        for jid, wait in [('bad', 0), ('j_' + 'a' * 48, 21)]:
            with self.assertRaises(ValueError):
                jobs.status(jid, wait)

    def test_cache_expiry(self):
        store.cache_put('x', {'x': 1}, ttl=-1)
        self.assertIsNone(store.cached('x'))


class FileTests(Base):
    def test_network_retry_drops_partial_original(self):
        path = Path(self.tmp.name) / 'partial.part'
        error = SourceError('download_connection_or_idle_timeout')
        error.retry_after = 0
        calls = []
        def transfer(*args):
            self.assertFalse(path.exists())
            calls.append(True)
            if len(calls) == 1:
                path.write_bytes(b'partial')
                raise error
            path.write_bytes(b'whole')
            return {'ok': True}
        with patch.object(files, 'stream', side_effect=transfer):
            files.stream_with_retry('https://arxiv.org/pdf/x', path, time.monotonic() + 5, lambda _: None, lambda: False, 100)
        self.assertEqual(path.read_bytes(), b'whole')

    def test_signed_mit_delivery_only_after_official_redirect(self):
        url = 'https://cf003.cdn.4science.cloud/4s-assetstore-prod--mit--dspace/file?Expires=1&Signature=opaque&Key-Pair-Id=public-id'
        with self.assertRaises(SourceError):
            files.checked_url(url, False)
        self.assertEqual(files.checked_url(url, False, signed_delivery=True), url)
        with self.assertRaises(SourceError):
            files.checked_url(url.replace('/4s-assetstore-prod--mit--dspace/', '/other-bucket/'), False, signed_delivery=True)
    def test_multiple_original_volumes_all_delivered_with_manifest(self):
        r = store.save_record(paper(type='thesis'), 'thesis:openalex:W1')
        locations = [{'url': 'https://dspace.mit.edu/a', 'repository_file_relation_verified': True, 'part_id': 'one'},
                     {'url': 'https://dspace.mit.edu/b', 'repository_file_relation_verified': True, 'part_id': 'two'}]
        def one(*args):
            return {'file': files.save_bytes(b'original', 'part.pdf', 'application/pdf')}
        with patch.object(files, 'resolve', return_value={'locations': locations}), patch.object(files, 'download_one', side_effect=one):
            out = files.download(r['record_id'])
        self.assertEqual(len(out['files']), 2)
        self.assertTrue(out['complete_volume_set'])
        manifest = json.loads(files.read_native(out['manifest']['file_id'], out['manifest']['file_name']))
        self.assertEqual(len(manifest['original_volumes']), 2)

    def test_url_ssrf_and_host_allowlist(self):
        for u in ['https://127.0.0.1/x', 'https://arxiv.org.evil.test/x', 'http://arxiv.org/pdf/x', 'https://arxiv.org/../x', 'https://arxiv.org/pdf/x?token=private', 'https://raw.githubusercontent.com/other/repo/main/x.pdf']:
            with self.assertRaises(SourceError):
                files.checked_url(u, False)
        self.assertEqual(files.checked_url('https://arxiv.org/pdf/1706.03762', False), 'https://arxiv.org/pdf/1706.03762')

    def test_private_dns_answer_refused(self):
        with patch('socket.getaddrinfo', return_value=[(0, 0, 0, '', ('192.168.1.1', 443))]), self.assertRaises(SourceError):
            files.checked_url('https://arxiv.org/pdf/x')

    def test_pdf_identity_and_single_page_preview(self):
        path = Path(self.tmp.name) / 'one.pdf'
        path.write_bytes(pdf_bytes())
        r = paper(type='thesis', thesis={'level': 'doctoral', 'institution': 'Test University'})
        out = files.verify_pdf(path, r, {})
        self.assertEqual(out['pdf_page_count'], 2)
        path.write_bytes(pdf_bytes(1))
        with self.assertRaisesRegex(SourceError, 'single_page'):
            files.verify_pdf(path, r, {})

    def test_wrong_identity_and_truncation(self):
        path = Path(self.tmp.name) / 'one.pdf'
        path.write_bytes(pdf_bytes(text='Different work'))
        with self.assertRaisesRegex(SourceError, 'identity_unverified'):
            files.verify_pdf(path, paper(), {})
        path.write_bytes(pdf_bytes()[:-30])
        with self.assertRaisesRegex(SourceError, 'complete_pdf'):
            files.verify_pdf(path, paper(), {})

    def test_scanned_pdf_requires_exact_repository_relation(self):
        path = Path(self.tmp.name) / 'one.pdf'
        path.write_bytes(pdf_bytes(text=''))
        with self.assertRaises(SourceError):
            files.verify_pdf(path, paper(), {})
        out = files.verify_pdf(path, paper(), {'repository_file_relation_verified': True, 'persistent_identifier_verified': True})
        self.assertIn('scanned', out['identity_method'])

    def test_original_native_file_hash_and_expiry(self):
        info = files.save_bytes(b'original\x00bytes', 'one.txt', 'text/plain')
        self.assertEqual(files.read_native(info['file_id'], 'one.txt'), b'original\x00bytes')
        self.assertFalse(info['space_saved'])
        (files.directory() / (info['file_id'] + '.bin')).write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            files.read_native(info['file_id'], 'one.txt')

    def test_large_file_kept_whole_without_native_link(self):
        fid = files.reserve()
        path = files.directory() / (fid + '.part')
        with path.open('wb') as out:
            out.truncate(files.MAX_NATIVE + 1)
        info = files.commit(fid, path, {'file_name': 'large.pdf', 'mime_type': 'application/pdf'})
        self.assertFalse(info['native_attachment_available'])
        self.assertIsNone(files.link(info))
        self.assertIn('prepare_command', info['retrieval'])
        self.assertEqual(info['byte_size'], files.MAX_NATIVE + 1)

    def test_quota_reserved_across_concurrent_jobs(self):
        def attempt(_):
            try:
                return files.reserve()
            except SourceError:
                return None
        with patch.object(files, 'QUOTA', 3 * files.MAX_THESIS):
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                ids = list(pool.map(attempt, range(6)))
            self.assertEqual(sum(v is not None for v in ids), 3)

    def test_unregistered_fulltext_stays_official_link(self):
        r = paper(access={'status': 'open', 'locations': [{'url': 'https://unknown.example/paper.pdf', 'role': 'pdf'}]})
        r = store.save_record(r, m.identity(r))
        resolved = files.resolve(r['record_id'])
        self.assertFalse(resolved['downloadable_route_found'])
        self.assertEqual(len(resolved['unavailable_locations']), 1)


class ImportExportTests(Base):
    def test_ris_import_preserves_unknown_degree(self):
        out = exports.import_refs('TY  - THES\nTI  - A thesis\nAU  - Wang\nPY  - 2024\nPB  - A university\nER  - \n')
        r = out['records'][0]
        self.assertEqual(r['thesis']['level'], 'unknown')
        self.assertFalse(out['metadata_independently_verified'])

    def test_bibtex_nested_literal_and_degree(self):
        out = exports.import_refs('@mastersthesis{x,title={A {Nested} title},author={Alice and Bob},year=2024,school={MIT}}', 'bibtex')
        self.assertEqual(out['records'][0]['thesis']['level'], 'masters')
        self.assertEqual(len(out['records'][0]['authors']), 2)

    def test_macro_and_unclosed_import_refused(self):
        for content, fmt in [('@article{x,title=MACRO}', 'bibtex'), ('TY  - THES\nTI  - X', 'ris')]:
            with self.assertRaises(ValueError):
                exports.import_refs(content, fmt)

    def test_csv_formula_escaped(self):
        self.assertEqual(exports.csv_cell('=cmd'), "'=cmd")
        self.assertEqual(exports.csv_cell(' @SUM(A1)'), "' @SUM(A1)")

    def test_unknown_thesis_export_not_phd(self):
        r = paper(type='thesis')
        r['record_id'] = 'r_one'
        self.assertTrue(exports.bib_record(r).startswith('@misc'))

    def test_run_export_matches_actual_query_and_provenance(self):
        with patch.object(p, 'search', return_value=p.response([paper()])):
            out = engine.search('test', sources='openalex')
        info = exports.export(out['run_id'], 'json')
        saved = json.loads(files.read_native(info['file_id'], info['file_name']))
        self.assertEqual(saved['plan']['query'], 'test')
        self.assertEqual(saved['results'][0]['title'], 'Original thesis')


class RepositoryTests(Base):
    def test_oai_thesis_type_and_deleted(self):
        xml = '''<record xmlns="http://www.openarchives.org/OAI/2.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><header><identifier>oai:test:1</identifier><datestamp>2024-01-01</datestamp></header><metadata><dc:dc><dc:title>A thesis</dc:title><dc:creator>Alice</dc:creator><dc:type>Master thesis</dc:type><dc:identifier>https://dspace.mit.edu/handle/1721.1/1</dc:identifier></dc:dc></metadata></record>'''
        ident, r, deleted = repositories.oai_record(ET.fromstring(xml), 'mit')
        self.assertFalse(deleted)
        self.assertEqual(r['thesis']['level'], 'masters')
        self.assertEqual(r['identifiers']['handle'], '1721.1/1')
        node = ET.fromstring(xml.replace('<header>', '<header status="deleted">'))
        self.assertTrue(repositories.oai_record(node, 'mit')[2])

    def test_unharvested_repository_not_empty_success(self):
        with self.assertRaisesRegex(SourceError, 'no_verified_repository'):
            repositories.search_index('test', 2, None)


if __name__ == '__main__':
    unittest.main()
