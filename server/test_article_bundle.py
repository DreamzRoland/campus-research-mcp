import base64
import hashlib
import json
import os
import tempfile
import unittest
from html.parser import HTMLParser
from unittest.mock import patch
import article_bundle as bundle
import article_downloads as downloads

DOI='10.1016/test.2026.1'
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==')
XML=b'''<root xmlns:m="http://www.w3.org/1998/Math/MathML"><publicationName>Test Journal</publicationName><article><floats><figure id="fig1"><label>Fig. 1</label><caption><simple-para>Original plot</simple-para></caption><link locator="gr1"/></figure></floats><head><title>A &amp; B <m:math><m:mfrac><m:mi>a</m:mi><m:mi>b</m:mi></m:mfrac></m:math></title><author-group><author><given-name>A.</given-name><surname>Author</surname></author></author-group><abstract><abstract-sec><simple-para>Abstract text</simple-para></abstract-sec></abstract></head><body><sections><section id="sec1"><label>1</label><section-title>Results</section-title><para>See <cross-ref refid="fig1">1</cross-ref>. &lt;script&gt;alert(1)&lt;/script&gt;</para><para>Again <cross-ref refid="fig1">Fig. 1</cross-ref>.</para></section></sections></body><tail><bibliography><bib-reference id="b1"><label>[1]</label><reference>Source</reference><source-text>Duplicate source</source-text></bib-reference></bibliography></tail></article></root>'''

def metadata(kind='IMAGE-HIGH-RES',doi=DOI):
    return bundle.parse_xml(f'''<attachment-metadata-response><coredata><doi>{doi}</doi></coredata><attachment><ref>gr1</ref><filename>gr1.png</filename><mimetype>image/png</mimetype><url>https://api.elsevier.com/content/object/eid/test-gr1.png</url><size>{len(PNG)}</size><width>1</width><height>1</height><type>{kind}</type></attachment></attachment-metadata-response>'''.encode())

class Tags(HTMLParser):
    def __init__(self): super().__init__();self.tags=[]
    def handle_starttag(self,tag,attrs): self.tags.append((tag,dict(attrs)))

class BundleTests(unittest.TestCase):
    def test_never_substitutes_thumbnail_or_wrong_article(self):
        with self.assertRaises(downloads.DownloadError): bundle.select_highres(metadata('IMAGE-THUMBNAIL'),['gr1'],DOI)
        with self.assertRaises(downloads.DownloadError): bundle.select_highres(metadata(doi='10.1016/wrong'),['gr1'],DOI)
    def test_does_not_send_credentials_to_external_hosts(self):
        for url in ['https://example.com/content/object/eid/x','http://api.elsevier.com/content/object/eid/x','https://api.elsevier.com@evil.test/content/object/x','https://api.elsevier.com/content/object/x?apiKey=bad']:
            with patch.object(bundle.DIRECT,'open') as request:
                with self.assertRaises(downloads.DownloadError): bundle.get_api(url,'TEST_ONLY','', 'image/png',100,1e20)
                request.assert_not_called()
    def test_picture_dimensions_and_exact_bytes(self):
        record=bundle.select_highres(metadata(),['gr1'],DOI)[0]
        with patch.object(bundle,'get_api',return_value=(PNG,'image/png')):
            asset=bundle.download_figure(record,'TEST_ONLY','',1e20)
        self.assertEqual(asset['body'],PNG)
        self.assertEqual(asset['sha256'],hashlib.sha256(PNG).hexdigest())
    def test_rejects_error_pages_and_partial_images(self):
        record=bundle.select_highres(metadata(),['gr1'],DOI)[0]
        for body,mime in [(b'<html>denied</html>','text/html'),(PNG[:-1],'image/png')]:
            with patch.object(bundle,'get_api',return_value=(body,mime)):
                with self.assertRaises(downloads.DownloadError): bundle.download_figure(record,'TEST_ONLY','',1e20)
    def test_rejects_wrong_pixel_dimensions(self):
        record=bundle.select_highres(metadata(),['gr1'],DOI)[0]; record['width']=2
        with patch.object(bundle,'get_api',return_value=(PNG,'image/png')):
            with self.assertRaises(downloads.DownloadError): bundle.download_figure(record,'TEST_ONLY','',1e20)
    def test_offline_html_preserves_original_picture_and_math(self):
        record=bundle.select_highres(metadata(),['gr1'],DOI)[0];record['body']=PNG
        output=bundle.render_html(bundle.parse_xml(XML),[record],DOI,True).decode()
        parser=Tags();parser.feed(output)
        images=[a for t,a in parser.tags if t=='img']
        self.assertEqual(len(images),1)
        self.assertEqual(base64.b64decode(images[0]['src'].split(',',1)[1]),PNG)
        self.assertIn('<mfrac>',output)
        self.assertEqual(sum(t=='figure' for t,a in parser.tags),1)
        self.assertFalse(any(t=='script' for t,a in parser.tags))
        self.assertIn('&lt;script&gt;',output)
        self.assertNotIn('Duplicate source',output)
        self.assertTrue(all(a.get('src','').startswith('data:') for t,a in parser.tags if 'src' in a))
    def test_zip_html_points_to_local_originals(self):
        record=bundle.select_highres(metadata(),['gr1'],DOI)[0]; record['body']=PNG
        output=bundle.render_html(bundle.parse_xml(XML),[record],DOI,False).decode()
        self.assertIn('src="images/gr1.png"',output)
        self.assertNotIn('src="https:',output)
    def test_refuses_dtd_entities(self):
        with self.assertRaises(downloads.DownloadError): bundle.parse_xml(b'<!DOCTYPE x [<!ENTITY a "x">]><x>&a;</x>')
    def test_no_completed_package_when_a_picture_fails(self):
        with patch.object(bundle,'prepare_article',return_value=({},'unused')),patch.object(bundle,'article_bytes',return_value=XML),patch.object(bundle,'get_api',return_value=(__import__('xml.etree.ElementTree',fromlist=['tostring']).tostring(metadata()),'application/xml')),patch.object(bundle,'download_figure',side_effect=downloads.DownloadError('picture failed')),patch.object(bundle,'cache_generated_file') as store:
            with self.assertRaisesRegex(downloads.DownloadError,'picture failed'): bundle.prepare_bundle(DOI,'TEST_ONLY')
            store.assert_not_called()
    def test_generated_files_share_cache_expiry_and_integrity(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':directory}):
            metadata_,token=downloads.cache_generated_file(b'<!doctype html><p>offline</p>','article-offline.html','text/html','html',DOI)
            self.assertEqual(downloads.read_chunk(token,0)[1],base64.b64encode(b'<!doctype html><p>offline</p>').decode())
            path=__import__('pathlib').Path(directory)/(token+'.json')
            data=json.loads(path.read_text());data['expires_at']=0;path.write_text(json.dumps(data))
            with self.assertRaises(downloads.DownloadError):downloads.read_chunk(token,0)

if __name__=='__main__': unittest.main()
