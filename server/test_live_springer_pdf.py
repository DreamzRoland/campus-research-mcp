"""MCP ResourceLink/resources/read tests for campus subscription and OA PDFs."""
import asyncio
import base64
import hashlib
import io
import json
import os
import sys
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from pypdf import PdfReader

CASES=[('closed','10.1007/s00193-021-01034-6',28),('closed','10.1038/s41586-023-06240-z',6),('oa_control','10.1038/s41598-020-79929-0',14)]
def content(result):
    assert not result.is_error,'MCP download failed: '+str([x.text for x in result.content if x.type=='text'])[:1000]
    return result.structured_content or json.loads(''.join(x.text for x in result.content if x.type=='text'))

async def main():
    export=Path('/var/lib/campus-scopus/springer-mcp-pdf-test');export.mkdir(mode=0o700,exist_ok=True)
    evidence=[]
    params=StdioServerParameters(command=sys.executable,args=['/opt/campus-scopus/server.py'],env=dict(os.environ))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            state=content(await session.call_tool('ping',{}));assert state['version']=='1.7.0' and state['springer_campus_pdf_available']
            print(json.dumps({'check':'ping','version':state['version'],'springer_pdf':True}),flush=True)
            for access,doi,pages in CASES:
                result=await session.call_tool('download_springer_pdf',{'doi':doi});data=content(result);info=data['file']
                assert data['space_saved'] is False and info['api_key_used'] is False and info['personal_login_used'] is False
                links=[x for x in result.content if x.type=='resource_link'];assert len(links)==1
                resource=(await session.read_resource(links[0].uri)).contents[0]
                raw=base64.b64decode(resource.blob,validate=True)
                assert len(raw)==info['byte_size'] and hashlib.sha256(raw).hexdigest()==info['sha256']
                assert len(PdfReader(io.BytesIO(raw)).pages)==pages==info['verification']['pages']
                path=export/info['file_name'];path.write_bytes(raw);path.chmod(0o600)
                item={'access_class':access,**info,'mcp_resource_sha256_verified':True,'space_saved_verified':False}
                evidence.append(item);print(json.dumps(item,ensure_ascii=False),flush=True)
    path=export/'validation.json';path.write_text(json.dumps(evidence,indent=2,ensure_ascii=False));path.chmod(0o600)

asyncio.run(main())
