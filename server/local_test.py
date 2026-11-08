#!/usr/bin/env python3
import asyncio
import json
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=[os.environ.get('CAMPUS_SERVER_PATH','/opt/campus-scopus/server.py')], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            expected = sorted(['check_sciencedirect_access', 'download_article', 'download_article_bundle', 'get_scopus_record', 'ping', 'read_article_chunk', 'search_scopus',
                               'search_springer','get_springer_record','search_wos','get_wos_record','download_springer_xml','download_springer_bundle','search_literature',
                               'search_crossref','get_crossref_record','download_cambridge_pdf','download_wiley_pdf','download_springer_pdf','download_aip_pdf','get_aip_download',
                               'download_asme_pdf','download_acs_pdf','download_mdpi_pdf','get_publisher_download',
                               'search_arxiv','get_arxiv_record','list_ai_conferences','search_ai_conferences',
                               'list_research_sources','search_theses','get_literature_record','start_research_search','get_research_job',
                               'cancel_research_job','get_related_papers','resolve_fulltext','download_literature','export_search_run','import_references'])
            assert names == expected, names
            print(json.dumps({'checkpoint':'mcp_discovery','ok':True,'tools':names}))
            result = await session.call_tool('ping', {})
            assert not result.is_error, 'ping failed'
            payload=result.structured_content or json.loads(''.join(x.text for x in result.content if x.type=='text'))
            assert payload['version']=='1.13.0' and payload['publisher_campus_pdf_available']==['asme','acs','mdpi']
            catalogue=await session.call_tool('list_ai_conferences',{})
            catalogue_data=catalogue.structured_content or json.loads(''.join(x.text for x in catalogue.content if x.type=='text'))
            assert len(catalogue_data['conferences'])==20
            print(json.dumps({'checkpoint':'mcp_ping', 'ok':True,'result':result.model_dump(mode='json')}))
            invalid = await session.call_tool('search_scopus', {'query':'combustion','count':100})
            assert invalid.is_error, 'count limit not enforced'
            print(json.dumps({'checkpoint':'input_limits','ok':True}))
            for name,args in [('download_asme_pdf',{'doi':'10.1021/test'}),('download_acs_pdf',{'doi':'10.3390/test'}),
                              ('download_mdpi_pdf',{'doi':'10.1115/test'}),('get_publisher_download',{'job_id':'x'*48,'wait_seconds':21})]:
                rejected=await session.call_tool(name,args);assert rejected.is_error,name
            print(json.dumps({'checkpoint':'publisher_input_limits','ok':True}))
            for name,args in [('search_arxiv',{'query':'test','count':11}),('get_arxiv_record',{'arxiv_id':'https://evil.test/x'}),
                              ('search_ai_conferences',{'query':'test','conferences':'unknown'})]:
                rejected=await session.call_tool(name,args);assert rejected.is_error,name
            print(json.dumps({'checkpoint':'ai_search_input_limits','ok':True}))
            if '--live' in sys.argv:
                live = await session.call_tool('search_scopus', {'query':'TITLE-ABS-KEY(combustion)','count':1})
                assert not live.is_error, 'MCP search execution failed'
                data = json.loads(''.join(x.text for x in live.content if x.type == 'text'))
                assert data.get('ok') and data.get('records'), 'Real MCP search did not return records'
                print(json.dumps({'checkpoint':'mcp_live_scopus_search','ok':True,'http_status':data.get('http_status'),
                                  'returned_records':len(data['records'])}))
                eid = data['records'][0].get('eid')
                assert eid, 'Search result lacks EID'
                detail = await session.call_tool('get_scopus_record', {'eid':eid})
                assert not detail.is_error, 'MCP record execution failed'
                record = json.loads(''.join(x.text for x in detail.content if x.type == 'text'))
                assert record.get('ok') and record.get('record'), 'MCP record retrieval failed'
                print(json.dumps({'checkpoint':'mcp_live_scopus_record','ok':True,'http_status':record.get('http_status')}))
                sd = await session.call_tool('check_sciencedirect_access', {'doi':'10.1016/j.combustflame.2026.115285'})
                assert not sd.is_error, 'MCP SD execution failed'
                access = json.loads(''.join(x.text for x in sd.content if x.type == 'text'))
                assert access.get('ok'), 'MCP ScienceDirect API access failed'
                print(json.dumps({'checkpoint':'mcp_live_sciencedirect_access','ok':True,'http_status':access.get('http_status')}))

if __name__ == '__main__':
    asyncio.run(main())
