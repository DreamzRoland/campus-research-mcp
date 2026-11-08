import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch
from pypdf import PdfWriter
import crossref_cambridge as c
from article_downloads import DownloadError
from native_file_output import read_article_resource

DOI='10.1017/test.2026'
LAND='https://www.cambridge.org/core/product/identifier/TEST/type/journal_article'
PDF='https://www.cambridge.org/core/services/aop-cambridge-core/content/view/ABC/test.pdf'

def pdf_bytes(pages=2,encrypted=False):
    writer=PdfWriter()
    for _ in range(pages):writer.add_blank_page(width=100,height=100)
    if encrypted:writer.encrypt('test-password')
    out=io.BytesIO();writer.write(out);return out.getvalue()

class Response(io.BytesIO):
    def __init__(self,raw,mime='application/json',url=LAND,length=None):
        super().__init__(raw);self.url=url;self.headers=Message()
        self.headers['Content-Type']=mime;self.headers['Content-Length']=str(len(raw) if length is None else length)

class DiscoveryTests(unittest.TestCase):
    def setUp(self):c._cache.clear();c._next_request=0

    def test_missing_abstract_is_explicit(self):
        r=c.normalized_record({'DOI':DOI})
        self.assertFalse(r['abstract_available']);self.assertEqual(r['abstract'],'')

    def test_jats_text_truncation_and_original_markup(self):
        raw='<jats:abstract><jats:p>A &amp; B</jats:p><jats:p>second</jats:p></jats:abstract>'
        r=c.normalized_record({'abstract':raw},5,True)
        self.assertEqual(r['abstract'],'A & B');self.assertTrue(r['abstract_truncated'])
        self.assertEqual(r['abstract_markup'],raw)

    def test_exact_doi_identity(self):
        with patch.object(c,'crossref_request',return_value={'DOI':'10.1017/other'}):
            with self.assertRaises(DownloadError):c.get_record(DOI)

    def test_search_filters_and_limits(self):
        with patch.object(c,'crossref_request',return_value={'items':[],'total-results':0}) as req:
            c.search('vortex',2,4,'0022-1120',True,2020,2026,56)
            params=req.call_args[0][1]
            self.assertEqual(params['offset'],4)
            for f in ('issn:0022-1120','has-abstract:true','from-pub-date:2020-01-01','member:56'):self.assertIn(f,params['filter'])
        for kw in ({'count':11},{'offset':-1},{'issn':'bad'},{'from_year':2027,'to_year':2026},{'member_id':True}):
            with self.assertRaises(DownloadError):c.search('vortex',**kw)

    def test_fixed_crossref_host_cache_and_no_auth_headers(self):
        body=json.dumps({'status':'ok','message':{'DOI':DOI}}).encode()
        with patch.object(c.CROSSREF,'open',return_value=Response(body)) as req:
            first=c.crossref_request('/works/10.1017%2Ftest.2026',{})
            first['DOI']='changed'
            self.assertEqual(c.crossref_request('/works/10.1017%2Ftest.2026',{})['DOI'],DOI)
            self.assertEqual(req.call_count,1)
            self.assertEqual(set(k.lower() for k in req.call_args[0][0].headers),{'accept','user-agent'})
        with self.assertRaises(DownloadError):c.crossref_request('//evil.test',{})

    def test_rate_limit_honors_retry_after_without_leaking_url(self):
        headers=Message();headers['Retry-After']='120'
        err=urllib.error.HTTPError('https://api.crossref.org/private',429,'limit',headers,None)
        with patch.object(c.CROSSREF,'open',side_effect=err):
            with self.assertRaisesRegex(DownloadError,'HTTP 429'):c.crossref_request('/works',{})
        self.assertGreater(c._next_request,time.monotonic()+115)
        with self.assertRaisesRegex(DownloadError,'退避'):c.crossref_request('/works',{})

    def test_cambridge_host_path_and_redirect_restrictions(self):
        for url in ['http://www.cambridge.org/core/x','https://evil.test/core/x','https://www.cambridge.org.evil.test/core/x',
                    'https://user:pass@www.cambridge.org/core/x','https://www.cambridge.org:444/core/x',
                    'https://www.cambridge.org/core/%2e%2e/admin','https://www.cambridge.org/core/x?api_key=secret',
                    'https://www.cambridge.org/core/x#fragment','https://www.cambridge.org/core/\\x','https://www.cambridge.org/core/x\n']:
            with self.subTest(url=url),self.assertRaises(DownloadError):c.validate_cambridge_url(url)
        with self.assertRaises(DownloadError):c.CambridgeRedirect().redirect_request(None,None,302,'',{},'https://evil.test/core/x')
        self.assertEqual(c.validate_cambridge_url(PDF,True),PDF)

    def test_cambridge_request_has_no_credentials_or_cookies(self):
        with patch.object(c.CAMBRIDGE,'open',return_value=Response(b'ok','text/html')) as req:
            raw,mime,_=c.cambridge_fetch(LAND,'text/html',100,time.monotonic()+5)
            self.assertEqual(raw,b'ok');self.assertEqual(mime,'text/html')
            self.assertEqual(set(k.lower() for k in req.call_args[0][0].headers),{'accept','user-agent'})

    def test_cambridge_truncated_and_oversized_response_rejected(self):
        for maximum,length in ((100,4),(1,2)):
            with patch.object(c.CAMBRIDGE,'open',return_value=Response(b'ok',length=length)):
                with self.assertRaises(DownloadError):c.cambridge_fetch(LAND,'text/html',maximum,time.monotonic()+5)

    def test_pdf_page_range_checked(self):
        raw=pdf_bytes()
        info=c.verify_pdf(raw,DOI,{'citation_firstpage':'10','citation_lastpage':'11'})
        self.assertEqual(info['pages'],2);self.assertTrue(info['page_range_verified'])
        with self.assertRaises(DownloadError):c.verify_pdf(raw,DOI,{'citation_firstpage':'10','citation_lastpage':'12'})
        with self.assertRaises(DownloadError):c.verify_pdf(raw,DOI,{})

    def test_preview_login_invalid_and_encrypted_pdf_rejected(self):
        for raw in (b'<html>login</html>',b'%PDF-broken',pdf_bytes(1),pdf_bytes(2,True)):
            with self.assertRaises(DownloadError):c.verify_pdf(raw,DOI,{})

    def test_article_doi_mismatch_stops_before_pdf_download(self):
        with patch.object(c,'get_record',return_value={'record':{'resource_url':LAND}}),patch.object(c,'cambridge_fetch',return_value=(b'<meta name="citation_doi" content="10.1017/other">','text/html',LAND)) as fetch:
            with self.assertRaises(DownloadError):c.prepare_cambridge_pdf(DOI)
            self.assertEqual(fetch.call_count,1)

    def test_pdf_native_resource_exact_bytes(self):
        raw=pdf_bytes()
        html=('<meta name="citation_doi" content="'+DOI+'"><meta name="citation_pdf_url" content="'+PDF+'"><meta name="citation_firstpage" content="1"><meta name="citation_lastpage" content="2">').encode()
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':temp}),patch.object(c,'get_record',return_value={'record':{'resource_url':LAND,'title':'Test'}}),patch.object(c,'cambridge_fetch',side_effect=[(html,'text/html',LAND),(raw,'application/pdf',PDF)]):
            info,token=c.prepare_cambridge_pdf(DOI)
            self.assertEqual(info['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(read_article_resource(token,info['file_name'],'pdf'),raw)

    def test_pdf_login_response_never_cached(self):
        html=('<meta name="citation_doi" content="'+DOI+'"><meta name="citation_pdf_url" content="'+PDF+'">').encode()
        with patch.object(c,'get_record',return_value={'record':{'resource_url':LAND,'title':'Test'}}),patch.object(c,'cambridge_fetch',side_effect=[(html,'text/html',LAND),(b'login','text/html',PDF)]),patch.object(c,'cache_verified_file') as cache:
            with self.assertRaises(DownloadError):c.prepare_cambridge_pdf(DOI)
            cache.assert_not_called()

if __name__=='__main__':unittest.main()
