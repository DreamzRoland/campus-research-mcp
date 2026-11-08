import fcntl
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
from pathlib import Path
import publisher_downloads as p
import publisher_jobs as j
from article_downloads import DownloadError
from native_file_output import read_article_resource
from test_wiley_downloads import pdf_bytes

DOIS={'asme':'10.1115/test','acs':'10.1021/test','mdpi':'10.3390/test'}
LANDS={'asme':'https://asmedigitalcollection.asme.org/gasturbinespower/article/1/2/3/4/Test',
    'acs':'https://pubs.acs.org/doi/10.1021/test','mdpi':'https://www.mdpi.com/2071-1050/16/5/1940'}
PDFS={'asme':'https://asmedigitalcollection.asme.org/gasturbinespower/article-pdf/1/2/3/4/test.pdf',
    'acs':'https://pubs.acs.org/enfuem/article-pdf/1/2/3/4/test.pdf','mdpi':LANDS['mdpi']+'/pdf'}
SIGNED='https://watermark02.silverchair.com/test.pdf?signature=TEST_PRIVATE_SESSION'

class Response(io.BytesIO):
    status=200
    def __init__(self,body,mime='application/pdf',url=None,length=None):
        super().__init__(body);self.url=url or PDFS['acs'];self.headers=Message();self.headers['Content-Type']=mime
        if length is not None:self.headers['Content-Length']=str(length)

def record(provider):
    return {'record':{'doi':DOIS[provider],'title':'Test','resource_url':LANDS[provider],
        'fulltext_links':[{'url':PDFS[provider],'content_type':'unspecified'}],'page_range':''}}

def html(provider):return ('<meta name="citation_doi" content="'+DOIS[provider]+'"><meta name="citation_pdf_url" content="'+PDFS[provider]+'">').encode()

