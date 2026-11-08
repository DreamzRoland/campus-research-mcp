"""Bounded article downloads and private, expiring transfers over MCP."""
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_FILE = 20 * 1024 * 1024
CHUNK_SIZE = 512 * 1024
MAX_CACHE = 512 * 1024 * 1024
LIFETIME = 24 * 3600
_lock = threading.Lock()
_inflight = set()

class DownloadError(Exception):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def cache_dir():
    path = Path(os.environ.get('CAMPUS_DOWNLOAD_DIR', '/var/lib/campus-scopus/downloads'))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path

def validate_doi(doi):
    if not re.fullmatch(r'10\.\d{4,9}/[^\s\x00-\x1f]{1,250}', doi):
        raise DownloadError('请提供有效 DOI，例如 10.1016/j.combustflame.2026.115285。')

def validate_pdf_content(path):
    try:
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise DownloadError('返回的 PDF 已加密，无法核验全文。')
        pages = len(reader.pages)
    except (PdfReadError, OSError, ValueError, TypeError):
        raise DownloadError('返回的 PDF 结构无效，未作为全文提供。') from None
    if pages == 0:
        raise DownloadError('返回的 PDF 没有页面，未作为全文提供。')
    if pages == 1:
        raise DownloadError('ScienceDirect 返回的 PDF 只有 1 页，可能是首页预览，无法确认全文；已停止交付。可改用 format="xml" 获取并检查正文，或核实机构的 PDF/API 权限。')
    return pages

def confirmed_space_receipt(metadata):
    """Use only separately verified persistent file receipts matching these bytes."""
    path = Path(os.environ.get('CAMPUS_SPACE_RECEIPTS', '/var/lib/campus-scopus/space_receipts.json'))
    try:
        receipt = json.loads(path.read_text()).get(metadata['sha256'])
        if (isinstance(receipt, dict) and receipt.get('verified') is True
                and receipt.get('sha256') == metadata['sha256']
                and receipt.get('byte_size') == metadata['byte_size']
                and receipt.get('doi') == metadata['doi']
                and urllib.parse.urlsplit(receipt.get('page_url', '')).hostname == 'chatgpt.com'
                and urllib.parse.urlsplit(receipt['page_url']).scheme == 'https'):
            return receipt
    except (OSError, ValueError, KeyError):
        pass
    return None

def _cleanup(path):
    now = time.time()
    for partial in path.glob('*.part'):
        if re.fullmatch(r'[a-f0-9]{48}\.part', partial.name) and now-partial.stat().st_mtime > LIFETIME:
            partial.unlink(missing_ok=True)
    for manifest in path.glob('*.json'):
        if not re.fullmatch(r'[a-f0-9]{48}\.json', manifest.name):
            continue
        try:
            data = json.loads(manifest.read_text())
            if data.get('expires_at', 0) < now:
                (path / (manifest.stem + '.bin')).unlink(missing_ok=True)
                manifest.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue
    return sum(p.stat().st_size for p in path.glob('*.bin') if p.is_file())

def _confirmed_cached_article(directory, doi, file_format):
    """Reuse the exact saved bytes, never infer file identity from DOI alone."""
    for manifest in directory.glob('*.json'):
        if not re.fullmatch(r'[a-f0-9]{48}\.json', manifest.name):
            continue
        try:
            data = json.loads(manifest.read_text())
            if (data.get('doi') != doi or data.get('format') != file_format
                    or data.get('expires_at', 0) <= time.time()):
                continue
            receipt = confirmed_space_receipt(data)
            if not receipt:
                continue
            binary = directory / (manifest.stem + '.bin')
            if not 0 < binary.stat().st_size == data['byte_size'] <= MAX_FILE:
                continue
            if hashlib.sha256(binary.read_bytes()).hexdigest() != data['sha256']:
                continue
            if file_format == 'pdf':
                data['pdf_page_count'] = validate_pdf_content(binary)
                data['fulltext_verified'] = False
            data['space_receipt'] = receipt
            return data, manifest.stem
        except (OSError, ValueError, KeyError, TypeError, DownloadError):
            continue
    return None

