import asyncio
import base64
import hashlib
import json
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=['/opt/campus-scopus/server.py'], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for mode in ['native-probe', 'native-probe-link']:
                result = await session.call_tool('download_article', {'doi':'10.1016/j.combustflame.2026.115355','destination':mode})
                assert not result.is_error
                info = result.structured_content
                if mode == 'native-probe':
                    data = base64.b64decode(next(x.resource.blob for x in result.content if x.type == 'resource'))
                else:
                    uri = next(x.uri for x in result.content if x.type == 'resource_link')
                    resource = await session.read_resource(uri)
                    data = base64.b64decode(resource.contents[0].blob)
                assert len(data) == info['byte_size']
                assert hashlib.sha256(data).hexdigest() == info['sha256']
                print(json.dumps({'ok':True, **info}))

asyncio.run(main())