class DownloadTests(unittest.TestCase):
    def test_provider_doi_and_host_boundaries(self):
        for provider in DOIS:
            p.validate_request(provider,DOIS[provider]);p.validate_url(provider,LANDS[provider],'landing');p.validate_url(provider,PDFS[provider],'pdf')
            for target in ('http://pubs.acs.org/x','https://evil.test/x','https://pubs.acs.org.evil.test/x',
                'https://user:secret@pubs.acs.org/doi/x','https://pubs.acs.org:444/doi/x',PDFS[provider]+'#x',
                PDFS[provider]+'?token=TEST',LANDS[provider]+'/../x',LANDS[provider]+'/%2e%2e/x',LANDS[provider]+'\\x',LANDS[provider]+'\n'):
                with self.subTest(provider=provider,target=target),self.assertRaises(DownloadError):p.validate_url(provider,target)
        with self.assertRaises(DownloadError):p.validate_request('sae','10.4271/test')
        with self.assertRaises(DownloadError):p.validate_request('asme',DOIS['acs'])
        p.validate_url('asme',SIGNED)
        for provider,role in [('asme','landing'),('asme','pdf'),('acs','redirect'),('mdpi','redirect')]:
            with self.assertRaises(DownloadError):p.validate_url(provider,SIGNED,role)
        with self.assertRaises(DownloadError):p.validate_url('mdpi',LANDS['mdpi']+'/xml','pdf')

    def test_external_redirect_never_contacted(self):
        with self.assertRaises(DownloadError):p.Redirect('acs',time.monotonic()+5).redirect_request(None,None,302,'',{},'https://evil.test/x.pdf')

    def test_fresh_cookies_direct_network_and_no_credential_headers(self):
        first=p.Session('acs',time.monotonic()+10);second=p.Session('acs',time.monotonic()+10)
        self.assertIsNot(first.cookies,second.cookies)
        with patch.object(p.urllib.request,'build_opener') as build:p.Session('mdpi',time.monotonic()+5)
        self.assertEqual(build.call_args.args[0].proxies,{})
        with patch.dict(os.environ,{'CONTROL_PLANE_API_KEY':'TEST_SECRET','WILEY_TDM_TOKEN':'TEST_SECRET'}),patch.object(first.opener,'open',return_value=Response(pdf_bytes(2,DOIS['acs']))) as req:
            first.fetch(PDFS['acs'],'pdf',20000,lambda n,t:None)
        self.assertEqual(set(k.lower() for k in req.call_args.args[0].headers),{'accept','user-agent'})

    def test_mdpi_plain_pdf_allowed_other_publishers_plain_rejected(self):
        raw=pdf_bytes(2,DOIS['mdpi']);session=p.Session('mdpi',time.monotonic()+10)
        with patch.object(session.opener,'open',return_value=Response(raw,'text/plain',PDFS['mdpi'])):
            self.assertEqual(session.fetch(PDFS['mdpi'],'pdf',20000,lambda n,t:None),raw)
        session=p.Session('acs',time.monotonic()+10)
        with patch.object(session.opener,'open',return_value=Response(raw,'text/plain')),self.assertRaises(DownloadError):session.fetch(PDFS['acs'],'pdf',20000,lambda n,t:None)

    def test_mdpi_text_label_does_not_accept_html_as_pdf(self):
        session=p.Session('mdpi',time.monotonic()+10)
        with patch.object(session.opener,'open',return_value=Response(b'<html>login</html>','text/plain',PDFS['mdpi'])):
            raw=session.fetch(PDFS['mdpi'],'pdf',20000,lambda n,t:None)
        with self.assertRaises(DownloadError):p.verify_pdf(raw,DOIS['mdpi'],{},record('mdpi')['record'])

    def test_transfer_rejects_partial_mime_encoding_length_size_deadline(self):
        responses=[Response(b'html','text/html'),Response(b'%PDF-',length=100),Response(b'%PDF-',length=21000000)]
        partial=Response(b'%PDF-');partial.status=206;responses.append(partial)
        compressed=Response(b'%PDF-');compressed.headers['Content-Encoding']='gzip';responses.append(compressed)
        for response in responses:
            s=p.Session('acs',time.monotonic()+10)
            with patch.object(s.opener,'open',return_value=response),self.assertRaises(DownloadError):s.fetch(PDFS['acs'],'pdf',20000,lambda n,t:None)
        s=p.Session('acs',time.monotonic()-1)
        with self.assertRaises(DownloadError):s.fetch(PDFS['acs'],'pdf',20000,lambda n,t:None)

    def test_read1_progress_and_slow_transfer_beyond_180_seconds(self):
        raw=pdf_bytes(2,DOIS['acs']);s=p.Session('acs',1800);progress=[];response=Response(raw);clock=iter(range(0,1000,40))
        with patch.object(p.time,'monotonic',side_effect=lambda:next(clock)),patch.object(response,'read1',wraps=response.read1) as read,patch.object(s.opener,'open',return_value=response):
            self.assertEqual(s.fetch(PDFS['acs'],'pdf',20000,lambda n,t:progress.append((n,t))),raw)
            self.assertLessEqual(read.call_args.args[0],65536)
        self.assertEqual(progress[-1],(len(raw),None));self.assertEqual(p.DOWNLOAD_SECONDS,1800)

    def test_errors_hide_signed_urls_cookie_and_raw_exception(self):
        for error in [urllib.error.HTTPError(SIGNED,403,'TEST_PRIVATE_SESSION',Message(),None),OSError('TEST_PRIVATE_SESSION')]:
            s=p.Session('asme',time.monotonic()+5)
            with patch.object(s.opener,'open',side_effect=error),self.assertRaises(DownloadError) as caught:s.fetch(PDFS['asme'],'pdf',20000,lambda n,t:None)
            self.assertNotIn('TEST_PRIVATE_SESSION',str(caught.exception))

    def test_pdf_identity_encryption_single_page_structure_and_page_range(self):
        doi=DOIS['acs'];r=record('acs')['record']
        self.assertEqual(p.verify_pdf(pdf_bytes(2,doi),doi,{},r)['pages'],2)
        for raw in [pdf_bytes(1,doi),pdf_bytes(2,'10.1021/other'),pdf_bytes(2,doi,True),pdf_bytes(2,doi)[:-20],b'<html/>']:
            with self.assertRaises(DownloadError):p.verify_pdf(raw,doi,{},r)
        with self.assertRaises(DownloadError):p.verify_pdf(pdf_bytes(2,doi),doi,{},dict(r,page_range='1-3'))
        with self.assertRaises(DownloadError):p.verify_pdf(pdf_bytes(2,doi),doi,{'citation_firstpage':'1','citation_lastpage':'3'},r)

    def test_page_doi_mismatch_stops_and_session_always_cleared(self):
        with patch.object(p,'get_record',return_value=record('acs')),patch.object(p,'Session') as cls:
            cls.return_value.fetch.return_value=html('acs').replace(DOIS['acs'].encode(),b'10.1021/other')
            with self.assertRaises(DownloadError):p.prepare_publisher_pdf('acs',DOIS['acs'],lambda **kw:None)
            self.assertEqual(cls.return_value.fetch.call_count,1);cls.return_value.close.assert_called_once()

    def test_only_mdpi_uses_crossref_registered_fallback(self):
        for provider in ('asme','acs'):
            with patch.object(p,'get_record',return_value=record(provider)),patch.object(p,'Session') as cls:
                cls.return_value.fetch.return_value=b'<html>challenge</html>'
                with self.assertRaises(DownloadError):p.prepare_publisher_pdf(provider,DOIS[provider],lambda **kw:None)
                self.assertEqual(cls.return_value.fetch.call_count,1)
        bad=record('mdpi');bad['record']['fulltext_links']=[{'url':'https://evil.test/x.pdf'}]
        with patch.object(p,'get_record',return_value=bad),patch.object(p,'Session') as cls:
            cls.return_value.fetch.return_value=b'<html/>'
            with self.assertRaises(DownloadError):p.prepare_publisher_pdf('mdpi',DOIS['mdpi'],lambda **kw:None)

    def test_three_providers_cache_original_bytes_and_native_resource(self):
        for provider in DOIS:
            raw=pdf_bytes(2,DOIS[provider])
            with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':temp}),patch.object(p,'get_record',return_value=record(provider)),patch.object(p,'Session') as cls:
                cls.return_value.fetch.side_effect=[b'<html/>' if provider=='mdpi' else html(provider),raw]
                cls.return_value.last_mime='text/plain' if provider=='mdpi' else 'application/pdf'
                info,token=p.prepare_publisher_pdf(provider,DOIS[provider],lambda **kw:None)
                self.assertEqual(read_article_resource(token,info['file_name'],'pdf'),raw)
                self.assertEqual(info['sha256'],hashlib.sha256(raw).hexdigest());self.assertEqual(info['provider'],provider)
                self.assertEqual(info['pdf_entry_source'],'crossref_registered_link' if provider=='mdpi' else 'publisher_article_metadata')
                self.assertNotIn('pdf_url',info);self.assertNotIn('signature=',json.dumps(info));cls.return_value.close.assert_called_once()

