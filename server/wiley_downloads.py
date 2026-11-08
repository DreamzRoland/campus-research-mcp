"""Wiley TDM PDFs via campus IP; no credential-bearing URLs or public file service."""
import fcntl
import http.client
import io
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from pypdf import PdfReader
from article_downloads import DownloadError, MAX_FILE, cache_verified_file, validate_doi
from crossref_cambridge import get_record

API='https://api.wiley.com/onlinelibrary/tdm/v1/articles/'
TOKEN_HEADER='Wiley-TDM-Client-Token'
_slots=threading.BoundedSemaphore(1)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None

DIRECT=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

def allowed_url(url):
    try:
        if not isinstance(url,str) or len(url)>16000 or any(ord(c)<33 for c in url) or '\\' in url:raise ValueError()
        p=urllib.parse.urlsplit(url)
        host=p.hostname or ''
        if (p.scheme!='https' or p.username or p.password or p.port not in (None,443) or p.fragment
                or not (host in ('api.wiley.com','alm.wiley.com','onlinelibrary.wiley.com') or host.endswith('.onlinelibrary.wiley.com'))):raise ValueError()
        decoded=urllib.parse.unquote(p.path)
        if '\\' in decoded or any(s in ('.','..') for s in decoded.split('/')):raise ValueError()
    except ValueError:raise DownloadError('Wiley返回了未获允许的下载地址；已停止，未向该地址发送Token。', code='unregistered_or_unsafe_fulltext_location') from None
    return url

def rate_slot(retry_after=None):
    """Persist the pacing across workers/restarts; never hold a token in this file."""
    path=Path(os.environ.get('CAMPUS_WILEY_RATE_FILE','/var/lib/campus-scopus/wiley-rate.json'))
    try:
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'r+') as stream:
            try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise DownloadError('Wiley请求正在排队，请稍后重试。') from None
            os.fchmod(stream.fileno(),0o600)
            raw=stream.read(1024)
            state=json.loads(raw) if raw else {}
            next_at=float(state.get('not_before',0))
            if not math.isfinite(next_at):raise ValueError()
            now=time.time()
            if retry_after is not None:
                next_at=max(next_at,now+retry_after)
            else:
                delay=max(0,next_at-now)
                if delay>11:raise DownloadError('Wiley限流退避中，请约'+str(math.ceil(delay))+'秒后重试。')
                if delay:time.sleep(delay)
                next_at=time.time()+10.1
            stream.seek(0);json.dump({'not_before':next_at},stream);stream.truncate();stream.flush();os.fsync(stream.fileno())
    except DownloadError:raise
    except (OSError,ValueError,TypeError):raise DownloadError('Wiley限流状态不可用，已停止请求。') from None

def retry_seconds(value):
    try:
        if value and value.strip().isdigit():return max(10,int(value.strip()))
        if value:return max(10,math.ceil(parsedate_to_datetime(value).timestamp()-time.time()))
    except (ValueError,TypeError,OverflowError):pass
    return 600

def read_pdf_response(response,deadline):
    if response.status!=200:raise DownloadError('Wiley未返回完整文件HTTP 200。')
    if response.headers.get_content_type()!='application/pdf':raise DownloadError('Wiley未返回PDF；可能是登录页、站点验证或错误页。')
    if response.headers.get('Content-Encoding','identity').lower() not in ('identity',''):
        raise DownloadError('Wiley返回了不支持的压缩传输。')
    length=response.headers.get('Content-Length')
    if length and (not length.isdigit() or not 0<int(length)<=MAX_FILE):raise DownloadError('Wiley文件大小无效或超过20MiB。', code='file_size_limit')
    blocks=[];size=0
    while True:
        if time.monotonic()>deadline:raise DownloadError('Wiley下载超过时限。', code='download_time_budget_exhausted')
        block=response.read(min(1024*1024,MAX_FILE+1-size))
        if not block:break
        size+=len(block)
        if size>MAX_FILE:raise DownloadError('Wiley文件超过20MiB。', code='file_size_limit')
        blocks.append(block)
    if length and size!=int(length):raise DownloadError('Wiley文件传输不完整。')
    return b''.join(blocks)

