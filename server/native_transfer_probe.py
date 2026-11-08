"""Temporary standard MCP file-transfer experiment; contains no article or secret."""
import base64
import hashlib
import io
import json
import secrets
import zipfile
from pathlib import Path
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ResourceLink, TextContent

URI = 'file:///campus-scopus-native-transfer-test.zip'
NAME = 'campus-scopus-native-transfer-test.zip'
PATH = Path('/var/lib/campus-scopus/native-transfer-probe.zip')

def probe_bytes():
    if not PATH.exists():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('probe.txt', 'MCP_NATIVE_TRANSFER_TEST_20261030\n')
            archive.writestr('payload.bin', secrets.token_bytes(32768))
        PATH.write_bytes(buffer.getvalue())
        PATH.chmod(0o600)
    return PATH.read_bytes()

def probe_result(link=False):
    data = probe_bytes()
    info = {'probe_only': True, 'file_name': NAME, 'byte_size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'resource_uri': URI,
            'transfer': 'resource_link' if link else 'embedded_resource'}
    item = ResourceLink(type='resource_link', uri=URI, name=NAME, mime_type='application/zip', size=len(data)) if link else EmbeddedResource(
        type='resource', resource=BlobResourceContents(uri=URI, mime_type='application/zip',
                                                     blob=base64.b64encode(data).decode('ascii')))
    return CallToolResult(content=[TextContent(type='text', text=json.dumps(info)), item],
                          structured_content=info)
