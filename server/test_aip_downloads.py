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
from pathlib import Path
from unittest.mock import patch
import aip_downloads as a
import aip_jobs as j
from article_downloads import DownloadError
from native_file_output import read_article_resource
from test_wiley_downloads import pdf_bytes

DOI='10.1063/5.test';PAGE='https://pubs.aip.org/aip/pof/article/35/5/123/1/Test'
PDF='https://pubs.aip.org/aip/pof/article-pdf/doi/'+DOI+'/1/test.pdf'
DELIVERY='https://watermark02.silverchair.com/test.pdf?signature=TEST_PRIVATE_SESSION'
HTML=('<meta name="citation_doi" content="'+DOI+'"><meta name="citation_pdf_url" content="'+PDF+'">').encode()
RECORD={'record':{'doi':DOI,'title':'Test article','resource_url':PAGE}}

class Response(io.BytesIO):
    status=200
    def __init__(self,body,mime='application/pdf',url=DELIVERY,length=None):
        super().__init__(body);self.url=url;self.headers=Message();self.headers['Content-Type']=mime
        if length is not None:self.headers['Content-Length']=str(length)

class AIPTests(unittest.TestCase):
    def test_host_and_role_boundaries(self):
        a.validate_url(PAGE,'landing');a.validate_url(PDF,'pdf');a.validate_url(DELIVERY)
        for url in ['http://pubs.aip.org/aip/pof/article/x','https://evil.test/x.pdf','https://pubs.aip.org.evil.test/x',
                    'https://user:secret@pubs.aip.org/aip/pof/article/x','https://pubs.aip.org:444/x','https://pubs.aip.org/a/%2e%2e/x',
                    'https://pubs.aip.org/x\\x','https://pubs.aip.org/x\n','https://watermark02.silverchair.com/not-a-pdf',PAGE+'#fragment']:
            with self.subTest(url=url),self.assertRaises(DownloadError):a.validate_url(url)
        with self.assertRaises(DownloadError):a.validate_url(DELIVERY,'landing')
        with self.assertRaises(DownloadError):a.validate_url(DELIVERY,'pdf')

    def test_redirect_external_target_never_contacted(self):
        with self.assertRaises(DownloadError):a.Redirect(time.monotonic()+5).redirect_request(None,None,302,'',{},'https://evil.test/a.pdf')

    def test_independent_anonymous_cookie_jars_and_direct_proxy_config(self):
        first=a.Session(time.monotonic()+5);second=a.Session(time.monotonic()+5)
        self.assertIsNot(first.cookies,second.cookies)
        with patch.object(a.urllib.request,'build_opener') as build:a.Session(time.monotonic()+5)
        self.assertEqual(build.call_args.args[0].proxies,{})

    def test_request_has_no_api_key_cookie_or_auth_headers(self):
        session=a.Session(time.monotonic()+5)
        with patch.dict(os.environ,{'CONTROL_PLANE_API_KEY':'TEST_PRIVATE_KEY','WILEY_TDM_TOKEN':'TEST_WILEY'}),patch.object(session.opener,'open',return_value=Response(pdf_bytes(2,DOI))) as req:
            session.fetch(PDF,'application/pdf',20000,lambda n,t:None)
        self.assertEqual(set(k.lower() for k in req.call_args.args[0].headers),{'accept','user-agent'})

    def test_pdf_stream_can_exceed_old_180_second_limit(self):
        raw=pdf_bytes(2,DOI);session=a.Session(1800);progress=[]
        clock=iter(range(0,1000,40))
        with patch.object(a.time,'monotonic',side_effect=lambda:next(clock)),patch.object(session.opener,'open',return_value=Response(raw)):
            result=session.fetch(PDF,'application/pdf',20000,lambda n,t:progress.append((n,t)))
        self.assertEqual(result,raw);self.assertEqual(progress[-1],(len(raw),None))
        self.assertEqual(a.DOWNLOAD_SECONDS,1800)

    def test_pdf_stream_uses_read1_and_reports_received_bytes(self):
        response=Response(pdf_bytes(2,DOI));session=a.Session(time.monotonic()+5);progress=[]
        with patch.object(response,'read1',wraps=response.read1) as read,patch.object(session.opener,'open',return_value=response):
            session.fetch(PDF,'application/pdf',20000,lambda n,t:progress.append((n,t)))
            self.assertTrue(read.called);self.assertLessEqual(read.call_args.args[0],65536)
        self.assertGreater(progress[-1][0],0)

    def test_html_partial_truncation_and_size_limit_rejected(self):
        for response in (Response(b'login','text/html'),Response(b'%PDF-',length=100),Response(b'%PDF-',length=21000000)):
            session=a.Session(time.monotonic()+5)
            with patch.object(session.opener,'open',return_value=response),self.assertRaises(DownloadError):session.fetch(PDF,'application/pdf',20000,lambda n,t:None)
        response=Response(b'xx');response.status=206
        with patch.object(a.time,'monotonic',return_value=1):
            session=a.Session(100)
            with patch.object(session.opener,'open',return_value=response),self.assertRaises(DownloadError):session.fetch(PDF,'application/pdf',20000,lambda n,t:None)

    def test_error_hides_signed_urls_and_cookie_values(self):
        error=urllib.error.HTTPError(DELIVERY,403,'TEST_PRIVATE_SESSION',Message(),io.BytesIO(b'TEST_PRIVATE_SESSION'))
        session=a.Session(time.monotonic()+5)
        with patch.object(session.opener,'open',side_effect=error),self.assertRaises(DownloadError) as caught:session.fetch(PDF,'application/pdf',20000,lambda n,t:None)
        self.assertNotIn('TEST_PRIVATE_SESSION',str(caught.exception))

    def test_pdf_identity_structure_and_single_page_preview(self):
        self.assertEqual(a.verify_pdf(pdf_bytes(2,DOI),DOI,{})['pages'],2)
        for raw in (b'<html>login</html>',pdf_bytes(1,DOI),pdf_bytes(2,'10.1063/other'),pdf_bytes(2,DOI,True),pdf_bytes(2,DOI)[:-20]):
            with self.assertRaises(DownloadError):a.verify_pdf(raw,DOI,{})
        with self.assertRaises(DownloadError):a.verify_pdf(pdf_bytes(2,DOI),DOI,{'citation_firstpage':'1','citation_lastpage':'4'})

    def test_page_doi_mismatch_stops_before_pdf_and_clears_session(self):
        with patch.object(a,'get_record',return_value=RECORD),patch.object(a,'Session') as cls:
            cls.return_value.fetch.return_value=HTML.replace(DOI.encode(),b'10.1063/other')
            with self.assertRaises(DownloadError):a.prepare_aip_pdf(DOI,lambda **kw:None)
            self.assertEqual(cls.return_value.fetch.call_count,1);cls.return_value.close.assert_called_once()

    def test_original_cached_bytes_never_include_signed_url(self):
        raw=pdf_bytes(2,DOI)
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':temp}),patch.object(a,'get_record',return_value=RECORD),patch.object(a,'Session') as cls:
            cls.return_value.fetch.side_effect=[HTML,raw]
            info,token=a.prepare_aip_pdf(DOI,lambda **kw:None)
            self.assertEqual(read_article_resource(token,info['file_name'],'pdf'),raw)
            self.assertEqual(info['sha256'],hashlib.sha256(raw).hexdigest());self.assertNotIn('pdf_url',info)

