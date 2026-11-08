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
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
import wiley_downloads as w
from article_downloads import DownloadError
from native_file_output import read_article_resource

DOI='10.1002/test.2026'
KEY='TEST_ONLY_NOT_A_REAL_TOKEN'
LAND='https://onlinelibrary.wiley.com/doi/10.1002/test.2026'
PDF='https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/test.2026?download=true'
RECORD={'record':{'resource_url':LAND,'title':'Test','doi_url':'https://doi.org/'+DOI}}

def pdf_bytes(pages=2,doi=DOI,encrypted=False):
    writer=PdfWriter()
    for n in range(pages):
        page=writer.add_blank_page(width=200,height=200)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(('BT /F1 10 Tf 10 20 Td ('+doi+') Tj ET').encode())
        page[NameObject('/Contents')]=writer._add_object(stream)
    if encrypted:writer.encrypt('fixture-password')
    out=io.BytesIO();writer.write(out);return out.getvalue()

class Response(io.BytesIO):
    status=200
    def __init__(self,raw,mime='application/pdf',url=None,length=None):
        super().__init__(raw);self.url=url or w.API+'10.1002%2Ftest.2026';self.headers=Message()
        self.headers['Content-Type']=mime;self.headers['Content-Length']=str(len(raw) if length is None else length)

def redirect(url=PDF,code=302):
    headers=Message();headers['Location']=url
    return urllib.error.HTTPError(w.API,code,'fixture',headers,None)

