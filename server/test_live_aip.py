"""Test real AIP jobs across disconnected MCP sessions, then native PDF transfer."""
import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from pypdf import PdfReader

def data(result):
    assert not result.is_error,'MCP failed: '+str([x.text for x in result.content if x.type=='text'])[:600]
    return result.structured_content or json.loads(''.join(x.text for x in result.content if x.type=='text'))

async def main():
    cases=[('non_oa','10.1063/5.0149813',16),('oa_control','10.1063/5.0144663',10)]
    if '--oa-only' in sys.argv:cases=cases[1:]
    if '--non-oa-only' in sys.argv:cases=cases[:1]
    export=Path('/var/lib/campus-scopus/aip-mcp-test');export.mkdir(mode=0o700,exist_ok=True)
    params=StdioServerParameters(command=sys.executable,args=['/opt/campus-scopus/server.py'],env=dict(os.environ))
    evidence=[]
    for label,doi,pages in cases:
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize();start=time.monotonic()
                initial=data(await session.call_tool('download_aip_pdf',{'doi':doi}))
                initial_seconds=time.monotonic()-start
                assert initial['status'] in ('queued','running') and initial_seconds<5
                job_id=initial['job_id']
                print(json.dumps({'checkpoint':'job_started','doi':doi,'job_id':job_id,'initial_call_seconds':round(initial_seconds,3)}),flush=True)
        # The original MCP session and server process have now closed; the worker must survive.
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize();stop=time.monotonic()+2000
                while True:
                    result=await session.call_tool('get_aip_download',{'job_id':job_id,'wait_seconds':20});state=data(result)
                    print(json.dumps({'checkpoint':'job_progress','doi':doi,**{k:state.get(k) for k in ('status','phase','received_bytes','elapsed_seconds','error')}}),flush=True)
                    assert state['status']!='failed',state.get('error')
                    if state['status']=='completed':break
                    assert time.monotonic()<stop,'Live job test deadline exceeded'
                info=state['file'];links=[x for x in result.content if x.type=='resource_link'];assert len(links)==1
                resource=(await session.read_resource(links[0].uri)).contents[0];raw=base64.b64decode(resource.blob,validate=True)
                assert len(raw)==info['byte_size'] and hashlib.sha256(raw).hexdigest()==info['sha256']
                assert len(PdfReader(io.BytesIO(raw)).pages)==pages==info['verification']['pages']
                path=export/info['file_name'];path.write_bytes(raw);path.chmod(0o600)
                item={'label':label,**info,'initial_call_seconds':round(initial_seconds,3),'job_survived_mcp_session_disconnect':True,
                      'mcp_resource_sha256_verified':True,'space_saved_verified':False}
                evidence.append(item);print(json.dumps({'checkpoint':'pdf_verified',**item},ensure_ascii=False),flush=True)
        report=export/('validation-'+label+'.json');report.write_text(json.dumps(item,ensure_ascii=False,indent=2));report.chmod(0o600)

asyncio.run(main())