class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'CAMPUS_AIP_JOB_DIR':self.temp.name,'CAMPUS_DOWNLOAD_DIR':self.temp.name+'/downloads'})
        self.env.start()
    def tearDown(self):self.env.stop();self.temp.cleanup()

    def test_start_returns_immediately_and_scrubs_worker_environment(self):
        with patch.dict(os.environ,{'CONTROL_PLANE_API_KEY':'TEST_SECRET','CREDENTIALS_DIRECTORY':'/test/secrets','HTTPS_PROXY':'http://test'}),patch.object(j.subprocess,'Popen') as launch:
            state=j.start(DOI)
        self.assertEqual(state['status'],'queued');self.assertEqual(state['next_tool'],'get_aip_download')
        self.assertNotIn('CONTROL_PLANE_API_KEY',launch.call_args.kwargs['env'])
        self.assertNotIn('CREDENTIALS_DIRECTORY',launch.call_args.kwargs['env']);self.assertNotIn('HTTPS_PROXY',launch.call_args.kwargs['env'])
        self.assertTrue(launch.call_args.kwargs['start_new_session'])

    def test_active_same_doi_reused_and_two_job_limit(self):
        with patch.object(j.subprocess,'Popen') as launch:
            first=j.start(DOI);again=j.start(DOI);self.assertEqual(first['job_id'],again['job_id'])
            j.start('10.1063/other')
            with self.assertRaises(DownloadError):j.start('10.1063/third')
            self.assertEqual(launch.call_count,2)

    def test_invalid_and_expired_job_ids(self):
        for job_id in ('../secrets.json','x'*48,'f'*47):
            with self.assertRaises(DownloadError):j.status(job_id)
        job_id='f'*48;j.write_state(job_id,{'job_id':job_id,'expires_at':time.time()-1})
        with self.assertRaises(DownloadError):j.status(job_id)

    def test_worker_failure_is_explicit_and_does_not_leak_trace(self):
        with patch.object(j.subprocess,'Popen'):state=j.start(DOI)
        with patch.object(j,'prepare_aip_pdf',side_effect=RuntimeError('TEST_SECRET')):j.worker(state['job_id'])
        status,token=j.status(state['job_id']);self.assertEqual(status['status'],'failed');self.assertIsNone(token)
        self.assertNotIn('TEST_SECRET',json.dumps(status))

    def test_worker_records_progress_and_delivers_resource_token_only_privately(self):
        with patch.object(j.subprocess,'Popen'):state=j.start(DOI)
        def transfer(doi,update):
            update(phase='pdf_download',received_bytes=100,total_bytes=None)
            return {'byte_size':200,'file_name':'test.pdf'},'f'*48
        with patch.object(j,'prepare_aip_pdf',side_effect=transfer):j.worker(state['job_id'])
        status,token=j.status(state['job_id']);self.assertEqual(status['status'],'completed')
        self.assertNotIn('cache_token',status);self.assertEqual(token,'f'*48)
        self.assertEqual(j.job_path(state['job_id']).stat().st_mode&0o777,0o600)

    def test_interrupted_worker_detected_without_reporting_success(self):
        with patch.object(j.subprocess,'Popen'):state=j.start(DOI)
        stored=j.read_state(state['job_id']);stored['created_at']=time.time()-60;j.write_state(state['job_id'],stored)
        status,_=j.status(state['job_id']);self.assertEqual(status['status'],'failed')

    def test_live_worker_lock_keeps_job_running_between_calls(self):
        with patch.object(j.subprocess,'Popen'):state=j.start(DOI)
        stored=j.read_state(state['job_id']);stored['created_at']=time.time()-300;stored['status']='running';j.write_state(state['job_id'],stored)
        with open(j.job_dir()/(state['job_id']+'.lock'),'a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            status,_=j.status(state['job_id']);self.assertEqual(status['status'],'running')

if __name__=='__main__':unittest.main()
