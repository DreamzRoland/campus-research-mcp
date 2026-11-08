"""Verify both real Springer article/chapter bundles through MCP resource reads."""
import asyncio,base64,hashlib,io,json,os,sys,zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from html.parser import HTMLParser
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

class Inspect(HTMLParser):
    def __init__(self):super().__init__();self.images=[];self.math=0;self.tables=0;self.scripts=0
    def handle_starttag(self,tag,attrs):
        if tag=='img':self.images.append(dict(attrs)['src'])
        if tag=='math':self.math+=1
        if tag=='table':self.tables+=1
        if tag=='script':self.scripts+=1

async def main():
    output=Path('/var/lib/campus-scopus/springer-bundle-test');output.mkdir(mode=0o700,exist_ok=True)
    params=StdioServerParameters(command=sys.executable,args=['/opt/campus-scopus/server.py'],env=dict(os.environ))
    reports=[]
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            for doi in ('10.1038/s41598-020-79929-0','10.1007/978-3-032-37207-9_95'):
                result=await session.call_tool('download_springer_bundle',{'doi':doi})
                if result.is_error:raise RuntimeError('Springer bundle MCP call failed; no success reported.')
                info=result.structured_content
                assert info['ok'] and info['image_count']==3 and info['all_figures_downloaded']
                expected={f['file_name']:f for f in info['files']};blobs={}
                for item in result.content:
                    if item.type!='resource_link':continue
                    resource=(await session.read_resource(item.uri)).contents[0]
                    raw=base64.b64decode(resource.blob,validate=True);meta=expected[item.name]
                    assert len(raw)==meta['byte_size'] and hashlib.sha256(raw).hexdigest()==meta['sha256']
                    path=output/item.name;path.write_bytes(raw);path.chmod(0o600)
                    blobs[meta['format']]=raw
                assert set(blobs)=={'xml','html','zip'}
                source=ET.fromstring(blobs['xml']);local=lambda e:e.tag.rsplit('}',1)[-1]
                view=Inspect();view.feed(blobs['html'].decode())
                assert len(view.images)==3 and all(src.startswith('data:image/') for src in view.images)
                assert view.scripts==0
                assert view.math==sum(local(e)=='math' for e in source.iter())
                assert view.tables==sum(local(e)=='table' for e in source.iter())
                with zipfile.ZipFile(io.BytesIO(blobs['zip'])) as archive:
                    assert archive.testzip() is None
                    assert archive.read('article.jats.xml')==blobs['xml']
                    manifest=json.loads(archive.read('manifest.json'))
                    index=Inspect();index.feed(archive.read('index.html').decode())
                    assert len(index.images)==3 and all(src.startswith('images/') for src in index.images)
                    for image,src in zip(manifest['images'],view.images):
                        raw=archive.read('images/'+image['filename'])
                        assert len(raw)==image['byte_size'] and hashlib.sha256(raw).hexdigest()==image['sha256']
                        assert base64.b64decode(src.split(',',1)[1],validate=True)==raw
                report={'doi':doi,'images':3,'all_original_image_bytes_verified':True,'mathml':view.math,'tables':view.tables,
                        'zip_integrity':True,'standalone_images_embedded':True,'attachments':info['files']}
                reports.append(report);print(json.dumps(report),flush=True)
    (output/'validation.json').write_text(json.dumps({'all_passed':True,'results':reports},indent=2),encoding='utf-8')

asyncio.run(main())
