import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import xml.etree.ElementTree as ET
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pypdf import PdfReader

async def main():
    params = StdioServerParameters(command=sys.executable, args=['/opt/campus-scopus/server.py'], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            descriptor = next(t for t in (await session.list_tools()).tools if t.name == 'download_article')
            assert 'openai/outputTemplate' not in (descriptor.meta or {})
            assert 'ui' not in (descriptor.meta or {})
            for doi, fmt in [('10.1016/j.combustflame.2026.115355','pdf'),
                             ('10.1016/j.combustflame.2026.115285','xml')]:
                result = await session.call_tool('download_article', {'doi':doi,'format':fmt,'destination':'space'})
                assert not result.is_error, 'Native file download failed'
                info = result.structured_content['article']
                assert result.structured_content['space_saved'] is False
                assert 'space_receipt' not in info
                files = [x for x in result.content if x.type == 'resource_link']
                assert len(files) == 1
                resource = (await session.read_resource(files[0].uri)).contents[0]
                body = base64.b64decode(resource.blob, validate=True)
                assert len(body) == info['byte_size']
                assert hashlib.sha256(body).hexdigest() == info['sha256']
                if fmt == 'pdf':
                    assert len(PdfReader(io.BytesIO(body)).pages) == info['pdf_page_count'] == 9
                else:
                    root = ET.fromstring(body)
                    assert root.tag.rsplit('}',1)[-1] == 'full-text-retrieval-response'
                print(json.dumps({'native_attachment_protocol':True, 'sha256_verified':True,
                                  **{k:info[k] for k in ['doi','file_name','byte_size','sha256']},
                                  'pdf_pages':info.get('pdf_page_count')}))
            preview = await session.call_tool('download_article', {'doi':'10.1016/j.combustflame.2026.115285','format':'pdf'})
            assert preview.is_error, 'Single-page preview was delivered'
            print(json.dumps({'single_page_preview_rejected':True}))

asyncio.run(main())
