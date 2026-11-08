"""Bounded AIP campus PDF transfer. Signed delivery URLs never leave memory."""
import http.client
import http.cookiejar
import io
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pypdf import PdfReader
from article_downloads import DownloadError,MAX_FILE,cache_verified_file,validate_doi
from crossref_cambridge import get_record

DOWNLOAD_SECONDS=1800
IDLE_SECONDS=60
DELIVERY_HOST='watermark02.silverchair.com'

def validate_url(url,role='redirect'):
    try:
        if not isinstance(url,str) or len(url)>20000 or any(ord(c)<33 for c in url) or '\\' in url:raise ValueError()
        p=urllib.parse.urlsplit(url);path=urllib.parse.unquote(p.path)
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443) or p.fragment:raise ValueError()
        if '\\' in path or any(x in ('.','..') for x in path.split('/')):raise ValueError()
        if p.hostname=='pubs.aip.org':
            if role=='pdf' and '/article-pdf/' not in p.path:raise ValueError()
            if role=='landing' and ('/article/' not in p.path and '/article-abstract/' not in p.path):raise ValueError()
        elif p.hostname==DELIVERY_HOST:
            if role!='redirect' or not p.path.lower().endswith('.pdf'):raise ValueError()
        else:raise ValueError()
    except ValueError:raise DownloadError('AIP返回了未获允许的文章或文件地址，已停止。', code='unregistered_or_unsafe_fulltext_location') from None
    return url

class DeliveryConnection(http.client.HTTPSConnection):
    def connect(self):
        if self.host!=DELIVERY_HOST:return super().connect()
        error=None
        for family,kind,protocol,canonical,address in socket.getaddrinfo(self.host,self.port,type=socket.SOCK_STREAM)[:4]:
            conn=socket.socket(family,kind,protocol);conn.settimeout(10)
            try:
                conn.connect(address)
                self.sock=self._context.wrap_socket(conn,server_hostname=self.host)
                self.sock.settimeout(self.timeout);return
            except OSError as exc:error=exc;conn.close()
        if error:raise error
        raise OSError('No delivery endpoints')

class DeliveryHandler(urllib.request.HTTPSHandler):
    def https_open(self,req):return self.do_open(DeliveryConnection,req,context=self._context)

class Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections=5
    def __init__(self,deadline):self.deadline=deadline
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if time.monotonic()>self.deadline:raise DownloadError('AIP下载已达到30分钟总时限。', code='download_time_budget_exhausted')
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

class CitationMetadata(HTMLParser):
    def __init__(self):super().__init__();self.values={}
    def handle_starttag(self,tag,attrs):
        a=dict(attrs);name=a.get('name','').lower()
        if tag=='meta' and name in ('citation_doi','citation_pdf_url','citation_title','citation_firstpage','citation_lastpage'):
            self.values[name]=a.get('content','')

class Session:
    def __init__(self,deadline):
        self.deadline=deadline;self.cookies=http.cookiejar.CookieJar()
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(self.cookies),
                    Redirect(deadline),DeliveryHandler(context=ssl.create_default_context()))

    def fetch(self,url,accept,maximum,progress):
        validate_url(url)
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise DownloadError('AIP下载已达到30分钟总时限。', code='download_time_budget_exhausted')
        req=urllib.request.Request(url,headers={'Accept':accept,'User-Agent':'CampusResearchMCP/1.8.0'})
        try:
            with self.opener.open(req,timeout=min(IDLE_SECONDS,remaining)) as response:
                validate_url(response.url)
                if response.status!=200:raise DownloadError('AIP未返回完整HTTP 200响应。')
                mime=response.headers.get_content_type()
                if mime!=accept:raise DownloadError('AIP未返回所需'+accept+'；可能是登录、权限或验证页面。')
                if response.headers.get('Content-Encoding','identity').lower() not in ('identity',''):raise DownloadError('AIP返回不支持的压缩传输。')
                length=response.headers.get('Content-Length')
                if length and (not length.isdigit() or not 0<int(length)<=maximum):raise DownloadError('AIP响应长度无效或超过大小限制。', code='file_size_limit')
                total=int(length) if length else None
                size=0;blocks=[];last=0.0
                progress(0,total)
                while True:
                    if time.monotonic()>self.deadline:raise DownloadError('AIP下载已达到30分钟总时限，未交付不完整文件。', code='download_time_budget_exhausted')
                    block=response.read1(min(64*1024,maximum+1-size))
                    if not block:break
                    size+=len(block)
                    if size>maximum:raise DownloadError('AIP文件超过大小限制。', code='file_size_limit')
                    blocks.append(block)
                    if time.monotonic()-last>=5:progress(size,total);last=time.monotonic()
                if total is not None and total!=size:raise DownloadError('AIP文件传输不完整，未交付。')
                progress(size,total)
                return b''.join(blocks)
        except urllib.error.HTTPError as exc:
            code=exc.code;exc.close()
            raise DownloadError('AIP HTTP '+str(code)+'：可能为订阅授权、站点验证或服务限制，未绕过或交付错误页。') from None
        except (OSError,ValueError,http.client.HTTPException):raise DownloadError('AIP校园文件连接失败或连续60秒无数据；可重试，未交付不完整文件。') from None

    def close(self):self.cookies.clear()

