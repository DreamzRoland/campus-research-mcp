import io
import json
import os
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from email.message import Message
from unittest.mock import patch
import research_sources as r
from article_downloads import DownloadError
from native_file_output import read_article_resource

DOI='10.1038/test-2026'
KEY='NOT_A_REAL_KEY_123'
ARTICLE=('<article><front><article-meta><article-id pub-id-type="doi">'+DOI+'</article-id></article-meta></front><body><sec><title>Results</title><p>'+('Article body. '*30)+'</p></sec></body></article>').encode()

class Response(io.BytesIO):
    status=200
    def __init__(self,body,mime='application/json'):
        super().__init__(body);self.headers=Message();self.headers['Content-Type']=mime

class ResearchTests(unittest.TestCase):
    def test_springer_response_cannot_echo_credentials(self):
        raw={'apiKey':KEY,'nextPage':'/x?api_key='+KEY,'records':[{'title':'A '+KEY,'url':[{'value':'https://example.com/?api_key='+KEY}]}]}
        with patch.object(r.DIRECT,'open',return_value=Response(json.dumps(raw).encode())) as opened:
            result=r.fetch('springer','/meta/v2/json',{'q':'test'},KEY)
        self.assertNotIn(KEY,json.dumps(result));self.assertNotIn('nextPage',result['data'])
        request=opened.call_args[0][0]
        self.assertTrue(request.full_url.startswith('https://api.springernature.com/meta/v2/json?'))
        self.assertNotIn('X-apikey',request.headers)

    def test_untrusted_target_rejected_before_network(self):
        with patch.object(r.DIRECT,'open') as opened:
            with self.assertRaises(DownloadError):r.fetch('springer','https://evil.test/',{},KEY)
            with self.assertRaises(DownloadError):r.fetch('wos','/apis/wos-starter/v1/documents/../../',{},KEY)
            opened.assert_not_called()

    def test_errors_do_not_include_urls_or_bodies(self):
        error=urllib.error.HTTPError('https://api.test/?api_key='+KEY,401,KEY,{},io.BytesIO(KEY.encode()))
        with patch.object(r.DIRECT,'open',side_effect=error):result=r.fetch('springer','/meta/v2/json',{},KEY)
        self.assertEqual(result['error'],'invalid_api_key');self.assertNotIn(KEY,json.dumps(result))

    def test_wos_header_and_url_encoding(self):
        with patch.object(r.DIRECT,'open',return_value=Response(b'{"uid":"WOS:123","title":"Test"}')) as opened:
            result=r.get_wos_record('WOS:123',KEY)
        request=opened.call_args[0][0]
        self.assertEqual(request.headers['X-apikey'],KEY)
        self.assertNotIn(KEY,request.full_url);self.assertTrue(result['ok'])
        self.assertFalse(result['record']['abstract_available'])

    def test_missing_key_never_makes_request(self):
        with patch.object(r.DIRECT,'open') as opened:
            result=r.search_wos('TS=(flame)','')
            self.assertEqual(result['error'],'missing_api_key');opened.assert_not_called()

    def test_query_limits_prevent_request(self):
        with patch.object(r.DIRECT,'open') as opened:
            for query,count,start in [('x',11,1),('x',True,1),('x',1,0),('x\n',1,1)]:
                with self.assertRaises(DownloadError):r.search_springer(query,KEY,count=count,start=start)
            opened.assert_not_called()

    def test_exact_springer_doi_identity(self):
        response={'ok':True,'records':[{'doi':'10.1038/wrong'}]}
        with patch.object(r,'search_springer',return_value=response):
            self.assertFalse(r.get_springer_record(DOI,KEY)['ok'])

    def test_exact_wos_uid_identity(self):
        with patch.object(r,'fetch',return_value={'ok':True,'data':{'uid':'WOS:456'}}):
            self.assertFalse(r.get_wos_record('WOS:123',KEY)['ok'])

    def test_native_jats_excludes_key_bearing_api_envelope(self):
        raw=b'<response><apiKey>'+KEY.encode()+b'</apiKey><records>'+ARTICLE+b'</records></response>'
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':directory}),patch.object(r,'fetch',return_value={'ok':True,'body':raw}):
            info,token=r.prepare_springer_xml(DOI,KEY)
            output=read_article_resource(token,info['file_name'],'xml')
            self.assertEqual(ET.fromstring(output).tag,'article')
            self.assertNotIn(KEY.encode(),output);self.assertTrue(info['api_envelope_removed'])

    def test_standalone_jats_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':directory}),patch.object(r,'fetch',return_value={'ok':True,'body':ARTICLE}):
            info,token=r.prepare_springer_xml(DOI,KEY)
            self.assertEqual(read_article_resource(token,info['file_name'],'xml'),ARTICLE)

    def test_nonfulltext_or_wrong_article_never_cached(self):
        for body in [ARTICLE.replace(DOI.encode(),b'10.1038/wrong'),ARTICLE.replace(b'<body>',b'<other>').replace(b'</body>',b'</other>'),b'<!DOCTYPE a [<!ENTITY x "x">]><a/>']:
            with patch.object(r,'fetch',return_value={'ok':True,'body':body}),patch.object(r,'cache_verified_file') as cache:
                with self.assertRaises(DownloadError):r.prepare_springer_xml(DOI,KEY)
                cache.assert_not_called()

    def test_official_external_dtd_declarations_are_never_loaded(self):
        prefix=b'<!DOCTYPE response [<!ENTITY % article SYSTEM "https://example.invalid/article.dtd"> %article;]>'
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':directory}),patch.object(r,'fetch',return_value={'ok':True,'body':prefix+ARTICLE}),patch.object(r.DIRECT,'open') as opened:
            info,token=r.prepare_springer_xml(DOI,KEY)
            self.assertEqual(read_article_resource(token,info['file_name'],'xml'),prefix+ARTICLE)
            opened.assert_not_called()

    def test_nested_book_part_matches_its_own_metadata_only(self):
        chapter=ARTICLE.replace(b'<article>',b'<book-part>').replace(b'</article>',b'</book-part>')
        chapter=chapter.replace(b'<front><article-meta>',b'<book-part-meta>').replace(b'</article-meta></front>',b'</book-part-meta>')
        chapter=chapter.replace(b'<article-id pub-id-type="doi">',b'<book-part-id book-part-id-type="doi">').replace(b'</article-id>',b'</book-part-id>')
        raw=b'<response><book-part><body>'+chapter+b'</body></book-part></response>'
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':directory}),patch.object(r,'fetch',return_value={'ok':True,'body':raw}):
            info,token=r.prepare_springer_xml(DOI,KEY)
            self.assertEqual(info['jats_document_type'],'book-part')
            self.assertIn(DOI.encode(),read_article_resource(token,info['file_name'],'xml'))

    def test_merge_keeps_citation_counts_by_source(self):
        response=r.merge_results({'scopus':{'ok':True,'records':[{'doi':DOI.upper(),'title':'Test','cited_by':'12'}]},'wos':{'ok':True,'records':[{'doi':DOI,'title':'Test','citations':[{'count':9}]}]}})
        self.assertEqual(len(response['results']),1)
        sources=response['results'][0]['sources']
        self.assertEqual(sources['scopus']['cited_by'],'12');self.assertEqual(sources['wos']['citations'][0]['count'],9)

    def test_federation_keeps_success_when_another_source_fails(self):
        result=r.federated_search({'springer':lambda:{'ok':True,'records':[{'doi':DOI,'title':'Test'}]},'wos':lambda:1/0})
        self.assertTrue(result['ok']);self.assertTrue(result['partial_failure']);self.assertEqual(len(result['results']),1)

    def test_no_title_based_merging_without_doi(self):
        result=r.merge_results({'wos':{'ok':True,'records':[{'uid':'WOS:1','title':'Same'}]},'scopus':{'ok':True,'records':[{'eid':'1','title':'Same'}]}})
        self.assertEqual(len(result['results']),2)

if __name__=='__main__':unittest.main()
