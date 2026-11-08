"""Real Wiley MCP downloads; prints only safe metadata, never tokens/redirect URLs."""
import asyncio
import base64
import hashlib
import io
import json
import os
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pypdf import PdfReader

def data(result):
    assert not result.is_error,'MCP tool returned an error'
    return result.structured_content or json.loads(''.join(x.text for x in result.content if x.type=='text'))

async def main():
    export=Path('/var/lib/campus-scopus/wiley-test');export.mkdir(mode=0o700,exist_ok=True)
    evidence=[]
    params=StdioServerParameters(command=sys.executable,args=['/opt/campus-scopus/server.py'],env=dict(os.environ))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            state=data(await session.call_tool('ping',{}));assert state['wiley_tdm_token_configured']
            print(json.dumps({'checkpoint':'wiley_token_presence','configured':True,'version':state['version']}),flush=True)
            search=data(await session.call_tool('search_crossref',{'query':'fluid','issn':'2577-8196','count':1}))
            assert search['ok'] and search['records']
            oa_doi=search['records'][0]['doi']
            print(json.dumps({'checkpoint':'wiley_crossref_search','doi':oa_doi,'title':search['records'][0]['title'],'abstract_available':search['records'][0]['abstract_available']}),flush=True)
            for label,doi in [('oa_engineering_reports',oa_doi),('official_wiley_example','10.1111/1467-923X.12168')]:
                record=data(await session.call_tool('get_crossref_record',{'doi':doi}))['record']
                result=await session.call_tool('download_wiley_pdf',{'doi':doi})
                if result.is_error:
                    item={'label':label,'doi':doi,'ok':False,'error':' '.join(x.text for x in result.content if x.type=='text')[:600]}
                    evidence.append(item);print(json.dumps(item,ensure_ascii=False),flush=True);continue
                info=data(result)['file'];links=[x for x in result.content if x.type=='resource_link'];assert len(links)==1
                resource=(await session.read_resource(links[0].uri)).contents[0]
                raw=base64.b64decode(resource.blob,validate=True);pages=len(PdfReader(io.BytesIO(raw)).pages)
                assert pages==info['verification']['pages']>=2
                assert len(raw)==info['byte_size'] and hashlib.sha256(raw).hexdigest()==info['sha256']
                dest=export/info['file_name'];dest.write_bytes(raw);dest.chmod(0o600)
                item={'ok':True,'label':label,'title':record['title'],**{k:info[k] for k in ('doi','file_name','byte_size','sha256','verification','campus_direct_route','personal_login_used','redirect_count')},'mcp_resource_sha256_verified':True,'abstract_available':record['abstract_available']}
                evidence.append(item);print(json.dumps(item),flush=True)
    path=export/'validation.json';path.write_text(json.dumps(evidence,indent=2,ensure_ascii=False));path.chmod(0o600)
    assert len(evidence)==2 and all(r['ok'] for r in evidence),'Some Wiley downloads failed; see sanitized validation report'

asyncio.run(main())