def verify_pdf(raw,doi,metadata):
    if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-2048:]:raise DownloadError('AIP返回的不是完整PDF。')
    try:
        reader=PdfReader(io.BytesIO(raw),strict=False)
        if reader.is_encrypted:raise DownloadError('AIP PDF已加密，无法校验。')
        pages=len(reader.pages)
        if pages<2:raise DownloadError('AIP PDF少于2页，无法排除首页预览。')
        text='\n'.join(reader.pages[i].extract_text() or '' for i in sorted({0,min(1,pages-1),pages-1}))
    except DownloadError:raise
    except Exception:raise DownloadError('AIP PDF结构无法解析。') from None
    if doi.lower() not in re.sub(r'[\s\u200b-\u200d\ufeff]+','',text).lower():raise DownloadError('AIP PDF内未识别到请求DOI。')
    first=metadata.get('citation_firstpage','');last=metadata.get('citation_lastpage','')
    expected=int(last)-int(first)+1 if first.isdigit() and last.isdigit() and int(last)>=int(first) else None
    if expected is not None and expected!=pages:raise DownloadError('AIP PDF页数与出版页码不符。')
    return {'pages':pages,'doi_in_pdf_text':True,'signature_eof_verified':True,'expected_pages':expected}

def prepare_aip_pdf(doi,update):
    validate_doi(doi);deadline=time.monotonic()+DOWNLOAD_SECONDS;session=Session(deadline)
    try:
        update(phase='metadata')
        record=get_record(doi)['record'];landing=record['resource_url']
        if landing.startswith('http://'):landing='https://'+landing[7:]
        validate_url(landing,'landing')
        update(phase='article_page',title=record['title'])
        raw=session.fetch(landing,'text/html',4*1024*1024,lambda n,t:None)
        parser=CitationMetadata();parser.feed(raw.decode('utf-8','replace'));meta=parser.values
        if meta.get('citation_doi','').lower()!=doi.lower():raise DownloadError('AIP文章页DOI不符或无法完成机构识别。')
        if not meta.get('citation_pdf_url'):raise DownloadError('AIP文章页未提供PDF入口，可能需要机构权限。')
        pdf_url=urllib.parse.urljoin(landing,meta['citation_pdf_url']);validate_url(pdf_url,'pdf')
        update(phase='pdf_download',received_bytes=0,total_bytes=None)
        body=session.fetch(pdf_url,'application/pdf',MAX_FILE,lambda n,t:update(received_bytes=n,total_bytes=t))
        update(phase='verifying')
        verification=verify_pdf(body,doi,meta)
        filename='aip-'+re.sub(r'[^A-Za-z0-9._-]','_',doi)[:160]+'.pdf'
        info,token=cache_verified_file(body,filename,'application/pdf','pdf',doi)
        info.update({'provider':'aip','title':record['title'],'campus_direct_route':True,'api_key_used':False,
                     'personal_login_used':False,'verification':verification,'signed_download_url_exposed':False})
        return info,token
    finally:session.close()
