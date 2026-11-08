"""Deliver verified cached bytes through standard MCP file resources."""
import base64
import hashlib
import json
import re
from urllib.parse import quote
from mcp.types import BlobResourceContents, EmbeddedResource, ResourceLink
from article_downloads import CHUNK_SIZE, MAX_FILE, DownloadError, cache_dir, read_chunk

def article_bytes(metadata, token):
    size = metadata['byte_size']
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_FILE:
        raise DownloadError('文件大小无效，未交付附件。')
    blocks = []
    digest = hashlib.sha256()
    for offset in range(0, size, CHUNK_SIZE):
        info, encoded = read_chunk(token, offset)
        block = base64.b64decode(encoded, validate=True)
        expected_size = min(CHUNK_SIZE, size - offset)
        if (info['offset'] != offset or info['byte_count'] != expected_size
                or len(block) != expected_size):
            raise DownloadError('文件分片长度不匹配，未交付附件。')
        blocks.append(block)
        digest.update(block)
    if digest.hexdigest() != metadata['sha256']:
        raise DownloadError('文件完整性校验失败，未交付附件。')
    return b''.join(blocks)

def article_resource(metadata, token):
    data = article_bytes(metadata, token)
    return EmbeddedResource(type='resource', resource=BlobResourceContents(
        uri='file:///' + quote(metadata['file_name'], safe=''),
        mime_type=metadata['mime_type'],
        blob=base64.b64encode(data).decode('ascii')))

def article_link(metadata, token):
    return ResourceLink(type='resource_link',
        uri='file:///campus-scopus/' + metadata['format'] + '/' + token + '/' + quote(metadata['file_name'], safe=''),
        name=metadata['file_name'], mime_type=metadata['mime_type'], size=metadata['byte_size'],
        description='Verified literature attachment. Materialize through this MCP server; SHA-256: ' + metadata['sha256'])

def read_article_resource(token, filename, file_format):
    if not re.fullmatch(r'[a-f0-9]{48}', token):
        raise DownloadError('无效的文件标识。')
    try:
        metadata = json.loads((cache_dir() / (token + '.json')).read_text())
    except (OSError, ValueError):
        raise DownloadError('文件不可用或已过期。') from None
    if metadata.get('file_name') != filename or metadata.get('format') != file_format:
        raise DownloadError('文件资源名称或格式不匹配。')
    return article_bytes(metadata, token)
