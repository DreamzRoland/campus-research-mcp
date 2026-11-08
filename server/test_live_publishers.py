"""Real MCP publisher jobs survive initial session exit, then materialize exact PDF bytes."""
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
    assert not result.is_error,'MCP failed: '+str([x.text for x in result.content if x.type=='text'])[:500]
    return result.structured_content or json.loads(''.join(x.text for x in result.content if x.type=='text'))

async def main():
    cases=[('asme','10.1115/1.4069506',12),('acs','10.1021/acs.energyfuels.1c01764',7),('mdpi','10.3390/su16051940',20)]
    if '--provider' in sys.argv:cases=[c for c in cases if c[0]==sys.argv[sys.argv.index('--provider')+1]]
    export=Path('/var/lib/campus-scopus/publisher-mcp-test');export.mkdir(mode=0o700,exist_ok=True)
    server=os.environ.get('CAMPUS_SERVER_PATH','/opt/campus-scopus/server.py')
    params=StdioServerParameters(command=sys.executable,args=[server],env=dict(os.environ))
    for provider,doi,pages in cases:
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize();start=time.monotonic()
                initial=data(await session.call_tool('download_'+provider+'_pdf',{'doi':doi}));elapsed=time.monotonic()-start
                assert initial['status'] in ('queued','running') and elapsed<5 and initial['provider']==provider
                job_id=initial['job_id'];print(json.dumps({'checkpoint':'job_started','provider':provider,'doi':doi,'job_id':job_id,'initial_call_seconds':round(elapsed,3)}),flush=True)
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize();stop=time.monotonic()+2000
                while True:
                    result=await session.call_tool('get_publisher_download',{'job_id':job_id,'wait_seconds':20});state=data(result)
                    print(json.dumps({'checkpoint':'progress','provider':provider,**{k:state.get(k) for k in ('status','phase','received_bytes','elapsed_seconds','error')}},ensure_ascii=False),flush=True)
                    assert state['status']!='failed',state.get('error')
                    if state['status']=='completed':break
                    assert time.monotonic()<stop,'live test deadline exceeded'
                info=state['file'];links=[x for x in result.content if x.type=='resource_link'];assert len(links)==1
                resource=(await session.read_resource(links[0].uri)).contents[0];raw=base64.b64decode(resource.blob,validate=True)
                assert len(raw)==info['byte_size'] and hashlib.sha256(raw).hexdigest()==info['sha256']
                assert len(PdfReader(io.BytesIO(raw)).pages)==pages==info['verification']['pages']
                p=export/info['file_name'];p.write_bytes(raw);p.chmod(0o600)
                report={**info,'job_id':job_id,'initial_call_seconds':round(elapsed,3),'job_survived_mcp_session_disconnect':True,'mcp_resource_sha256_verified':True,'space_saved_verified':False}
                p=export/('validation-'+provider+'.json');p.write_text(json.dumps(report,ensure_ascii=False,indent=2));p.chmod(0o600)
                print(json.dumps({'checkpoint':'pdf_verified',**report},ensure_ascii=False),flush=True)

if __name__=='__main__':asyncio.run(main())