class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'CAMPUS_PUBLISHER_JOB_DIR':self.temp.name,'CAMPUS_DOWNLOAD_DIR':self.temp.name+'/downloads'});self.env.start()
    def tearDown(self):self.env.stop();self.temp.cleanup()

    def test_detached_worker_with_credentials_and_proxies_removed(self):
        with patch.dict(os.environ,{'CONTROL_PLANE_API_KEY':'TEST_SECRET','CREDENTIALS_DIRECTORY':'/test/secrets','HTTPS_PROXY':'http://test','CAMPUS_AIP_JOB_DIR':'/test/aip'}),patch.object(j.subprocess,'Popen') as launch:
            state=j.start('acs',DOIS['acs'])
        self.assertEqual(state['next_tool'],'get_publisher_download');self.assertEqual(state['provider'],'acs')
        env=launch.call_args.kwargs['env']
        for name in ('CONTROL_PLANE_API_KEY','CREDENTIALS_DIRECTORY','HTTPS_PROXY','CAMPUS_AIP_JOB_DIR'):self.assertNotIn(name,env)
        self.assertTrue(launch.call_args.kwargs['start_new_session']);self.assertTrue(launch.call_args.kwargs['close_fds'])

    def test_deduplication_and_shared_two_job_limit(self):
        with patch.object(j.subprocess,'Popen') as launch:
            first=j.start('asme',DOIS['asme']);again=j.start('asme',DOIS['asme'].upper());self.assertEqual(first['job_id'],again['job_id'])
            j.start('acs',DOIS['acs'])
            with self.assertRaises(DownloadError):j.start('mdpi',DOIS['mdpi'])
            self.assertEqual(launch.call_count,2)

    def test_invalid_ids_provider_doi_wait_and_expiry(self):
        for bad in ('../secrets.json','x'*48,'f'*47):
            with self.assertRaises(DownloadError):j.status(bad)
        for wait in (-1,21,True):
            with self.assertRaises(DownloadError):j.status('f'*48,wait)
        j.write_state('f'*48,{'job_id':'f'*48,'expires_at':time.time()-1})
        with self.assertRaises(DownloadError):j.status('f'*48)
        with patch.object(j.subprocess,'Popen') as launch:
            with self.assertRaises(DownloadError):j.start('asme',DOIS['acs'])
            launch.assert_not_called()

    def test_interrupted_process_explicit_failure_and_live_flock_persistence(self):
        with patch.object(j.subprocess,'Popen'):state=j.start('asme',DOIS['asme'])
        stored=j.read_state(state['job_id']);stored.update(created_at=time.time()-300,status='running');j.write_state(state['job_id'],stored)
        with open(j.job_dir()/(state['job_id']+'.lock'),'a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);self.assertEqual(j.status(state['job_id'])[0]['status'],'running')
        self.assertEqual(j.status(state['job_id'])[0]['status'],'failed')

    def test_completion_progress_private_token_permissions_and_frozen_elapsed(self):
        with patch.object(j.subprocess,'Popen'):state=j.start('mdpi',DOIS['mdpi'])
        def transfer(provider,doi,update):
            self.assertEqual(provider,'mdpi');update(phase='pdf_download',received_bytes=100,total_bytes=None)
            return {'byte_size':200,'file_name':'test.pdf'},'f'*48
        with patch.object(j,'prepare_publisher_pdf',side_effect=transfer):j.worker(state['job_id'])
        s,token=j.status(state['job_id']);self.assertEqual(s['status'],'completed');self.assertEqual(token,'f'*48);self.assertNotIn('cache_token',s)
        with patch.object(j.time,'time',return_value=time.time()+100):later,_=j.status(state['job_id'])
        self.assertEqual(s['elapsed_seconds'],later['elapsed_seconds']);self.assertEqual(j.job_path(state['job_id']).stat().st_mode&0o777,0o600)
        self.assertEqual(j.job_dir().stat().st_mode&0o777,0o700)

    def test_worker_failure_and_launch_failure_are_sanitized(self):
        with patch.object(j.subprocess,'Popen'):state=j.start('acs',DOIS['acs'])
        with patch.object(j,'prepare_publisher_pdf',side_effect=RuntimeError('TEST_SECRET')):j.worker(state['job_id'])
        s,token=j.status(state['job_id']);self.assertEqual(s['status'],'failed');self.assertIsNone(token);self.assertNotIn('TEST_SECRET',json.dumps(s))
        with patch.object(j.subprocess,'Popen',side_effect=OSError('TEST_SECRET')),self.assertRaises(DownloadError):j.start('mdpi',DOIS['mdpi'])

if __name__=='__main__':unittest.main()
