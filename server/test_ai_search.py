import copy
import io
import json
import os
import unittest
import urllib.error
import urllib.parse
from email.message import Message
from unittest.mock import patch
import ai_search as ai
from article_downloads import DownloadError
import research_sources as research

FEED='''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:x="http://arxiv.org/schemas/atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/">
<o:totalResults>8</o:totalResults><entry><id>http://arxiv.org/abs/1706.03762v7</id><title>Attention Is All You Need</title>
<author><name>A Researcher</name></author><summary>Useful abstract.</summary><published>2017-06-12T00:00:00Z</published>
<updated>2023-01-01T00:00:00Z</updated><category term="cs.CL"/><x:primary_category term="cs.CL"/>
<x:doi>10.5555/test</x:doi><x:comment>Accepted at a conference (author supplied)</x:comment>
<x:journal_ref>NIPS 2017</x:journal_ref></entry></feed>'''.encode()

def binding(**updates):
    data={'paper':'https://dblp.org/rec/conf/nips/Example17','title':'Attention Is All You Need.',
        'year':'2017','publication_year':'2017','conference':'neurips','stream':'https://dblp.org/streams/conf/nips',
        'venue':'NIPS','authors':'A Researcher | B Researcher','dois':'https://doi.org/10.5555/test',
        'documents':'https://papers.nips.cc/paper/123 | https://openreview.net/forum?id=example'}
    data.update(updates)
    return {key:{'type':'literal','value':value} for key,value in data.items()}

def payload(*rows):return json.dumps({'results':{'bindings':list(rows)}}).encode()

class Response(io.BytesIO):
    status=200
    def __init__(self,raw,mime):
        super().__init__(raw);self.headers=Message();self.headers['Content-Type']=mime

