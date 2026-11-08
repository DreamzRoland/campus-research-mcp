"""Final fallback order, isolation, challenges, file identity and cleanup."""
import io
import os
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from test_retrieval import Base, paper, pdf_bytes
from retrieval import files, models, scihub, store
from retrieval.transport import SourceError


class SciHubTests(Base):
    def setUp(self):
        super().setUp()
        self.policy = patch.dict(os.environ, {'CAMPUS_SCIHUB_FALLBACK':'1'})
        self.policy.start()
        self.record = store.save_record(paper(identifiers={'doi':'10.1234/example'}), 'work:doi:10.1234/example')

    def tearDown(self):
        self.policy.stop()
        super().tearDown()

    def response(self, body, mime='text/html', declared=None):
        r = io.BytesIO(body)
        r.headers = Message()
        r.headers['Content-Type'] = mime
        r.headers['Content-Length'] = str(len(body) if declared is None else declared)
        return r

    def session(self, response):
        s = scihub.Session(time.monotonic()+20, lambda:False, lambda _:None)
        s.opener = Mock()
        s.opener.open.return_value = response
        return s

    def test_eligibility_is_explicit_doi_articles_only(self):
        self.assertTrue(scihub.eligible(self.record))
        self.assertFalse(scihub.eligible(paper()))
        self.assertFalse(scihub.eligible(paper(type='thesis',identifiers={'doi':'10.1234/example'})))
        with patch.dict(os.environ, {'CAMPUS_SCIHUB_FALLBACK':'0'}):
            self.assertFalse(scihub.eligible(self.record))

    def test_primary_success_never_contacts_fallback(self):
        with patch.object(files, 'download_primary', return_value={'ok':True}), patch.object(scihub, 'download') as backup:
            self.assertTrue(files.download(self.record['record_id'])['ok'])
            backup.assert_not_called()

    def test_fallback_is_last_and_preserves_primary_error(self):
        with patch.object(files, 'download_primary', side_effect=SourceError('original_pdf_unavailable')), patch.object(scihub, 'download', return_value={'ok':True}) as backup:
            self.assertTrue(files.download(self.record['record_id'])['ok'])
            self.assertEqual(backup.call_args.args[-1], 'original_pdf_unavailable')

    def test_disabled_fallback_preserves_failure(self):
        with patch.dict(os.environ, {'CAMPUS_SCIHUB_FALLBACK':'0'}), patch.object(files, 'download_primary', side_effect=SourceError('primary_failed')), patch.object(scihub, 'download') as backup:
            with self.assertRaisesRegex(SourceError, 'primary_failed'):
                files.download(self.record['record_id'])
            backup.assert_not_called()

    def test_cancel_quota_and_safety_errors_never_fallback(self):
        for code in ('cancelled','download_time_budget_exhausted','private_file_cache_full','file_size_limit','unregistered_or_unsafe_fulltext_location'):
            with self.subTest(code=code), patch.object(files,'download_primary',side_effect=SourceError(code)), patch.object(scihub,'download') as backup:
                with self.assertRaises(SourceError):
                    files.download(self.record['record_id'])
                backup.assert_not_called()

    def test_primary_location_terminal_error_stops_routes_and_cleans_quota(self):
        locations = [{'url':'https://arxiv.org/first.pdf'}, {'url':'https://arxiv.org/second.pdf'}]
        for code in ('file_size_limit','unregistered_or_unsafe_fulltext_location'):
            with self.subTest(code=code), patch.object(files,'resolve',return_value={'locations':locations,'publisher_routes':[]}), patch.object(files,'stream_with_retry',side_effect=SourceError(code)) as transfer, patch.object(scihub,'download') as backup:
                with self.assertRaises(SourceError) as error:
                    files.download(self.record['record_id'])
                self.assertEqual(error.exception.code,code)
                self.assertEqual(transfer.call_count,1)
                backup.assert_not_called()
                with store.connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0],0)

    def test_aggregate_terminal_causes_cannot_reenter_fallback(self):
        for cause in files.TERMINAL_DOWNLOAD_ERRORS:
            code = 'original_pdf_unavailable:original_pdf_requires_interactive_access,'+cause
            with self.subTest(code=code), patch.object(files,'download_primary',side_effect=SourceError(code)), patch.object(scihub,'download') as backup:
                with self.assertRaises(SourceError) as error:
                    files.download(self.record['record_id'])
                self.assertEqual(error.exception.code,code)
                backup.assert_not_called()

    def test_publisher_terminal_error_is_not_replaced_or_retried(self):
        resolved={'publisher_routes':['springer'],'locations':[{'url':'https://arxiv.org/second.pdf'}]}
        with patch.object(files,'resolve',return_value=resolved), patch.object(files,'publisher_file',side_effect=SourceError('file_size_limit')), patch.object(files,'stream_with_retry') as transfer, patch.object(scihub,'download') as backup:
            with self.assertRaisesRegex(SourceError,'file_size_limit'):
                files.download(self.record['record_id'])
            transfer.assert_not_called()
            backup.assert_not_called()

    def test_real_springer_url_rejection_cannot_enter_fallback(self):
        import springer_pdf
        resolved={'publisher_routes':['springer'],'locations':[]}
        metadata={'record':{'doi':'10.1234/example','resource_url':'https://unapproved.example/article'}}
        with patch.object(files,'resolve',return_value=resolved), patch.object(springer_pdf,'get_record',return_value=metadata), patch.object(scihub,'download') as backup:
            with self.assertRaises(SourceError) as error:
                files.download(self.record['record_id'])
            self.assertEqual(error.exception.code,'unregistered_or_unsafe_fulltext_location')
            backup.assert_not_called()

    def test_real_springer_length_check_cannot_enter_fallback(self):
        import springer_pdf
        landing='https://link.springer.com/article/10.1234/example'
        response=self.response(b'',declared=files.MAX_NATIVE+1)
        response.status=200;response.url=landing
        session=springer_pdf.PublisherSession(time.monotonic()+30)
        session.opener=Mock();session.opener.open.return_value=response
        resolved={'publisher_routes':['springer'],'locations':[]}
        metadata={'record':{'doi':'10.1234/example','resource_url':landing}}
        with patch.object(files,'resolve',return_value=resolved), patch.object(springer_pdf,'get_record',return_value=metadata), patch.object(springer_pdf,'PublisherSession',return_value=session), patch.object(scihub,'download') as backup:
            with self.assertRaises(SourceError) as error:
                files.download(self.record['record_id'])
            self.assertEqual(error.exception.code,'file_size_limit')
            backup.assert_not_called()

    def test_all_publisher_url_guards_emit_typed_terminal_errors(self):
        import aip_downloads, crossref_cambridge, publisher_downloads, springer_pdf, wiley_downloads
        from article_downloads import DownloadError
        guards=[aip_downloads.validate_url,crossref_cambridge.validate_cambridge_url,springer_pdf.validate_url,wiley_downloads.allowed_url]
        guards += [lambda url, provider=p:publisher_downloads.validate_url(provider,url) for p in ('asme','acs','mdpi')]
        for guard in guards:
            with self.subTest(guard=guard), self.assertRaises(DownloadError) as error:
                guard('https://unapproved.example/article.pdf')
            self.assertEqual(error.exception.code,'unregistered_or_unsafe_fulltext_location')

    def test_legacy_cache_quota_is_terminal_in_unified_download(self):
        import article_downloads
        def full_cache(*args):
            return article_downloads.cache_verified_file(b'%PDF-original','one.pdf','application/pdf','pdf','10.1234/example')
        with patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':self.tmp.name}), patch.object(article_downloads,'_cleanup',return_value=article_downloads.MAX_CACHE), patch.object(files,'resolve',return_value={'publisher_routes':['springer'],'locations':[]}), patch.object(files,'publisher_file',side_effect=full_cache), patch.object(scihub,'download') as backup:
            with self.assertRaises(SourceError) as error:
                files.download(self.record['record_id'])
            self.assertEqual(error.exception.code,'private_file_cache_full')
            backup.assert_not_called()

    def test_untyped_legacy_message_is_not_parsed_as_an_error_code(self):
        from article_downloads import DownloadError
        error=DownloadError('file_size_limit mentioned in unrelated text')
        self.assertIsNone(error.code)
        with patch.object(files,'resolve',return_value={'publisher_routes':['springer'],'locations':[]}), patch.object(files,'publisher_file',side_effect=error), patch.object(scihub,'download',return_value={'ok':True}) as backup:
            self.assertTrue(files.download(self.record['record_id'])['ok'])
            backup.assert_called_once()

    def test_theses_do_not_fallback(self):
        thesis = store.save_record(paper(type='thesis', identifiers={'doi':'10.1234/thesis'}), 'thesis:doi:10.1234/thesis')
        with patch.object(files,'download_primary',side_effect=SourceError('original_pdf_unavailable')), patch.object(scihub,'download') as backup:
            with self.assertRaises(SourceError):
                files.download(thesis['record_id'])
            backup.assert_not_called()

    def test_exact_https_hosts_only_and_private_dns_rejected(self):
        with patch('socket.getaddrinfo',return_value=[(0,0,0,'',('8.8.8.8',443))]):
            self.assertEqual(scihub.checked_url('https://sci-net.xyz/a.pdf'),'https://sci-net.xyz/a.pdf')
        for url in ('http://sci-hub.ru/a','https://sci-hub.ru.evil.test/a','https://sci-net.xyz.evil.test/a','https://127.0.0.1/a','https://sci-hub.ru:444/a','https://sci-hub.ru/../a','https://sci-hub.ru/a?token=private'):
            with self.subTest(url=url), self.assertRaises(SourceError):
                scihub.checked_url(url)
        with patch('socket.getaddrinfo',return_value=[(0,0,0,'',('192.168.1.2',443))]), self.assertRaises(SourceError):
            scihub.checked_url('https://sci-hub.ru/a')

    def test_site_challenge_stops_without_second_request(self):
        session = self.session(self.response('<title>Sci-Hub: проверка на робота</title>'.encode()))
        with patch.object(scihub,'checked_url'), self.assertRaisesRegex(SourceError,'scihub_site_verification_required'):
            session.fetch('https://sci-hub.ru/10.1234/a',Path(self.tmp.name)/'a.part',1000)
        self.assertEqual(session.opener.open.call_count,1)

    def test_cancellation_during_rate_wait_sends_no_request(self):
        cancelled={'value':False}
        session=self.session(None)
        session.cancelled=lambda:cancelled['value']
        def wait(*args):
            cancelled['value']=True
        with patch.object(scihub,'checked_url'), patch.object(store,'reserve_rate',side_effect=wait), self.assertRaisesRegex(SourceError,'cancelled'):
            session.fetch('https://sci-hub.ru/a',Path(self.tmp.name)/'cancelled.part',1000)
        session.opener.open.assert_not_called()

    def test_deadline_exhausted_during_rate_wait_sends_no_request(self):
        session=self.session(None)
        def wait(*args):
            session.deadline=time.monotonic()-1
        with patch.object(scihub,'checked_url'), patch.object(store,'reserve_rate',side_effect=wait), self.assertRaisesRegex(SourceError,'download_time_budget_exhausted'):
            session.fetch('https://sci-hub.ru/a',Path(self.tmp.name)/'expired.part',1000)
        session.opener.open.assert_not_called()

    def test_redirect_wait_cancellation_does_not_send_next_request(self):
        cancelled={'value':False}
        session=self.session(None)
        session.cancelled=lambda:cancelled['value']
        headers=Message();headers['Location']='https://sci-net.xyz/a.pdf'
        session.opener.open.side_effect=urllib.error.HTTPError('https://sci-hub.ru/a',302,'redirect',headers,io.BytesIO())
        slots=[]
        def wait(*args):
            slots.append(True)
            cancelled['value']=len(slots)>1
        with patch.object(scihub,'checked_url'), patch.object(store,'reserve_rate',side_effect=wait), self.assertRaisesRegex(SourceError,'cancelled'):
            session.fetch('https://sci-hub.ru/a',Path(self.tmp.name)/'redirect.part',1000)
        self.assertEqual(session.opener.open.call_count,1)

    def test_article_report_modal_is_not_a_download_challenge(self):
        html = '<title>Sci-Hub. An article</title><object type="application/pdf" data="/file.pdf"></object><script src="/captcha.js"></script><div id="report-modal">captcha</div>'
        self.assertFalse(scihub.challenge(html))
        self.assertTrue(scihub.challenge('<title>Captcha</title><object type="application/pdf" data="/file.pdf"></object>'))
        self.assertTrue(scihub.challenge('<title>Just a moment</title>'))

    def test_captcha_frame_cannot_be_mistaken_for_pdf(self):
        html='<title>Verify</title><iframe src="https://www.google.com/recaptcha/widget"></iframe>'
        p=scihub.PdfLinks(scihub.BASE);p.feed(html)
        self.assertEqual(p.links,[])
        self.assertTrue(scihub.challenge(html))

    def test_original_url_and_literal_download_button_no_javascript_execution(self):
        p=scihub.PdfLinks(scihub.BASE)
        p.feed('''<embed type="application/pdf" original-url="/file.pdf"><button onclick="location.href='/other.pdf'">Download</button><button onclick="eval('hidden()')">No URL</button>''')
        self.assertEqual(p.links,['https://sci-hub.ru/file.pdf','https://sci-hub.ru/other.pdf'])

    def test_anonymous_cookie_jar_is_fresh_and_memory_only(self):
        first=scihub.Session(time.monotonic()+10,lambda:False,lambda _:None)
        second=scihub.Session(time.monotonic()+10,lambda:False,lambda _:None)
        self.assertIsNot(first.cookies,second.cookies)
        self.assertEqual(len(first.cookies),0)
        self.assertEqual(len(second.cookies),0)

    def test_redirect_to_unapproved_host_stops_before_request(self):
        session = self.session(None)
        headers = Message();headers['Location']='https://evil.test/a.pdf'
        session.opener.open.side_effect = urllib.error.HTTPError('https://sci-hub.ru/a',302,'redirect',headers,io.BytesIO())
        with patch('socket.getaddrinfo',return_value=[(0,0,0,'',('8.8.8.8',443))]), self.assertRaisesRegex(SourceError,'unapproved_delivery'):
            session.fetch('https://sci-hub.ru/a',Path(self.tmp.name)/'a.part',1000)
        self.assertEqual(session.opener.open.call_count,1)
        sent = session.opener.open.call_args.args[0]
        self.assertNotIn('Authorization',sent.headers)
        self.assertNotIn('Cookie',sent.headers)

    def test_truncation_and_declared_size_are_refused(self):
        for body, declared, code in ((b'abc',4,'truncated_http_body'),(b'abc',99999,'response_size_limit')):
            session = self.session(self.response(body,'application/pdf',declared))
            with patch.object(scihub,'checked_url'), self.assertRaisesRegex(SourceError,code):
                session.fetch('https://sci-hub.ru/a',Path(self.tmp.name)/'a.part',1000)

    def test_pdf_embed_and_fragment(self):
        p = scihub.PdfLinks('https://sci-hub.ru/10.1234/a')
        p.feed('<embed src="//sci-hub.ru/files/a.pdf#view=FitH"><a href="/files/a.pdf">PDF</a>')
        self.assertEqual(p.links,['https://sci-hub.ru/files/a.pdf'])

    def test_valid_pdf_identity_and_native_hash(self):
        data = pdf_bytes(text='Original thesis Alice Researcher 10.1234/example')
        def fetch(url,path,maximum):
            path.write_bytes(data)
            return {'url':url}
        with patch.object(scihub.Session,'fetch',side_effect=fetch):
            out = scihub.download(self.record,lambda _:None,lambda:False,time.monotonic()+30,'primary_unavailable')
        self.assertEqual(files.read_native(out['file']['file_id'],out['file']['file_name']),data)
        self.assertTrue(out['fallback_used'])
        self.assertFalse(out['space_saved'])
        self.assertEqual(out['file']['verification']['identity_method'],'exact_doi_in_pdf')

    def test_challenge_failure_cleans_reservation(self):
        with patch.object(scihub.Session,'fetch',side_effect=SourceError('scihub_site_verification_required')):
            with self.assertRaises(SourceError):
                scihub.download(self.record,lambda _:None,lambda:False,time.monotonic()+30,'primary_unavailable')
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0],0)
        self.assertEqual(scihub.describe()['status'],'needs_interactive_verification')

    def test_wrong_pdf_cannot_be_delivered(self):
        def fetch(url,path,maximum):
            path.write_bytes(pdf_bytes(text='Different paper entirely'))
            return {'url':url}
        with patch.object(scihub.Session,'fetch',side_effect=fetch), self.assertRaisesRegex(SourceError,'pdf_identity_unverified'):
            scihub.download(self.record,lambda _:None,lambda:False,time.monotonic()+30,'primary_unavailable')
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()
