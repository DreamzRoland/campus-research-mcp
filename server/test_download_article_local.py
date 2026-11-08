import hashlib,json,tempfile,unittest
from pathlib import Path
from download_article_local import decode_response,save_verified,ExportError

class LocalExportTests(unittest.TestCase):
    def setUp(self):
        self.body=b'<full-text-retrieval-response><body><section/></body><bib-reference/></full-text-retrieval-response>'
        self.info={'ok':True,'doi':'10.1016/test','format':'xml','file_name':'article.xml',
                   'byte_size':len(self.body),'sha256':hashlib.sha256(self.body).hexdigest()}
    def decode(self,info=None,body=None):
        return decode_response(json.dumps(info or self.info).encode()+b'\n'+(self.body if body is None else body),'10.1016/test','xml')
    def test_integrity_and_structure(self):
        info,body=self.decode();self.assertEqual(body,self.body)
        self.assertEqual(info['xml_sections'],1);self.assertEqual(info['xml_references'],1)
    def test_changed_bytes_not_saved(self):
        with self.assertRaises(ExportError):self.decode(body=b'x'*len(self.body))
        with self.assertRaises(ExportError):self.decode(body=self.body[:-1])
    def test_wrong_identity_and_path(self):
        for updates in [{'doi':'10.1016/other'},{'file_name':'../article.xml'},{'format':'pdf'}]:
            with self.assertRaises(ExportError):self.decode({**self.info,**updates})
    def test_remote_failure(self):
        with self.assertRaisesRegex(ExportError,'只有1页'):
            decode_response(b'{"ok":false,"error":"PDF\\u53ea\\u67091\\u9875"}\n','10.1016/test','pdf')
    def test_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            original=Path(tmp)/'article.xml';original.write_bytes(b'user document')
            output=save_verified(self.info,self.body,tmp)
            self.assertNotEqual(output,original);self.assertEqual(original.read_bytes(),b'user document')
            self.assertEqual(save_verified(self.info,self.body,tmp),output)

if __name__=='__main__':unittest.main()
