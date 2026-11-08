import hashlib,io,json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import article_downloads as d
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

class Response(io.BytesIO):
    def __init__(self, body, mime='application/pdf', length=None):
        super().__init__(body)
        self.headers={'Content-Type':mime,'Content-Length':str(len(body) if length is None else length)}

class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':self.tmp.name})
        self.env.start()
    def tearDown(self):
        self.env.stop();self.tmp.cleanup()
    def download(self,body,**kwargs):
        with patch.object(d.DIRECT,'open',return_value=Response(body,**kwargs)):
            return d.prepare_article('10.1016/test.2026.1','pdf','DUMMY_TEST_KEY')
    def pdf(self, payload=b'', pages=2):
        writer=PdfWriter()
        for _ in range(pages): writer.add_blank_page(width=612,height=792)
        if payload:
            stream=DecodedStreamObject();stream.set_data(b'%'+payload+b'\n')
            writer.pages[0][NameObject('/Contents')]=writer._add_object(stream)
        buffer=io.BytesIO();writer.write(buffer)
        return buffer.getvalue()
    def test_transfer_integrity_and_private_permissions(self):
        body=self.pdf(b'x'*(d.CHUNK_SIZE+700))
        metadata,token=self.download(body)
        chunks=[]
        for offset in range(0,len(body),d.CHUNK_SIZE):
            info,encoded=d.read_chunk(token,offset)
            chunks.append(d.base64.b64decode(encoded))
            self.assertEqual(info['offset'],offset)
        self.assertEqual(b''.join(chunks),body)
        self.assertEqual(metadata['sha256'],hashlib.sha256(body).hexdigest())
        self.assertEqual((Path(self.tmp.name)/(token+'.bin')).stat().st_mode&0o777,0o600)
        self.assertEqual(Path(self.tmp.name).stat().st_mode&0o777,0o700)
    def test_rejects_html_disguised_as_pdf(self):
        with self.assertRaises(d.DownloadError):self.download(b'<html>login required</html>')
        self.assertEqual(list(Path(self.tmp.name).iterdir()),[])
    def test_rejects_wrong_mime(self):
        with self.assertRaises(d.DownloadError):self.download(b'<html>error</html>',mime='text/html')
    def test_rejects_oversized_response(self):
        with self.assertRaises(d.DownloadError):self.download(b'%PDF-1.7',length=d.MAX_FILE+1)
    def test_rejects_partial_response(self):
        with self.assertRaises(d.DownloadError):self.download(b'%PDF-1.7',length=100)
    def test_invalid_id_and_offsets(self):
        for token in ['../../etc/passwd','a'*47,'g'*48]:
            with self.assertRaises(d.DownloadError):d.read_chunk(token,0)
        metadata,token=self.download(self.pdf())
        for offset in [-1,1,True,2*d.CHUNK_SIZE]:
            with self.assertRaises(d.DownloadError):d.read_chunk(token,offset)
    def test_expired_file(self):
        metadata,token=self.download(self.pdf())
        metadata['expires_at']=int(time.time())-1
        (Path(self.tmp.name)/(token+'.json')).write_text(json.dumps(metadata))
        with self.assertRaises(d.DownloadError):d.read_chunk(token,0)
    def test_doi_format_and_no_key(self):
        for doi in ['https://evil.example/test','10.1/x','10.1016/test\r\nHeader: x']:
            with self.assertRaises(d.DownloadError):d.prepare_article(doi,'pdf','dummy')
        with self.assertRaises(d.DownloadError):d.prepare_article('10.1016/test','html','dummy')
        with self.assertRaises(d.DownloadError):d.prepare_article('10.1016/test','pdf','')
    def test_xml_root_validation(self):
        valid=b'<?xml version="1.0"?><full-text-retrieval-response><originalText>test</originalText></full-text-retrieval-response>'
        with patch.object(d.DIRECT,'open',return_value=Response(valid,mime='application/xml')):
            info,token=d.prepare_article('10.1016/test','xml','dummy')
            self.assertEqual(info['format'],'xml')
        with patch.object(d.DIRECT,'open',return_value=Response(b'<html>login</html>',mime='application/xml')):
            with self.assertRaises(d.DownloadError):d.prepare_article('10.1016/test','xml','dummy')
    def test_space_receipt_matches_file_identity(self):
        receipt_path=Path(self.tmp.name)/'receipts.json'
        info={'sha256':'a'*64,'byte_size':123,'doi':'10.1016/test'}
        receipt={**info,'verified':True,'page_url':'https://chatgpt.com/space/page_test'}
        receipt_path.write_text(json.dumps({info['sha256']:receipt}))
        with patch.dict(os.environ,{'CAMPUS_SPACE_RECEIPTS':str(receipt_path)}):
            self.assertEqual(d.confirmed_space_receipt(info),receipt)
            self.assertIsNone(d.confirmed_space_receipt({**info,'byte_size':124}))
            self.assertIsNone(d.confirmed_space_receipt({**info,'doi':'10.1016/other'}))

    def test_reuse_verified_bytes_but_reject_corrupt_or_expired_cache(self):
        body=self.pdf(b'original verified file')
        info,token=self.download(body)
        receipt={**{k:info[k] for k in ['sha256','byte_size','doi']},
                 'verified':True,'page_url':'https://chatgpt.com/space/page_test'}
        receipt_path=Path(self.tmp.name)/'receipts.json'
        receipt_path.write_text(json.dumps({info['sha256']:receipt}))
        with patch.dict(os.environ,{'CAMPUS_SPACE_RECEIPTS':str(receipt_path)}):
            with patch.object(d.DIRECT,'open',side_effect=AssertionError('must reuse exact bytes')):
                reused,reused_token=d.prepare_article(info['doi'],'pdf','dummy')
                self.assertEqual(reused_token,token)
                self.assertEqual(reused['space_receipt'],receipt)
            (Path(self.tmp.name)/(token+'.bin')).write_bytes(body.replace(b'original',b'modified'))
            fresh,fresh_token=self.download(self.pdf(b'new upstream bytes'))
            self.assertNotEqual(fresh_token,token)
            self.assertNotIn('space_receipt',fresh)
            (Path(self.tmp.name)/(token+'.bin')).write_bytes(body)
            info['expires_at']=int(time.time())-1
            (Path(self.tmp.name)/(token+'.json')).write_text(json.dumps(info))
            fresh,_=self.download(self.pdf(b'another upstream file'))
            self.assertNotIn('space_receipt',fresh)

    def test_single_page_preview_and_old_cache_are_not_delivered(self):
        with self.assertRaisesRegex(d.DownloadError,'只有 1 页'):
            self.download(self.pdf(pages=1))
        self.assertEqual(list(Path(self.tmp.name).iterdir()),[])
        metadata,token=self.download(self.pdf())
        self.assertEqual(metadata['pdf_page_count'],2)
        self.assertFalse(metadata['fulltext_verified'])
        (Path(self.tmp.name)/(token+'.bin')).write_bytes(self.pdf(pages=1))
        with self.assertRaisesRegex(d.DownloadError,'只有 1 页'):
            d.read_chunk(token,0)

if __name__=='__main__':unittest.main()