def prepare_article(doi, file_format, key, institutional_token=''):
    validate_doi(doi)
    if file_format not in ('pdf', 'xml'):
        raise DownloadError('format 只支持 pdf 或 xml。')
    if not key:
        raise DownloadError('尚未配置 ScienceDirect API key。')
    directory = cache_dir()
    token = secrets.token_hex(24)
    with _lock:
        used = _cleanup(directory)
        saved = _confirmed_cached_article(directory, doi, file_format)
        if saved:
            return saved
        if used + (len(_inflight)+1)*MAX_FILE > MAX_CACHE:
            raise DownloadError('临时文件缓存已满，请稍后重试。', code='private_file_cache_full')
        _inflight.add(token)
    temporary = directory / (token + '.part')
    binary = directory / (token + '.bin')
    manifest = directory / (token + '.json')
    mime = 'application/pdf' if file_format == 'pdf' else 'application/xml'
    headers = {'X-ELS-APIKey': key, 'Accept': mime}
    if institutional_token:
        headers['X-ELS-Insttoken'] = institutional_token
    url = 'https://api.elsevier.com/content/article/doi/' + urllib.parse.quote(doi, safe='')
    url += '?' + urllib.parse.urlencode({'view': 'FULL', 'httpAccept': mime})
    digest = hashlib.sha256()
    size = 0
    prefix = b''
    deadline = time.monotonic() + 85
    try:
        with DIRECT.open(urllib.request.Request(url, headers=headers), timeout=20) as response:
            length = response.headers.get('Content-Length', '')
            if length.isdigit() and int(length) > MAX_FILE:
                raise DownloadError('文件超过 20 MiB 上限。', code='file_size_limit')
            content_type = response.headers.get('Content-Type', '').split(';')[0].lower()
            expected = {'application/pdf'} if file_format == 'pdf' else {'application/xml', 'text/xml'}
            if content_type not in expected:
                raise DownloadError('上游没有返回所请求的文件格式；未保存错误页面。')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                while True:
                    if time.monotonic() > deadline:
                        raise DownloadError('文献下载超时，请稍后重试。', code='download_time_budget_exhausted')
                    block = response.read(64 * 1024)
                    if not block:
                        break
                    if len(prefix) < 4096:
                        prefix += block[:4096-len(prefix)]
                    size += len(block)
                    if size > MAX_FILE:
                        raise DownloadError('文件超过 20 MiB 上限。', code='file_size_limit')
                    digest.update(block)
                    output.write(block)
            if length.isdigit() and size != int(length):
                raise DownloadError('文件未完整下载，请重试。')
        if not size:
            raise DownloadError('上游返回了空文件。')
        if file_format == 'pdf' and not prefix.startswith(b'%PDF-'):
            raise DownloadError('返回内容不是有效 PDF，未保存。')
        pdf_pages = validate_pdf_content(temporary) if file_format == 'pdf' else None
        if file_format == 'xml':
            try:
                root = ET.parse(temporary).getroot()
                if root.tag.rsplit('}',1)[-1] not in ('full-text-retrieval-response','doc'):
                    raise DownloadError('返回内容不是文献 XML，未保存。')
            except ET.ParseError:
                raise DownloadError('文献 XML 格式无效，未保存。') from None
        filename = 'article-' + re.sub(r'[^A-Za-z0-9._-]', '_', doi)[:170] + '.' + file_format
        data = {'doi':doi, 'format':file_format, 'file_name':filename, 'mime_type':mime,
                'byte_size':size, 'sha256':digest.hexdigest(), 'chunk_size':CHUNK_SIZE,
                'expires_at':int(time.time())+LIFETIME}
        if pdf_pages is not None:
            data.update(pdf_page_count=pdf_pages, fulltext_verified=False)
        receipt = confirmed_space_receipt(data)
        if receipt:
            data['space_receipt'] = receipt
        temporary.replace(binary)
        fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output)
        return data, token
    except urllib.error.HTTPError as exc:
        raise DownloadError(f'ScienceDirect 返回 HTTP {exc.code}；当前文献或所选格式可能未获授权。') from None
    except (OSError, ValueError):
        raise DownloadError('文献下载连接失败，请稍后重试。') from None
    finally:
        temporary.unlink(missing_ok=True)
        if not manifest.exists():
            binary.unlink(missing_ok=True)
        with _lock:
            _inflight.discard(token)

def cache_generated_file(body, filename, mime_type, file_format, doi):
    """Store bounded generated attachments using the same private cache budget."""
    if (file_format,mime_type) not in [('html','text/html'),('zip','application/zip')]:
        raise DownloadError('生成的附件格式无效。')
    return cache_verified_file(body, filename, mime_type, file_format, doi)

def cache_verified_file(body, filename, mime_type, file_format, doi):
    """Cache bytes already validated by the upstream-specific download client."""
    validate_doi(doi)
    if isinstance(body, bytes) and len(body) > MAX_FILE:
        raise DownloadError('生成的附件格式或大小无效。', code='file_size_limit')
    if (not isinstance(body, bytes) or not 0 < len(body) <= MAX_FILE
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,210}', filename)
            or (file_format, mime_type) not in [('html', 'text/html'), ('zip', 'application/zip'),
                                              ('xml','application/xml'),('pdf','application/pdf')]):
        raise DownloadError('生成的附件格式或大小无效。')
    directory = cache_dir()
    token = secrets.token_hex(24)
    binary = directory / (token + '.bin')
    manifest = directory / (token + '.json')
    with _lock:
        if _cleanup(directory) + len(_inflight)*MAX_FILE + len(body) > MAX_CACHE:
            raise DownloadError('临时文件缓存已满。', code='private_file_cache_full')
        metadata = {'doi':doi, 'format':file_format, 'file_name':filename, 'mime_type':mime_type,
                    'byte_size':len(body), 'sha256':hashlib.sha256(body).hexdigest(),
                    'chunk_size':CHUNK_SIZE, 'expires_at':int(time.time())+LIFETIME}
        try:
            fd = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output: output.write(body)
            fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output: json.dump(metadata, output)
        except OSError:
            binary.unlink(missing_ok=True); manifest.unlink(missing_ok=True)
            raise DownloadError('生成附件缓存失败。') from None
    return metadata, token

def read_chunk(download_id, offset):
    if not re.fullmatch(r'[a-f0-9]{48}', download_id):
        raise DownloadError('无效的文件标识。')
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise DownloadError('无效的文件偏移。')
    directory = cache_dir()
    try:
        data = json.loads((directory / (download_id + '.json')).read_text())
        if data['expires_at'] < time.time():
            raise DownloadError('临时文件已过期，请重新下载文献。')
        if offset > data['byte_size'] or offset % CHUNK_SIZE:
            raise DownloadError('无效的文件偏移。')
        if data.get('format') == 'pdf' and offset == 0:
            validate_pdf_content(directory / (download_id + '.bin'))
        with (directory / (download_id + '.bin')).open('rb') as source:
            source.seek(offset)
            block = source.read(CHUNK_SIZE)
        return {'offset':offset, 'next_offset':offset+len(block), 'byte_count':len(block),
                'eof':offset+len(block) >= data['byte_size']}, base64.b64encode(block).decode('ascii')
    except (OSError, ValueError, KeyError):
        raise DownloadError('文件不可用或已过期，请重新下载文献。') from None
