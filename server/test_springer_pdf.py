import http.cookiejar
import io
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from email.message import Message
from unittest.mock import patch
import springer_pdf as s
from article_downloads import DownloadError
from native_file_output import read_article_resource
from test_wiley_downloads import pdf_bytes

DOI='10.1007/test.2026'
PAGE='https://link.springer.com/article/'+DOI
PDF='https://link.springer.com/content/pdf/'+DOI+'.pdf'
RECORD={'record':{'doi':DOI,'title':'Test article','resource_url':PAGE,'page_range':'10-11'}}
HTML=('<meta name="citation_doi" content="'+DOI+'"><meta name="citation_pdf_url" content="'+PDF+'">').encode()

class Response(io.BytesIO):
    status=200
    def __init__(self,body,mime='text/html',url=PAGE,length=None):
        super().__init__(body);self.url=url;self.headers=Message();self.headers['Content-Type']=mime
        self.headers['Content-Length']=str(len(body) if length is None else length)

class SpringerPDFTests(unittest.TestCase):
    def test_official_publisher_and_identity_url_roles(self):
        self.assertEqual(s.validate_url(PDF,'pdf'),PDF)
        s.validate_url('https://idp.springer.com/authorize?client_id=anonymous')
        s.validate_url('https://idp.nature.com/authorize')
        s.validate_url('https://idp.nature.com/transit?code=TEST_ANONYMOUS_SESSION')
        for role in ('landing','pdf'):
            with self.assertRaises(DownloadError):s.validate_url('https://idp.nature.com/authorize',role)
            with self.assertRaises(DownloadError):s.validate_url('https://idp.nature.com/transit',role)

    def test_host_scheme_path_userinfo_and_port_restrictions(self):
        for url in ['http://link.springer.com/article/x','https://evil.test/article/x','https://link.springer.com.evil.test/article/x',
                    'https://user:pass@link.springer.com/article/x','https://link.springer.com:444/article/x','https://link.springer.com/article/%2e%2e/x',
                    'https://link.springer.com/article/\\x','https://link.springer.com/article/x\n','https://link.springer.com/admin','https://idp.nature.com/admin',
                    'https://www.nature.com/articles/x#fragment']:
            with self.subTest(url=url),self.assertRaises(DownloadError):s.validate_url(url)

    def test_redirect_rejects_external_target_and_expired_deadline(self):
        handler=s.PublisherRedirect(time.monotonic()+5)
        with self.assertRaises(DownloadError):handler.redirect_request(None,None,303,'',{},'https://evil.test/login')
        expired=s.PublisherRedirect(time.monotonic()-1)
        with self.assertRaisesRegex(DownloadError,'超时'):expired.redirect_request(None,None,303,'',{},PAGE)

    def test_sessions_have_independent_cookie_jars_and_no_proxy(self):
        a=s.PublisherSession(time.monotonic()+5);b=s.PublisherSession(time.monotonic()+5)
        self.assertIsNot(a.cookies,b.cookies)
        with patch.object(s.urllib.request,'build_opener') as build:
            s.PublisherSession(time.monotonic()+5)
            proxy=build.call_args.args[0]
            self.assertEqual(proxy.proxies,{})
        a.close();b.close()

    def test_cookie_policy_rejects_unrelated_domains(self):
        cookie=http.cookiejar.Cookie(0,'anonymous','TEST_SESSION',None,False,'.evil.test',True,True,'/',True,True,None,True,None,None,{},False)
        self.assertFalse(s.PublisherCookies().set_ok(cookie,urllib.request.Request(PAGE)))

    def test_requests_never_load_api_credentials_or_user_cookie_headers(self):
        session=s.PublisherSession(time.monotonic()+5)
        with patch.dict(os.environ,{'SPRINGER_META_API_KEY':'TEST_NOT_REAL_KEY','HTTPS_PROXY':'http://127.0.0.1:1'}),patch.object(session.opener,'open',return_value=Response(HTML)) as opened:
            session.fetch(PAGE,'text/html',1000)
            req=opened.call_args.args[0]
            self.assertEqual(set(k.lower() for k in req.headers),{'accept','user-agent'})
            self.assertNotIn('TEST_NOT_REAL_KEY',str(req.headers)+req.full_url)
        session.close()

    def test_fetch_rejects_oversize_truncation_and_partial_response(self):
        session=s.PublisherSession(time.monotonic()+5)
        for response in (Response(b'ok',length=10),Response(b'ok',length=1001)):
            with patch.object(session.opener,'open',return_value=response),self.assertRaises(DownloadError):session.fetch(PAGE,'text/html',1000)
        response=Response(b'ok');response.status=206
        with patch.object(session.opener,'open',return_value=response),self.assertRaises(DownloadError):session.fetch(PAGE,'text/html',1000)

    def test_http_error_never_exposes_response_body_or_session_url(self):
        session=s.PublisherSession(time.monotonic()+5)
        error=urllib.error.HTTPError(PAGE+'?code=TEST_PRIVATE_SESSION',403,'TEST_PRIVATE_SESSION',Message(),io.BytesIO(b'TEST_PRIVATE_SESSION'))
        with patch.object(session.opener,'open',side_effect=error),self.assertRaises(DownloadError) as err:session.fetch(PAGE,'text/html',1000)
        self.assertIn('403',str(err.exception));self.assertNotIn('TEST_PRIVATE_SESSION',str(err.exception))

    def test_pdf_doi_and_exact_springer_page_range(self):
        raw=pdf_bytes(2,DOI)
        info=s.verify_pdf(raw,RECORD['record'],{},'link.springer.com')
        self.assertEqual(info['pages'],2);self.assertTrue(info['doi_in_pdf_text'])
        with self.assertRaises(DownloadError):s.verify_pdf(raw,{**RECORD['record'],'page_range':'10-12'},{},'link.springer.com')
        with self.assertRaises(DownloadError):s.verify_pdf(pdf_bytes(3,DOI),RECORD['record'],{},'link.springer.com')

    def test_nature_methods_extra_pages_are_valid_but_missing_pages_fail(self):
        info=s.verify_pdf(pdf_bytes(3,DOI),RECORD['record'],{},'www.nature.com')
        self.assertTrue(info['pdf_includes_extra_pages']);self.assertTrue(info['full_pdf_verified'])
        with self.assertRaises(DownloadError):s.verify_pdf(pdf_bytes(2,DOI),{**RECORD['record'],'page_range':'1-3'},{},'www.nature.com')

    def test_pdf_login_invalid_encrypted_single_page_and_wrong_doi_rejected(self):
        for raw in (b'<html>login</html>',b'%PDF-invalid',pdf_bytes(1,DOI),pdf_bytes(2,DOI,True),pdf_bytes(2,'10.1007/other'),pdf_bytes(2,DOI)[:-20]):
            with self.assertRaises(DownloadError):s.verify_pdf(raw,RECORD['record'],{},'link.springer.com')

    def test_non_publisher_doi_is_rejected_before_publisher_session(self):
        with patch.object(s,'get_record',return_value={'record':{'resource_url':'https://www.cambridge.org/core/x'}}),patch.object(s,'PublisherSession') as session:
            with self.assertRaises(DownloadError):s.prepare_springer_pdf(DOI)
            session.assert_not_called()

    def test_article_identity_mismatch_stops_pdf_and_closes_cookies(self):
        with patch.object(s,'get_record',return_value=RECORD),patch.object(s,'PublisherSession') as cls:
            session=cls.return_value;session.fetch.return_value=(b'<meta name="citation_doi" content="10.1007/other">','text/html',PAGE)
            with self.assertRaises(DownloadError):s.prepare_springer_pdf(DOI)
            self.assertEqual(session.fetch.call_count,1);session.close.assert_called_once()

    def test_external_pdf_link_never_requested(self):
        with patch.object(s,'get_record',return_value=RECORD),patch.object(s,'PublisherSession') as cls:
            session=cls.return_value;session.fetch.return_value=(HTML.replace(PDF.encode(),b'https://evil.test/a.pdf'),'text/html',PAGE)
            with self.assertRaises(DownloadError):s.prepare_springer_pdf(DOI)
            self.assertEqual(session.fetch.call_count,1)

    def test_final_html_or_identity_page_never_cached(self):
        for last in [(b'login','text/html',PDF),(b'login','text/html','https://idp.springer.com/authorize')]:
            with patch.object(s,'get_record',return_value=RECORD),patch.object(s,'PublisherSession') as cls,patch.object(s,'cache_verified_file') as cache:
                cls.return_value.fetch.side_effect=[(HTML,'text/html',PAGE),last]
                with self.assertRaises(DownloadError):s.prepare_springer_pdf(DOI)
                cache.assert_not_called()

    def test_native_resource_preserves_original_bytes_and_hides_session_query(self):
        raw=pdf_bytes(2,DOI)
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':temp}),patch.object(s,'get_record',return_value=RECORD),patch.object(s,'PublisherSession') as cls:
            session=cls.return_value;session.fetch.side_effect=[(HTML,'text/html',PAGE+'?code=TEST_SESSION'),(raw,'application/pdf',PDF+'?code=TEST_SESSION')]
            info,token=s.prepare_springer_pdf(DOI)
            self.assertEqual(read_article_resource(token,info['file_name'],'pdf'),raw)
            self.assertNotIn('TEST_SESSION',str(info));self.assertFalse(info['api_key_used'])
            session.close.assert_called_once()

if __name__=='__main__':unittest.main()
