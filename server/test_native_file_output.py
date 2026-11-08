import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import native_file_output as native
from article_downloads import CHUNK_SIZE, DownloadError, MAX_FILE

class NativeOutputTests(unittest.TestCase):
    def metadata(self, body):
        return {'byte_size':len(body), 'sha256':hashlib.sha256(body).hexdigest(),
                'file_name':'article-test.pdf','mime_type':'application/pdf'}

    def test_original_bytes_across_chunk_boundary(self):
        body = b'%PDF-' + bytes(range(256)) * 3000
        def chunk(token, offset):
            block = body[offset:offset+CHUNK_SIZE]
            return {'offset':offset,'byte_count':len(block)}, base64.b64encode(block).decode()
        with patch.object(native, 'read_chunk', side_effect=chunk):
            item = native.article_resource(self.metadata(body), 'unused')
        self.assertEqual(base64.b64decode(item.resource.blob), body)
        self.assertEqual(str(item.resource.uri), 'file:///article-test.pdf')
        self.assertEqual(item.resource.mime_type, 'application/pdf')

    def test_rejects_changed_cached_bytes(self):
        body = b'original'
        with patch.object(native, 'read_chunk', return_value=({'offset':0,'byte_count':8},base64.b64encode(b'modified').decode())):
            with self.assertRaises(DownloadError):
                native.article_resource(self.metadata(body), 'unused')

    def test_rejects_truncated_chunk(self):
        body = b'original'
        with patch.object(native, 'read_chunk', return_value=({'offset':0,'byte_count':2},'eHg=')):
            with self.assertRaises(DownloadError):
                native.article_resource(self.metadata(body), 'unused')

    def test_rejects_invalid_sizes_without_reading(self):
        for size in [0, -1, True, MAX_FILE+1]:
            info = self.metadata(b'x'); info['byte_size'] = size
            with patch.object(native, 'read_chunk') as chunk:
                with self.assertRaises(DownloadError): native.article_resource(info, 'unused')
                chunk.assert_not_called()

    def test_link_contains_reference_without_binary(self):
        metadata = self.metadata(b'original'); metadata['format'] = 'pdf'
        item = native.article_link(metadata, 'a'*48)
        self.assertEqual(item.type, 'resource_link')
        self.assertEqual(item.size, 8)
        self.assertFalse(hasattr(item, 'blob'))

    def test_resource_refuses_traversal_and_wrong_filename(self):
        with patch.object(native, 'cache_dir') as directory:
            with self.assertRaises(DownloadError): native.read_article_resource('../secrets', 'x', 'pdf')
            directory.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            token = 'a'*48
            (Path(tmp)/(token+'.json')).write_text(json.dumps({'file_name':'article-test.pdf','format':'pdf'}))
            with patch.object(native, 'cache_dir', return_value=Path(tmp)), patch.object(native, 'read_chunk') as chunk:
                with self.assertRaises(DownloadError): native.read_article_resource(token, 'wrong.pdf', 'pdf')
                with self.assertRaises(DownloadError): native.read_article_resource(token, 'article-test.pdf', 'xml')
                chunk.assert_not_called()

    def test_resource_preserves_expiry_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = 'a'*48
            metadata = self.metadata(b'original'); metadata['format'] = 'pdf'
            (Path(tmp)/(token+'.json')).write_text(json.dumps(metadata))
            with patch.object(native, 'cache_dir', return_value=Path(tmp)), patch.object(native, 'read_chunk', side_effect=DownloadError('expired')):
                with self.assertRaisesRegex(DownloadError, 'expired'):
                    native.read_article_resource(token, 'article-test.pdf', 'pdf')

if __name__ == '__main__': unittest.main()
