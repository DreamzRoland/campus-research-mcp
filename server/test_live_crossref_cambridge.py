"""Real MCP tests, credentials loaded only by systemd. Never prints secrets or file bytes."""
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

DOIS=[('10.1017/jfm.2025.113',35),('10.1017/S0022112095000462',26),('10.1017/jfm.2026.11772',21)]

def data(result):
    assert not result.is_error,'MCP tool returned an error: '+str([x.text for x in result.content if x.type=='text'])[:1500]
    return result.structured_content or json.loads(''.join(x.text for x in result.content if x.type=='text'))

async def main():
    export=Path('/var/lib/campus-scopus/crossref-cambridge-test');export.mkdir(mode=0o700,exist_ok=True)
    params=StdioServerParameters(command=sys.executable,args=['/opt/campus-scopus/server.py'],env=dict(os.environ))
    evidence=[]
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            results=data(await session.call_tool('search_crossref',{'query':'unsteady','issn':'0022-1120','has_abstract':True,'count':2}))
            assert results['ok'] and len(results['records'])==2 and all(r['abstract_available'] and '0022-1120' in r['issn'] for r in results['records'])
            print(json.dumps({'checkpoint':'crossref_search','ok':True,'doi':[r['doi'] for r in results['records']]}),flush=True)
            combined=data(await session.call_tool('search_literature',{'crossref_query':'vortex','crossref_issn':'0022-1120','scopus_query':'TITLE-ABS-KEY(vortex)','wos_query':'TS=(vortex)','springer_query':'vortex','count_per_source':1}))
            assert combined['ok'] and not combined['partial_failure'] and set(combined['source_status'])=={'scopus','springer','wos','crossref'}
            print(json.dumps({'checkpoint':'four_source_search','ok':True,'source_status':combined['source_status']}),flush=True)
            for doi,expected_pages in DOIS:
                record=data(await session.call_tool('get_crossref_record',{'doi':doi,'include_abstract_markup':True}))['record']
                assert record['doi'].lower()==doi.lower() and record['abstract_available'] and record['abstract_markup']
                result=await session.call_tool('download_cambridge_pdf',{'doi':doi});info=data(result)['file']
                links=[x for x in result.content if x.type=='resource_link'];assert len(links)==1
                resource=(await session.read_resource(links[0].uri)).contents[0]
                raw=base64.b64decode(resource.blob,validate=True)
                pages=len(PdfReader(io.BytesIO(raw)).pages)
                assert pages==expected_pages==info['verification']['pages']
                assert len(raw)==info['byte_size'] and hashlib.sha256(raw).hexdigest()==info['sha256']
                if doi.endswith('11772'):assert info['verification']['institution_stamp']=='University Town of Shenzhen'
                dest=export/info['file_name'];dest.write_bytes(raw);dest.chmod(0o600)
                item={**{k:info[k] for k in ('doi','file_name','byte_size','sha256','verification','campus_direct_route','personal_login_used')},'abstract_characters':len(record['abstract']),'original_jats_abstract_available':bool(record['abstract_markup']),'mcp_resource_sha256_verified':True}
                evidence.append(item);print(json.dumps(item),flush=True)
    path=export/'validation.json';path.write_text(json.dumps(evidence,indent=2));path.chmod(0o600)

asyncio.run(main())
