import asyncio,base64,hashlib,json,os,sys
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    path=os.environ.get('CAMPUS_SERVER_PATH','/opt/campus-scopus/server.py')
    params=StdioServerParameters(command=sys.executable,args=[path],env=dict(os.environ))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            tools=await session.list_tools()
            chunk_tool=next(t for t in tools.tools if t.name=='read_article_chunk')
            assert chunk_tool.meta['ui']['visibility']==['app']
            resources=await session.list_resources()
            uri=next(str(r.uri) for r in resources.resources if 'article-download' in str(r.uri))
            widget=await session.read_resource(uri)
            assert widget.contents[0].mime_type=='text/html;profile=mcp-app'
            assert 'uploadFile' in widget.contents[0].text
            result=await session.call_tool('download_article',{'doi':'10.1016/j.combustflame.2026.115285','format':'xml'})
            assert not result.is_error, 'XML download rejected: '+str([getattr(x,'text','') for x in result.content])
            info=result.structured_content['article']
            assert 'space_receipt' not in info, 'A prior Page attachment must not imply a new Space save'
            token=result.meta['download_id']
            digest=hashlib.sha256();size=0;prefix=b''
            for offset in range(0,info['byte_size'],info['chunk_size']):
                chunk=await session.call_tool('read_article_chunk',{'download_id':token,'offset':offset})
                assert not chunk.is_error,'chunk transfer failed'
                assert 'data_base64' not in str(chunk.structured_content)
                raw=base64.b64decode(chunk.meta['data_base64']);digest.update(raw);size+=len(raw)
                if not prefix:prefix=raw[:5]
            assert prefix.startswith(b'<')
            assert size==info['byte_size'] and digest.hexdigest()==info['sha256']
            print(json.dumps({'live_xml_download':True,'mcp_chunk_transfer':True,'sha256_verified':True,
                              'byte_size':size,'doi':info['doi'],'file_name':info['file_name'],
                              'app_only_chunk_tool':True,'widget_resource_verified':True,
                              'space_receipt_verified':bool(info.get('space_receipt',{}).get('verified'))}))

asyncio.run(main())
