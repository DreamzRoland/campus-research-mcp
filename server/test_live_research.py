"""Live integration; prints only booleans, record IDs and digest metadata."""
import asyncio,base64,hashlib,json,os,sys
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

def content(result):
    if result.is_error:raise RuntimeError('MCP execution failed (details withheld).')
    if result.structured_content is not None:return result.structured_content
    return json.loads(''.join(c.text for c in result.content if c.type=='text'))

async def main():
    params=StdioServerParameters(command=sys.executable,args=['/opt/campus-scopus/server.py'],env=dict(os.environ))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            state=content(await session.call_tool('ping',{}))
            print(json.dumps({'check':'ping','state':state}))
            queries=[('springer_meta_key_configured','search_springer',{'query':'combustion','count':1}),
                     ('springer_oa_key_configured','search_springer',{'query':'combustion','collection':'openaccess','count':1}),
                     ('wos_starter_key_configured','search_wos',{'query':'TS=(combustion)','limit':1})]
            for present,tool,args in queries:
                if not state.get(present):
                    print(json.dumps({'tool':tool,'collection':args.get('collection','meta'),'skipped':'missing_api_key'}));continue
                data=content(await session.call_tool(tool,args))
                print(json.dumps({'tool':tool,'collection':args.get('collection'),'ok':data.get('ok'),'error':data.get('error'),
                                  'http_status':data.get('http_status'),'records':len(data.get('records',[]))}))
                if not data.get('ok') or not data.get('records'):continue
                entry=data['records'][0]
                if tool=='search_wos':
                    detail=content(await session.call_tool('get_wos_record',{'uid':entry['uid']}))
                    print(json.dumps({'check':'wos_record','ok':detail.get('ok'),'uid':entry['uid']}))
                else:
                    collection=args.get('collection','meta');doi=entry['doi']
                    detail=content(await session.call_tool('get_springer_record',{'doi':doi,'collection':collection}))
                    print(json.dumps({'check':'springer_record','collection':collection,'ok':detail.get('ok'),'doi':doi}))
                    if collection=='openaccess':
                        result=await session.call_tool('download_springer_xml',{'doi':doi})
                        if result.is_error:
                            print(json.dumps({'check':'springer_jats','ok':False,'error':'tool_returned_error'}));continue
                        info=content(result)['file'];links=[c for c in result.content if c.type=='resource_link']
                        assert len(links)==1
                        resource=(await session.read_resource(links[0].uri)).contents[0]
                        raw=base64.b64decode(resource.blob)
                        assert len(raw)==info['byte_size'] and hashlib.sha256(raw).hexdigest()==info['sha256']
                        print(json.dumps({'check':'springer_jats_resource','ok':True,'doi':doi,'bytes':len(raw),'sha256':info['sha256']}))
            unified=content(await session.call_tool('search_literature',{'scopus_query':'TITLE-ABS-KEY(combustion)',
                                      'wos_query':'TS=(combustion)','springer_query':'combustion','count_per_source':1}))
            print(json.dumps({'check':'federated_search','ok':unified['ok'],'partial_failure':unified['partial_failure'],
                              'source_status':unified['source_status'],'unique_records':len(unified['results'])}))

asyncio.run(main())