class WileyTests(unittest.TestCase):
    def test_url_boundary(self):
        for url in ['http://api.wiley.com/x','https://api.wiley.com.evil.test/x','https://evil.test/x','https://user:password@api.wiley.com/x',
                    'https://api.wiley.com:444/x','https://api.wiley.com/a/%2e%2e/x','https://api.wiley.com/a\\x','https://api.wiley.com/x\n','https://api.wiley.com/x#f']:
            with self.subTest(url=url),self.assertRaises(DownloadError):w.allowed_url(url)
        self.assertEqual(w.allowed_url(PDF),PDF)

    def test_token_only_initial_api_header_never_redirect(self):
        raw=pdf_bytes()
        with patch.object(w,'rate_slot'),patch.object(w.DIRECT,'open',side_effect=[redirect(),Response(raw,url=PDF)]) as opened:
            body,hops=w.fetch_pdf(DOI,KEY,time.monotonic()+30)
        self.assertEqual(body,raw);self.assertEqual(hops,1)
        first,second=[call[0][0] for call in opened.call_args_list]
        self.assertTrue(first.full_url.startswith(w.API));self.assertNotIn(KEY,first.full_url)
        self.assertEqual(first.get_header(w.TOKEN_HEADER.capitalize()),KEY)
        self.assertNotIn(KEY,str(second.headers)+str(second.unredirected_hdrs))
        self.assertEqual(set(k.lower() for k in second.headers),{'accept','user-agent'})

    def test_external_redirect_is_not_contacted(self):
        with patch.object(w,'rate_slot'),patch.object(w.DIRECT,'open',side_effect=redirect('https://evil.test/pdf')) as opened:
            with self.assertRaises(DownloadError):w.fetch_pdf(DOI,KEY,time.monotonic()+30)
            self.assertEqual(opened.call_count,1)

    def test_wiley_alm_redirect_never_receives_token(self):
        target='https://alm.wiley.com/fixture-private-download-path'
        with patch.object(w,'rate_slot'),patch.object(w.DIRECT,'open',side_effect=[redirect(target),Response(pdf_bytes(),url=target)]) as opened:
            w.fetch_pdf(DOI,KEY,time.monotonic()+30)
            second=opened.call_args_list[1].args[0]
            self.assertNotIn(KEY,str(second.headers)+str(second.unredirected_hdrs))

    def test_redirect_containing_token_is_not_contacted_or_exposed(self):
        with patch.object(w,'rate_slot'),patch.object(w.DIRECT,'open',side_effect=redirect(PDF+'&key='+KEY)) as opened:
            with self.assertRaises(DownloadError) as err:w.fetch_pdf(DOI,KEY,time.monotonic()+30)
            self.assertNotIn(KEY,str(err.exception));self.assertEqual(opened.call_count,1)

    def test_missing_credential_fails_before_network(self):
        with patch.object(w,'get_record') as record:
            with self.assertRaises(DownloadError):w.prepare_wiley_pdf(DOI,'')
            record.assert_not_called()

    def test_non_wiley_metadata_fails_before_token_use(self):
        with patch.object(w,'get_record',return_value={'record':{'resource_url':'https://www.cambridge.org/core/x'}}),patch.object(w,'fetch_pdf') as fetch:
            with self.assertRaises(DownloadError):w.prepare_wiley_pdf(DOI,KEY)
            fetch.assert_not_called()

    def test_http_error_sanitized(self):
        for code in (400,401,403,404,500):
            err=urllib.error.HTTPError(w.API+'?token='+KEY,code,KEY,Message(),io.BytesIO(KEY.encode()))
            with patch.object(w,'rate_slot'),patch.object(w.DIRECT,'open',side_effect=err):
                with self.assertRaises(DownloadError) as caught:w.fetch_pdf(DOI,KEY,time.monotonic()+30)
                self.assertIn(str(code),str(caught.exception));self.assertNotIn(KEY,str(caught.exception))

    def test_429_persists_retry_after(self):
        headers=Message();headers['Retry-After']='120'
        err=urllib.error.HTTPError(w.API,429,'limit',headers,None)
        with patch.object(w,'rate_slot') as rate,patch.object(w.DIRECT,'open',side_effect=err):
            with self.assertRaisesRegex(DownloadError,'429'):w.fetch_pdf(DOI,KEY,time.monotonic()+30)
            self.assertEqual(rate.call_args_list[-1].args,(120,))

    def test_rate_state_survives_calls_and_contains_no_token(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CAMPUS_WILEY_RATE_FILE':temp+'/rate.json'}),patch.object(w.time,'time',return_value=1000),patch.object(w.time,'sleep') as sleep:
            w.rate_slot();w.rate_slot();sleep.assert_called_once()
            with open(temp+'/rate.json') as stream:state=json.load(stream)
            self.assertGreaterEqual(state['not_before'],1010)
            self.assertNotIn(KEY,json.dumps(state));w.rate_slot(120)
            with self.assertRaisesRegex(DownloadError,'退避'):w.rate_slot()

    def test_corrupt_rate_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CAMPUS_WILEY_RATE_FILE':temp+'/rate.json'}):
            with open(temp+'/rate.json','w') as stream:stream.write('invalid')
            with self.assertRaises(DownloadError):w.rate_slot()

    def test_pdf_identity_and_pages(self):
        self.assertEqual(w.verify_pdf(pdf_bytes(),DOI)['pages'],2)
        for raw in (pdf_bytes(1),pdf_bytes(2,'10.1002/other'),pdf_bytes(2,encrypted=True),b'<html>login</html>',pdf_bytes()[:-20]):
            with self.assertRaises(DownloadError):w.verify_pdf(raw,DOI)

    def test_response_login_truncation_oversize_and_partial_rejected(self):
        for response in (Response(b'login','text/html'),Response(b'%PDF-',length=100),Response(b'xx',length=21*1024*1024)):
            with self.assertRaises(DownloadError):w.read_pdf_response(response,time.monotonic()+5)
        response=Response(b'%PDF-');response.status=206
        with self.assertRaises(DownloadError):w.read_pdf_response(response,time.monotonic()+5)

    def test_legacy_pdf_requires_both_exact_title_and_page_range(self):
        title='A sufficiently long article title for strict identity checking'
        raw=pdf_bytes(2,doi=title)
        info=w.verify_pdf(raw,DOI,{'title':title,'page_range':'279-280'})
        self.assertFalse(info['doi_in_pdf_text']);self.assertEqual(info['identity_method'],'exact_title_and_page_range')
        for metadata in ({'title':title},{'title':'Different long article title for this test','page_range':'279-280'},
                         {'title':title,'page_range':'279-288'}):
            with self.assertRaises(DownloadError):w.verify_pdf(raw,DOI,metadata)

    def test_doi_match_does_not_override_page_range_mismatch(self):
        with self.assertRaises(DownloadError):w.verify_pdf(pdf_bytes(),DOI,{'page_range':'1-9'})

    def test_unexpected_redirect_cannot_be_accepted(self):
        with patch.object(w,'rate_slot'),patch.object(w.DIRECT,'open',return_value=Response(pdf_bytes(),url=PDF)):
            with self.assertRaisesRegex(DownloadError,'未核验'):w.fetch_pdf(DOI,KEY,time.monotonic()+30)

    def test_original_native_bytes_and_no_signed_url(self):
        raw=pdf_bytes()
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':temp}),patch.object(w,'get_record',return_value=RECORD),patch.object(w,'fetch_pdf',return_value=(raw,1)):
            info,token=w.prepare_wiley_pdf(DOI,KEY)
            self.assertEqual(info['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(read_article_resource(token,info['file_name'],'pdf'),raw)
            self.assertNotIn(KEY,json.dumps(info));self.assertNotIn('pdf_url',info)

    def test_credential_echo_prevents_caching(self):
        with patch.object(w,'get_record',return_value=RECORD),patch.object(w,'fetch_pdf',return_value=(pdf_bytes()+KEY.encode(),0)),patch.object(w,'cache_verified_file') as cache:
            with self.assertRaises(DownloadError):w.prepare_wiley_pdf(DOI,KEY)
            cache.assert_not_called()

if __name__=='__main__':unittest.main()
