"""Verify one-page previews are rejected while structured XML body remains available."""
import asyncio,base64,hashlib,json,os,sys
import xml.etree.ElementTree as ET
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params=StdioServerParameters(command=sys.executable,args=['/opt/campus-scopus/server.py'],env=dict(os.environ))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            for doi in ['10.1016/j.combustflame.2026.115285','10.1016/j.combustflame.2026.115373']:
                pdf=await session.call_tool('download_article',{'doi':doi,'format':'pdf'})
                assert pdf.is_error and '只有 1 页' in str(pdf.content), 'Preview was not rejected'
            result=await session.call_tool('download_article',{'doi':'10.1016/j.combustflame.2026.115285','format':'xml'})
            assert not result.is_error
            info=result.structured_content['article'];token=result.meta['download_id'];blocks=[]
            for offset in range(0,info['byte_size'],info['chunk_size']):
                chunk=await session.call_tool('read_article_chunk',{'download_id':token,'offset':offset})
                assert not chunk.is_error
                blocks.append(base64.b64decode(chunk.meta['data_base64']))
            body=b''.join(blocks)
            assert len(body)==info['byte_size'] and hashlib.sha256(body).hexdigest()==info['sha256']
            root=ET.fromstring(body)
            count=lambda name:sum(e.tag.rsplit('}',1)[-1]==name for e in root.iter())
            assert count('body') and count('section')>1 and count('bib-reference')>1
            output=Path('/var/lib/campus-scopus/article-115285-fulltext.xml')
            output.write_bytes(body);output.chmod(0o600)
            print(json.dumps({'one_page_pdfs_rejected':2,'xml_transfer_integrity':True,
                              'xml_sections':count('section'),'xml_references':count('bib-reference'),
                              'xml_bytes':len(body),'xml_sha256':info['sha256']}))
asyncio.run(main())