class AISearchTests(unittest.TestCase):
    def setUp(self):
        self.saved_next=ai._next.copy();self.saved_cache=copy.deepcopy(ai._cache)
        for key in ai.ENDPOINTS:ai._next[key]=0;ai._cache[key].clear()
    def tearDown(self):
        ai._next.update(self.saved_next)
        for key in ai.ENDPOINTS:ai._cache[key]=self.saved_cache[key]

    def test_arxiv_modern_legacy_version_and_official_urls(self):
        for value,expected in [('1706.03762','1706.03762'),('arXiv:1706.03762v7','1706.03762v7'),
            ('hep-th/9901001','hep-th/9901001'),('https://arxiv.org/pdf/1706.03762v7.pdf','1706.03762v7'),
            ('https://arxiv.org/abs/cs.LG/0601001','cs.LG/0601001')]:
            self.assertEqual(ai.arxiv_id(value),expected)
        for value in ['https://evil.test/abs/1706.03762','https://arxiv.org.evil.test/abs/1706.03762',
            'https://arxiv.org:444/abs/1706.03762','https://arxiv.org/abs/1706.03762?key=secret','../1706.03762',
            '1706.03762v0','1706.03762\nx','https://arxiv.org/list/1706.03762',None]:
            with self.subTest(value=value),self.assertRaises(DownloadError):ai.arxiv_id(value)

    def test_arxiv_native_query_categories_dates_paging_and_sort(self):
        with patch.object(ai,'_request',return_value=FEED) as req:
            out=ai.search_arxiv('ti:"attention"',count=2,start=3,categories='cs.LG,stat.ML',from_year=2017,to_year=2018,sort_by='submittedDate')
        params=req.call_args.args[1]
        self.assertEqual(req.call_args.args[0],'arxiv');self.assertIn('(cat:cs.LG OR cat:stat.ML)',params['search_query'])
        self.assertIn('submittedDate:[201701010000 TO 201812312359]',params['search_query'])
        self.assertEqual(params['sortBy'],'submittedDate');self.assertEqual(out['next_start'],4)
        self.assertEqual(out['records'][0]['arxiv_id'],'1706.03762v7')

    def test_arxiv_authors_metadata_links_and_acceptance_not_inferred(self):
        rows,total=ai._atom(FEED,1);r=rows[0]
        self.assertEqual(total,8);self.assertEqual(r['authors'],['A Researcher']);self.assertEqual(r['doi'],'10.5555/test')
        self.assertTrue(r['abstract_available']);self.assertEqual(r['primary_category'],'cs.CL')
        self.assertEqual(r['pdf_url'],'https://arxiv.org/pdf/1706.03762v7')
        self.assertEqual(r['publication_status'],'preprint_or_author_version');self.assertFalse(r['conference_acceptance_verified'])

    def test_arxiv_abstract_clipping_missing_abstract_and_empty_feed(self):
        long=FEED.replace(b'Useful abstract.',b'a'*14000)
        row=ai._atom(long,10)[0][0];self.assertTrue(row['abstract_truncated']);self.assertEqual(len(row['abstract']),1200)
        row=ai._atom(FEED.replace(b'Useful abstract.',b''),1)[0][0];self.assertFalse(row['abstract_available'])
        empty=b'<feed xmlns="http://www.w3.org/2005/Atom"/>';self.assertEqual(ai._atom(empty,2),([],0))

    def test_arxiv_xml_dtd_entity_error_and_invalid_identifier_rejected(self):
        bads=[b'<!DOCTYPE feed><feed/>',b'<!ENTITY x "private"><feed/>',b'<html/>',b'bad',
            FEED.replace(b'http://arxiv.org/abs/1706.03762v7',b'http://arxiv.org/api/errors#id_list'),
            FEED.replace(b'http://arxiv.org/abs/1706.03762v7',b'https://evil.test/x'),FEED.replace(b'>8<',b'>oops<')]
        for raw in bads:
            with self.subTest(raw=raw[:30]),self.assertRaises(DownloadError):ai._atom(raw,1)

    def test_arxiv_get_exact_version_and_missing_record(self):
        with patch.object(ai,'_request',return_value=FEED):
            self.assertTrue(ai.get_arxiv_record('1706.03762')['ok']);self.assertTrue(ai.get_arxiv_record('1706.03762v7')['ok'])
            self.assertFalse(ai.get_arxiv_record('1706.03762v6')['ok']);self.assertFalse(ai.get_arxiv_record('1706.99999')['ok'])

    def test_arxiv_input_limits_prevent_network(self):
        cases=[{'query':''},{'query':'x\n'},{'query':'x','count':11},{'query':'x','count':True},
            {'query':'x','start':5001},{'query':'x','sort_by':'newest'},{'query':'x','categories':'cs.LG OR all:x'},
            {'query':'x','from_year':2025,'to_year':2020},{'query':'x','from_year':True},{'query':'x','categories':1},
            {'query':'x','categories':','.join(['cs.LG']*7)}]
        with patch.object(ai,'_request') as req:
            for kwargs in cases:
                with self.subTest(kwargs=kwargs),self.assertRaises(DownloadError):ai.search_arxiv(**kwargs)
            req.assert_not_called()

    def test_conference_catalogue_aliases_no_external_requests(self):
        self.assertEqual(len(ai.list_ai_conferences()['conferences']),20)
        self.assertEqual(ai.conference_codes('NIPS,neurips,ICML'),['neurips','icml'])
        self.assertEqual(ai.conference_codes('acm-mm'),['acmmm']);self.assertEqual(len(ai.conference_codes('all')),20)
        for value in ('','icml,unknown','icml,iclr,aaai,uai,colt,kdd,cvpr',None):
            with self.assertRaises(DownloadError):ai.conference_codes(value)

    def test_conference_query_uses_fixed_stream_and_safe_literals(self):
        q=ai.conference_query('"attention is all you need"', ['neurips'],5,0,2017,2017)
        self.assertIn('<https://dblp.org/streams/conf/nips>',q);self.assertIn('a dblp:Inproceedings',q)
        self.assertIn('CONTAINS(LCASE(STR(?title)), LCASE("attention is all you need"))',q)
        self.assertIn('COALESCE(?event_year, ?publication_year)',q);self.assertIn('LIMIT 6 OFFSET 0',q)
        self.assertIn('REGEX(STR(?venue), ?venue_pattern',q)
        self.assertEqual(ai._literal('x" } SERVICE <https://evil.test> {'),json.dumps('x" } SERVICE <https://evil.test> {',ensure_ascii=False))

    def test_conference_normalization_missing_abstract_and_evidence(self):
        with patch.object(ai,'_request',return_value=payload(binding())) as req:
            out=ai.search_ai_conferences('attention','neurips',count=1,from_year=2017,to_year=2017)
        self.assertEqual(req.call_args.args[0],'dblp');r=out['records'][0]
        self.assertEqual(r['doi'],'10.5555/test');self.assertEqual(r['identifier'],'conf/nips/Example17')
        self.assertEqual(r['conference_evidence']['stream'],'https://dblp.org/streams/conf/nips')
        self.assertFalse(r['abstract_available']);self.assertFalse(r['conference_acceptance_verified'])
        self.assertTrue(r['main_venue_filter_applied']);self.assertIn('https://openreview.net/forum?id=example',r['official_links'])

    def test_conference_pagination_uses_extra_row_not_fake_total(self):
        with patch.object(ai,'_request',return_value=payload(binding(),binding(paper='https://dblp.org/rec/conf/nips/Other17'))):
            out=ai.search_ai_conferences('attention','neurips',count=1,offset=2)
        self.assertTrue(out['has_more']);self.assertEqual(out['next_offset'],3);self.assertIsNone(out['total_results'])
        with patch.object(ai,'_request',return_value=payload()):self.assertFalse(ai.search_ai_conferences('', 'iclr')['has_more'])

    def test_conference_rejects_workshops_findings_wrong_stream_or_year(self):
        for changes in [{'venue':'NIPS Workshop'},{'venue':'Findings of ACL'},{'conference':'icml'},
            {'stream':'https://dblp.org/streams/conf/nipsw'},{'paper':'https://evil.test/rec/x'},
            {'year':'2024'},{'year':'not-a-year'}]:
            with patch.object(ai,'_request',return_value=payload(binding(**changes))),self.assertRaises(DownloadError):
                ai.search_ai_conferences('x','neurips',from_year=2017,to_year=2017)

    def test_conference_volume_labels_and_long_author_list(self):
        row=binding(conference='acl',stream='https://dblp.org/streams/conf/acl',paper='https://dblp.org/rec/conf/acl/Example17',
            venue='ACL (1)',authors=' | '.join('Author'+str(i) for i in range(52)))
        with patch.object(ai,'_request',return_value=payload(row)):
            r=ai.search_ai_conferences('x','acl')['records'][0]
        self.assertTrue(r['authors_truncated']);self.assertEqual(len(r['authors']),50)

    def test_conference_invalid_query_year_count_offset_and_json(self):
        with patch.object(ai,'_request') as req:
            for kwargs in [{'count':11},{'offset':1001},{'query':'x\n'},{'from_year':False},
                {'from_year':2025,'to_year':2024},{'query':' '.join(['x']*13)}]:
                with self.subTest(kwargs=kwargs),self.assertRaises(DownloadError):ai.search_ai_conferences(**kwargs)
            req.assert_not_called()
        for raw in (b'<html/>',b'{"results":{}}',b'{"results":{"bindings":null}}'):
            with patch.object(ai,'_request',return_value=raw),self.assertRaises(DownloadError):ai.search_ai_conferences('x')

    def test_metadata_links_reject_credentials_and_unsafe_schemes(self):
        for value in ['javascript:alert(1)','https://user:secret@example.com/a','https://example.com/a?api_key=TEST','https://example.com:123/a','https://example.com/\nx']:
            self.assertEqual(ai._link(value),'')
        self.assertEqual(ai._link('http://openreview.net/forum?id=abc'),'https://openreview.net/forum?id=abc')

    def test_fixed_api_hosts_encoded_query_no_credentials_and_cache(self):
        with patch.dict(os.environ,{'CONTROL_PLANE_API_KEY':'TEST_SECRET','WILEY_TDM_TOKEN':'TEST_WILEY'}),patch.object(ai.DIRECT,'open',return_value=Response(FEED,'application/atom+xml')) as opened:
            first=ai._request('arxiv',{'search_query':'ti:"a&b"'});second=ai._request('arxiv',{'search_query':'ti:"a&b"'})
        self.assertEqual(first,second);self.assertEqual(opened.call_count,1)
        req=opened.call_args.args[0];self.assertTrue(req.full_url.startswith(ai.ENDPOINTS['arxiv']+'?'))
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)['search_query'],['ti:"a&b"'])
        self.assertEqual(set(k.lower() for k in req.headers),{'accept','user-agent'})
        with self.assertRaises(DownloadError):ai._request('https://evil.test',{})
        with self.assertRaises(DownloadError):ai._request('dblp',{'query':'a'*17000})

    def test_http_errors_are_sanitized_and_retry_after_honored(self):
        headers=Message();headers['Retry-After']='120'
        error=urllib.error.HTTPError('https://example.test/?token=TEST_SECRET',429,'TEST_SECRET',headers,None)
        with patch.object(ai.DIRECT,'open',side_effect=error),self.assertRaises(DownloadError) as caught:ai._request('dblp',{'query':'x'})
        self.assertNotIn('TEST_SECRET',str(caught.exception));self.assertGreater(ai._next['dblp'],ai.time.monotonic()+100)
        with patch.object(ai.DIRECT,'open') as req,self.assertRaises(DownloadError):ai._request('dblp',{'query':'y'})
        req.assert_not_called()

    def test_non_200_html_oversized_and_network_response_rejected(self):
        response=Response(b'{}','application/json');response.status=202
        for response in (response,Response(b'<html/>','text/html'),Response(b'x'*(ai.MAX_RESPONSE+1),'application/json')):
            with patch.object(ai.DIRECT,'open',return_value=response),self.assertRaises(DownloadError):ai._request('dblp',{'query':'bad'})
        with patch.object(ai.DIRECT,'open',side_effect=OSError('TEST_SECRET')),self.assertRaises(DownloadError) as caught:ai._request('dblp',{'query':'bad'})
        self.assertNotIn('TEST_SECRET',str(caught.exception))

    def test_arxiv_rate_interval_and_no_redirect(self):
        responses=[Response(FEED,'application/atom+xml'),Response(FEED,'application/atom+xml')]
        with patch.object(ai.time,'monotonic',return_value=10),patch.object(ai.time,'sleep') as pause,patch.object(ai.DIRECT,'open',side_effect=responses):
            ai._request('arxiv',{'id_list':'1706.03762'});ai._request('arxiv',{'id_list':'1706.03762v7'})
        pause.assert_called_once();self.assertAlmostEqual(pause.call_args.args[0],3.1)
        self.assertIsNone(ai.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.test'))

    def test_exact_doi_federation_no_fuzzy_preprint_conference_merge(self):
        arxiv=ai._atom(FEED,1)[0][0]
        dblp={'provider':'dblp','doi':'10.5555/test','identifier':'conf/nips/Example17','title':'Attention'}
        merged=research.federated_search({'arxiv':lambda:{'ok':True,'records':[arxiv]},'dblp':lambda:{'ok':True,'records':[dblp]}})
        self.assertEqual(len(merged['results']),1);self.assertEqual(set(merged['results'][0]['sources']),{'arxiv','dblp'})
        arxiv['doi']='';dblp['doi']=''
        separate=research.merge_results({'arxiv':{'ok':True,'records':[arxiv]},'dblp':{'ok':True,'records':[dblp]}})
        self.assertEqual(len(separate['results']),2)
        partial=research.federated_search({'arxiv':lambda:{'ok':True,'records':[arxiv]},'dblp':lambda:(_ for _ in ()).throw(DownloadError('bad'))})
        self.assertTrue(partial['partial_failure']);self.assertTrue(partial['ok'])

class MCPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:import server
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith('mcp'):raise unittest.SkipTest('MCP SDK is installed on Linux test host')
            raise
        cls.server=server

    def test_ai_only_federation_and_legacy_sources_are_optional(self):
        with patch.object(ai,'search_arxiv',return_value={'ok':True,'records':[{'identifier':'1706.03762','title':'A'}]}) as arxiv,patch.object(ai,'search_ai_conferences',return_value={'ok':True,'records':[]}) as conference:
            out=self.server.search_literature(arxiv_query='ti:attention',conference_query='attention',conferences='neurips',conference_year=2017,count_per_source=2)
        self.assertTrue(out['ok']);arxiv.assert_called_once_with('ti:attention',count=2)
        conference.assert_called_once_with('attention','neurips',count=2,from_year=2017,to_year=2017)

    def test_ai_tools_expose_metadata_and_tool_error(self):
        with patch.object(ai,'get_arxiv_record',return_value={'ok':True,'record':{'arxiv_id':'1706.03762v7'}}):
            self.assertTrue(self.server.get_arxiv_record('1706.03762')['ok'])
        self.assertEqual(len(self.server.list_ai_conferences()['conferences']),20)
        with self.assertRaises(Exception):self.server.search_arxiv('x',count=11)
        with self.assertRaises(Exception):self.server.search_literature(arxiv_query='x',count_per_source=11)
        with self.assertRaises(Exception):self.server.search_literature(conference_query='x',conference_year=True)

if __name__=='__main__':unittest.main()
