import asyncio,base64,hashlib,io,json,os,sys,zipfile
from pathlib import Path
from html.parser import HTMLParser
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from article_bundle import image_dimensions

class Inspect(HTMLParser):
    def __init__(self):super().__init__();self.images=[];self.figures=0;self.math=0;self.scripts=0
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='img':self.images.append(attrs['src'])
        if tag=='figure':self.figures+=1
        if tag=='math':self.math+=1
        if tag=='script':self.scripts+=1

async def main():
    params=StdioServerParameters(command=sys.executable,args=[os.environ.get('CAMPUS_SERVER_PATH','/opt/campus-scopus/server.py')],env=dict(os.environ))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            result=await session.call_tool('download_article_bundle',{'doi':'10.1016/j.combustflame.2026.115355'})
            if result.is_error: raise RuntimeError(str([getattr(c,'text','') for c in result.content]))
            info=result.structured_content
            assert info['figure_count']==10 and info['all_highres_downloaded'] and info['image_bytes_unchanged']
            assert info['space_saved'] is False
            descriptors={f['file_name']:f for f in info['files']}
            output=Path('/var/lib/campus-scopus/bundle-validation');output.mkdir(mode=0o700,exist_ok=True)
            blobs={}
            for item in result.content:
                if item.type!='resource_link':continue
                resource=(await session.read_resource(item.uri)).contents[0]
                raw=base64.b64decode(resource.blob)
                expected=descriptors[item.name]
                assert len(raw)==expected['byte_size'] and hashlib.sha256(raw).hexdigest()==expected['sha256']
                file=output/item.name;file.write_bytes(raw);file.chmod(0o600)
                blobs[expected['format']]=raw
            assert set(blobs)=={'xml','html','zip'}
            standalone=Inspect();standalone.feed(blobs['html'].decode())
            assert standalone.figures==len(standalone.images)==10 and standalone.math>300 and standalone.scripts==0
            assert all(src.startswith('data:image/') for src in standalone.images)
            with zipfile.ZipFile(io.BytesIO(blobs['zip'])) as archive:
                assert archive.testzip() is None
                assert archive.read('article.xml')==blobs['xml']
                manifest=json.loads(archive.read('manifest.json'))
                index=Inspect();index.feed(archive.read('index.html').decode())
                assert len(index.images)==10 and all(src.startswith('images/') for src in index.images)
                for expected,src in zip(manifest['figures'],standalone.images):
                    original=archive.read('images/'+expected['filename'])
                    assert len(original)==expected['size']
                    assert hashlib.sha256(original).hexdigest()==expected['sha256']
                    assert base64.b64decode(src.split(',',1)[1])==original
                    assert image_dimensions(original,expected['mimetype'])==(expected['width'],expected['height'])
                (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
            summary={'bundle_complete':True,'original_highres_figures':10,'original_bytes_and_dimensions_verified':True,
                     'standalone_has_embedded_originals':True,'native_mathml_count':standalone.math,'zip_integrity':True,
                     'attachments':list(descriptors.values())}
            (output/'validation.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary))

asyncio.run(main())