def fetch_pdf(doi,token,deadline):
    url=API+urllib.parse.quote(doi,safe='')
    for hop in range(6):
        allowed_url(url)
        # A signed download URL stays private, and must never contain our client token.
        if token in url or token in urllib.parse.unquote(url):raise DownloadError('Wiley下载地址含认证字段，已停止。', code='unregistered_or_unsafe_fulltext_location')
        rate_slot()
        remaining=deadline-time.monotonic()
        if remaining<=0:raise DownloadError('Wiley下载超过时限。', code='download_time_budget_exhausted')
        request=urllib.request.Request(url,headers={'Accept':'application/pdf','User-Agent':'CampusResearchMCP/1.6.0'})
        # Explicit manual redirects: the TDM token goes ONLY to the initial fixed API request.
        if hop==0:request.add_unredirected_header(TOKEN_HEADER,token)
        try:
            with DIRECT.open(request,timeout=min(30,remaining)) as response:
                allowed_url(response.url)
                if response.url!=url:raise DownloadError('Wiley发生未核验的重定向，已停止。', code='unregistered_or_unsafe_fulltext_location')
                return read_pdf_response(response,deadline),hop
        except urllib.error.HTTPError as exc:
            code=exc.code
            location=exc.headers.get('Location','')
            retry=exc.headers.get('Retry-After')
            exc.close()
            if code in (301,302,303,307,308):
                if hop==5 or not location:raise DownloadError('Wiley下载跳转过多或缺少地址。', code='unregistered_or_unsafe_fulltext_location') from None
                url=urllib.parse.urljoin(url,location);allowed_url(url)
                continue
            if code==429:
                rate_slot(retry_seconds(retry))
                raise DownloadError('Wiley HTTP 429：已按Retry-After暂停请求，请稍后重试。') from None
            messages={400:'Token请求未被接受',401:'认证失败',403:'Token或访问授权被拒绝',404:'DOI不存在或当前校园IP无该文献权限'}
            raise DownloadError('Wiley HTTP '+str(code)+'：'+messages.get(code,'服务错误或访问限制')+'；未交付错误页。') from None
        except (OSError,http.client.HTTPException):raise DownloadError('Wiley校园直连网络、TLS或传输失败。') from None
    raise DownloadError('Wiley下载跳转失败。')

def verify_pdf(raw,doi,metadata=None):
    if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-2048:]:raise DownloadError('Wiley返回文件不是完整PDF。')
    try:
        reader=PdfReader(io.BytesIO(raw),strict=False)
        if reader.is_encrypted:raise DownloadError('Wiley PDF已加密，无法核验。')
        pages=len(reader.pages)
        if pages<2:raise DownloadError('Wiley PDF少于2页，无法排除单页预览，未交付。')
        sample='\n'.join(reader.pages[i].extract_text() or '' for i in sorted({0,min(1,pages-1),min(2,pages-1),pages-1}))
    except DownloadError:raise
    except Exception:raise DownloadError('Wiley PDF结构无法解析。') from None
    normalized=re.sub(r'\s+','',sample).lower()
    doi_match=doi.lower() in normalized
    metadata=metadata or {}
    page_range=re.fullmatch(r'(\d+)\s*[-–]\s*(\d+)',metadata.get('page_range',''))
    expected=int(page_range[2])-int(page_range[1])+1 if page_range else None
    if expected is not None and expected!=pages:raise DownloadError('Wiley PDF页数与Crossref出版页码范围不符，未交付。')
    title=metadata.get('title','')
    normalize_title=lambda text:re.sub(r'[^a-z0-9]','',text.lower())
    title_normalized=normalize_title(title)
    first_pages='\n'.join(reader.pages[i].extract_text() or '' for i in range(min(2,pages)))
    title_match=len(title_normalized)>=30 and title_normalized in normalize_title(first_pages)
    fallback=title_match and expected==pages
    if not doi_match and not fallback:raise DownloadError('Wiley PDF无法核对请求DOI，也未同时匹配完整题名和出版页码范围。')
    return {'pages':pages,'doi_in_pdf_text':doi_match,'signature_eof_verified':True,
            'expected_pages':expected,'title_and_page_range_verified':fallback,
            'identity_method':'doi_text' if doi_match else 'exact_title_and_page_range'}

def prepare_wiley_pdf(doi,token):
    validate_doi(doi)
    if not isinstance(token,str) or not token:raise DownloadError('尚未配置WILEY_TDM_TOKEN，请在本机交互终端隐藏录入。')
    if len(token)>1024 or any(ord(c)<33 or ord(c)>126 for c in token):raise DownloadError('Wiley Token格式无效，请重新在终端录入。')
    if not _slots.acquire(blocking=False):raise DownloadError('已有Wiley下载任务，请稍后重试。')
    try:
        record=get_record(doi)['record']
        landing=record['resource_url']
        if landing.startswith('http://'):landing='https://'+landing[7:]
        allowed_url(landing)
        raw,hops=fetch_pdf(doi,token,time.monotonic()+100)
        if token.encode() in raw:raise DownloadError('Wiley响应含认证字段，已停止附件交付。')
        verification=verify_pdf(raw,doi,record)
        filename='wiley-'+re.sub(r'[^A-Za-z0-9._-]','_',doi)[:160]+'.pdf'
        info,cache_token=cache_verified_file(raw,filename,'application/pdf','pdf',doi)
        info.update({'provider':'wiley','title':record['title'],'doi_url':record['doi_url'],
                     'campus_direct_route':True,'personal_login_used':False,'verification':verification,
                     'tdm_api_used':True,'redirect_count':hops,'signed_download_url_exposed':False})
        return info,cache_token
    finally:_slots.release()
