import hashlib,json,os,tempfile,time,unittest,urllib.parse
from pathlib import Path
from unittest.mock import patch
import file_delivery as d

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.key=b'test-signing-key-32-bytes-minimum!'
        key_path=self.path/'key';key_path.write_bytes(self.key)
        self.env=patch.dict(os.environ,{'CAMPUS_DELIVERY_HTTPS_BASE':'https://download.example.test',
            'CAMPUS_DELIVERY_KEY_FILE':str(key_path)})
        self.env.start();self.token='a'*48
        self.body=b'<?xml version="1.0"?><article>test body</article>'
        self.info={'format':'xml','file_name':'article.xml','sha256':hashlib.sha256(self.body).hexdigest(),
                   'byte_size':len(self.body),'expires_at':int(time.time())+3600}
        (self.path/(self.token+'.bin')).write_bytes(self.body)
        (self.path/(self.token+'.json')).write_text(json.dumps(self.info))
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def ticket(self):return d.create_link(self.token,self.info)['url'].rsplit('/',1)[1]
    def test_roundtrip_and_expiry(self):
        ticket=self.ticket();info,body=d.resolve_ticket(ticket,self.path,self.key)
        self.assertEqual(body,self.body);self.assertEqual(info,self.info)
        with self.assertRaises(ValueError):d.resolve_ticket(ticket,self.path,self.key,now=time.time()+601)
    def test_tampering_and_different_key(self):
        ticket=self.ticket()
        with self.assertRaises(ValueError):d.resolve_ticket(ticket[:-1]+('a' if ticket[-1]!='a' else 'b'),self.path,self.key)
        with self.assertRaises(ValueError):d.resolve_ticket(ticket,self.path,b'wrong-key')
    def test_cache_corruption_or_expiry(self):
        ticket=self.ticket();(self.path/(self.token+'.bin')).write_bytes(b'x'*len(self.body))
        with self.assertRaises(ValueError):d.resolve_ticket(ticket,self.path,self.key)
    def test_disabled_and_invalid_origin(self):
        with patch.dict(os.environ,{'CAMPUS_DELIVERY_HTTPS_BASE':''}):self.assertIsNone(d.create_link(self.token,self.info))
        with patch.dict(os.environ,{'CAMPUS_DELIVERY_HTTPS_BASE':'http://example.test'}):
            with self.assertRaises(ValueError):d.create_link(self.token,self.info)
    def test_http_listener_transfers_file_without_directory_listing(self):
        import threading,urllib.request,urllib.error
        server=d.ThreadingHTTPServer(('127.0.0.1',0),d.Handler)
        server.cache=self.path;server.signing_key=self.key
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with opener.open(base+'/download/'+self.ticket()) as response:
                self.assertEqual(response.read(),self.body)
                self.assertIn('attachment',response.headers['Content-Disposition'])
                self.assertEqual(response.headers['Cache-Control'],'private, no-store')
            with self.assertRaises(urllib.error.HTTPError):opener.open(base+'/')
            with self.assertRaises(urllib.error.HTTPError):opener.open(base+'/download/invalid')
        finally:server.shutdown();server.server_close();worker.join()

if __name__=='__main__':unittest.main()
