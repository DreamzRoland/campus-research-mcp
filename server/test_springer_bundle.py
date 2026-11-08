import base64
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from email.message import Message
from html.parser import HTMLParser
from unittest.mock import patch
import springer_bundle as s
from article_downloads import DownloadError
from native_file_output import read_article_resource

DOI='10.1038/test-2026'
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==')
XML=b'''<article xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:m="http://www.w3.org/1998/Math/MathML"><front><article-meta><article-id pub-id-type="doi">10.1038/test-2026</article-id><title-group><article-title>Example &amp; safe</article-title></title-group></article-meta></front><body><sec id="s1"><title>Results</title><p>See <xref rid="Fig1">Fig. 1</xref>. &lt;script&gt;example&lt;/script&gt;<inline-formula><alternatives><tex-math>x</tex-math><m:math><m:mi>x</m:mi></m:math><inline-graphic xlink:href="equation.gif"/></alternatives></inline-formula></p><fig id="Fig1"><label>Figure 1</label><caption><p>Caption</p></caption><graphic specific-use="web" xlink:href="MediaObjects/test_Fig1_HTML.png"/></fig><table-wrap><label>Table 1</label><table><tbody><tr><th colspan="2">Heading</th></tr><tr><td>A</td><td>B</td></tr></tbody></table></table-wrap><ext-link xlink:href="javascript:alert(1)">link</ext-link></sec></body><back><ref-list><ref id="r1"><mixed-citation><name><surname>Smith</surname><given-names>J</given-names></name> Citation</mixed-citation></ref></ref-list></back></article>'''

class Inspect(HTMLParser):
    def __init__(self):super().__init__();self.tags=[]
    def handle_starttag(self,tag,attrs):self.tags.append((tag,dict(attrs)))

class Response(io.BytesIO):
    status=200
    def __init__(self,raw,mime='image/png'):
        super().__init__(raw);self.headers=Message();self.headers['Content-Type']=mime;self.headers['Content-Length']=str(len(raw))

def asset(root):
    _,jobs,mapping=s.figure_jobs(root,DOI)
    return [{**jobs[0],'body':PNG,'mimetype':'image/png','width':1,'height':1,'byte_size':len(PNG),'sha256':hashlib.sha256(PNG).hexdigest()}],mapping

class BundleTests(unittest.TestCase):
    def test_xml_path_cannot_select_external_or_traversal_targets(self):
        for ref in ['https://evil.test/x.png','MediaObjects/../x.png','MediaObjects/x.png?api_key=x','MediaObjects/x.svg']:
            root=ET.fromstring(XML.replace(b'MediaObjects/test_Fig1_HTML.png',ref.encode()))
            with self.assertRaises(DownloadError):s.figure_jobs(root,DOI)

    def test_public_image_request_never_has_api_headers_or_cookies(self):
        _,jobs,_=s.figure_jobs(ET.fromstring(XML),DOI)
        with patch.object(s.MEDIA,'open',return_value=Response(PNG)) as opened:
            item=s.fetch_image(jobs[0],time.monotonic()+30)
        request=opened.call_args[0][0]
        self.assertEqual(set(k.lower() for k in request.headers),{'accept','user-agent'})
        self.assertTrue(request.full_url.startswith('https://media.springernature.com/full/'))
        self.assertEqual(item['body'],PNG)

    def test_fetch_rejects_external_origin_before_request(self):
        with patch.object(s.MEDIA,'open') as opened:
            with self.assertRaises(DownloadError):s.fetch_image({'url':'https://example.com/full/x','filename':'x.png'},time.monotonic()+5)
            opened.assert_not_called()

    def test_html_or_truncated_image_is_never_accepted(self):
        _,jobs,_=s.figure_jobs(ET.fromstring(XML),DOI)
        for data,mime in [(b'<html>denied</html>','text/html'),(PNG[:-5],'image/png')]:
            with patch.object(s.MEDIA,'open',return_value=Response(data,mime)):
                with self.assertRaises(DownloadError):s.fetch_image(jobs[0],time.monotonic()+30)

    def test_math_alternatives_render_once_and_preserve_table(self):
        root=ET.fromstring(XML);assets,mapping=asset(root)
        output=s.render_html(root,assets,mapping,DOI,True).decode();parser=Inspect();parser.feed(output)
        images=[a for tag,a in parser.tags if tag=='img']
        self.assertEqual(len(images),1);self.assertEqual(base64.b64decode(images[0]['src'].split(',',1)[1]),PNG)
        self.assertEqual(sum(tag=='math' for tag,a in parser.tags),1)
        self.assertEqual(sum(tag=='table' for tag,a in parser.tags),1)
        self.assertIn('colspan="2"',output);self.assertIn('Smith J Citation',output)
        self.assertNotIn('javascript:',output);self.assertNotIn('<script>',output)
        self.assertFalse(any(a.get('src','').startswith('http') for tag,a in parser.tags))

    def test_zip_reading_edition_uses_local_paths(self):
        root=ET.fromstring(XML);assets,mapping=asset(root)
        output=s.render_html(root,assets,mapping,DOI,False).decode()
        self.assertIn('src="images/test_Fig1_HTML.png"',output)
        self.assertNotIn('src="data:',output)

    def test_bad_figure_prevents_any_completed_package(self):
        with patch.object(s,'prepare_springer_xml',return_value=({},'unused')),patch.object(s,'article_bytes',return_value=XML),patch.object(s,'fetch_image',side_effect=DownloadError('missing picture')),patch.object(s,'cache_generated_file') as cache:
            with self.assertRaisesRegex(DownloadError,'missing picture'):s.prepare_bundle(DOI,'TEST_ONLY')
            cache.assert_not_called()

    def test_bundle_preserves_xml_and_original_picture_bytes(self):
        root=ET.fromstring(XML);assets,_=asset(root)
        meta={'doi':DOI,'file_name':'springer-test.jats.xml','byte_size':len(XML),'sha256':hashlib.sha256(XML).hexdigest()}
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'CAMPUS_DOWNLOAD_DIR':directory}),patch.object(s,'prepare_springer_xml',return_value=(meta,'unused')),patch.object(s,'article_bytes',return_value=XML),patch.object(s,'fetch_image',return_value=assets[0]):
            files,manifest=s.prepare_bundle(DOI,'TEST_ONLY')
            metadata,token=files[2]
            raw=read_article_resource(token,metadata['file_name'],'zip')
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                self.assertIsNone(archive.testzip());self.assertEqual(archive.read('article.jats.xml'),XML)
                self.assertEqual(archive.read('images/test_Fig1_HTML.png'),PNG)
                self.assertFalse(json.loads(archive.read('manifest.json'))['author_originals_verified'])
            self.assertEqual(manifest['image_count'],1)

    def test_web_alternative_does_not_duplicate_one_figure(self):
        root=ET.fromstring(XML.replace(b'<graphic specific-use="web" xlink:href="MediaObjects/test_Fig1_HTML.png"/>',b'<alternatives><graphic xlink:href="MediaObjects/test.eps"/><graphic specific-use="web" xlink:href="MediaObjects/test_Fig1_HTML.png"/></alternatives>'))
        figures,jobs,mapping=s.figure_jobs(root,DOI)
        self.assertEqual(len(figures),1);self.assertEqual(len(jobs),1)

    def test_chapter_url_uses_chapter_media_namespace(self):
        root=ET.fromstring(XML);root.tag='book-part'
        _,jobs,_=s.figure_jobs(root,'10.1007/test_1')
        self.assertIn('/chp%3A10.1007%2Ftest_1/',jobs[0]['url'])

if __name__=='__main__':unittest.main()
