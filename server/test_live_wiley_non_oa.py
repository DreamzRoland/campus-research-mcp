"""Verify closed-status Wiley articles and native PDF delivery through campus MCP."""
import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pypdf import PdfReader

DOIS=['10.1002/fld.2124','10.1002/fld.5156']
DIRECT=urllib.request.build_opener(urllib.request.ProxyHandler({}))

def content(result):
    assert not result.is_error,'MCP execution failed'
    return result.structured_content or json.loads(''.join(x.text for x in result.content if x.type=='text'))

def oa_evidence(doi):
    url='https://api.openalex.org/works/https://doi.org/'+doi+'?'+urllib.parse.urlencode({'select':'doi,title,open_access,primary_location'})
    with DIRECT.open(urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'CampusResearchMCP/1.6.0'}),timeout=25) as response:
        data=json.load(response)
    oa=data.get('open_access') or {};location=data.get('primary_location') or {}
    assert data['doi'].lower()=='https://doi.org/'+doi.lower()
    assert oa.get('is_oa') is False and oa.get('oa_status')=='closed' and location.get('is_oa') is False
    return {'source':'OpenAlex','source_api':url,'checked_at_utc':datetime.now(timezone.utc).isoformat(),
            'is_oa':oa['is_oa'],'oa_status':oa['oa_status'],'publisher_version_is_oa':location['is_oa'],
            'any_repository_has_fulltext':oa.get('any_repository_has_fulltext'),'title':data['title']}

async def main():
    export=Path('/var/lib/campus-scopus/wiley-non-oa-test');export.mkdir(mode=0o700,exist_ok=True)
    evidence=[]
    params=StdioServerParameters(command=sys.executable,args=['/opt/campus-scopus/server.py'],env=dict(os.environ))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            state=content(await session.call_tool('ping',{}));assert state['wiley_tdm_token_configured']
            for doi in DOIS:
                status=oa_evidence(doi)
                print(json.dumps({'checkpoint':'non_oa_status','doi':doi,'is_oa':status['is_oa'],'oa_status':status['oa_status'],'publisher_version_is_oa':status['publisher_version_is_oa']}),flush=True)
                record=content(await session.call_tool('get_crossref_record',{'doi':doi}))['record']
                result=await session.call_tool('download_wiley_pdf',{'doi':doi})
                if result.is_error:
                    item={'doi':doi,'ok':False,'oa_evidence':status,'error':' '.join(x.text for x in result.content if x.type=='text')[:600]}
                    evidence.append(item);print(json.dumps(item,ensure_ascii=False),flush=True);continue
                info=content(result)['file'];links=[x for x in result.content if x.type=='resource_link'];assert len(links)==1
                resource=(await session.read_resource(links[0].uri)).contents[0]
                raw=base64.b64decode(resource.blob,validate=True)
                pages=len(PdfReader(io.BytesIO(raw)).pages)
                assert pages==info['verification']['pages']>=2
                assert len(raw)==info['byte_size'] and hashlib.sha256(raw).hexdigest()==info['sha256']
                assert info['tdm_api_used'] is True and info['campus_direct_route'] is True
                path=export/info['file_name'];path.write_bytes(raw);path.chmod(0o600)
                item={'ok':True,'oa_evidence':status,'source':'Wiley TDM API via existing campus MCP',
                      **{k:info[k] for k in ('doi','title','file_name','byte_size','sha256','verification','campus_direct_route','personal_login_used','redirect_count','tdm_api_used')},
                      'journal':record['source'],'publication_date':record['date'],'publisher_page_range':record.get('page_range'),
                      'mcp_resource_sha256_verified':True,'space_saved_verified':False}
                evidence.append(item);print(json.dumps(item,ensure_ascii=False),flush=True)
    report=export/'validation.json';report.write_text(json.dumps(evidence,ensure_ascii=False,indent=2));report.chmod(0o600)
    assert len(evidence)==2 and all(r['ok'] for r in evidence),'Some non-OA requests failed; see sanitized report'

asyncio.run(main())
