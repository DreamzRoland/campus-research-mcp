"""ASME/ACS/MDPI original PDFs via direct campus HTTP; no keys or user sessions."""
import http.client
import http.cookiejar
import io
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pypdf import PdfReader
from article_downloads import DownloadError,MAX_FILE,cache_verified_file,validate_doi
from aip_downloads import DeliveryHandler,CitationMetadata
from crossref_cambridge import get_record

DOWNLOAD_SECONDS=1800
IDLE_SECONDS=60
PROVIDERS={'asme':'10.1115/','acs':'10.1021/','mdpi':'10.3390/'}
DELIVERY='watermark02.silverchair.com'

def validate_request(provider,doi):
    if provider not in PROVIDERS:raise DownloadError('只支持asme、acs或mdpi。')
    validate_doi(doi)
    if not doi.lower().startswith(PROVIDERS[provider]):raise DownloadError('DOI前缀与所选出版社不符。')

def validate_url(provider,url,role='redirect'):
    try:
        if provider not in PROVIDERS or role not in ('landing','pdf','redirect'):raise ValueError()
        if not isinstance(url,str) or len(url)>20000 or any(ord(c)<33 for c in url) or '\\' in url:raise ValueError()
        p=urllib.parse.urlsplit(url);path=urllib.parse.unquote(p.path)
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443) or p.fragment:raise ValueError()
        if '\\' in path or any(x in ('.','..') for x in path.split('/')):raise ValueError()
        if p.hostname==DELIVERY:
            if provider!='asme' or role!='redirect' or not path.lower().endswith('.pdf'):raise ValueError()
        elif provider=='asme' and p.hostname=='asmedigitalcollection.asme.org':
            if role=='landing' and not ('/article/' in path or '/article-abstract/' in path):raise ValueError()
            if role=='pdf' and '/article-pdf/' not in path:raise ValueError()
        elif provider=='acs' and p.hostname=='pubs.acs.org':
            if role=='landing' and not (path.startswith('/doi/') or '/article/' in path or '/article-abstract/' in path):raise ValueError()
            if role=='pdf' and not ('/article-pdf/' in path or path.startswith('/doi/pdf/')):raise ValueError()
        elif provider=='mdpi' and p.hostname in ('www.mdpi.com','mdpi.com'):
            if not re.fullmatch(r'/\d{4}-\d{3}[\dXx]/\d+/\d+/\d+(?:/(?:pdf|pdf-vor))?/?',path):raise ValueError()
            if role=='pdf' and not re.search(r'/(?:pdf|pdf-vor)/?$',path):raise ValueError()
            if role=='landing' and re.search(r'/(?:pdf|pdf-vor)/?$',path):raise ValueError()
        else:raise ValueError()
        if p.hostname!=DELIVERY and any(k.lower() in ('apikey','api_key','key','token','access_token','authorization') for k,_ in urllib.parse.parse_qsl(p.query)):raise ValueError()
    except (ValueError,TypeError):raise DownloadError('出版社返回未获允许的HTTPS文章或PDF地址，已停止。', code='unregistered_or_unsafe_fulltext_location') from None
    return url

class Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections=5
    def __init__(self,provider,deadline):self.provider=provider;self.deadline=deadline
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if time.monotonic()>self.deadline:raise DownloadError('文献下载已达到30分钟总时限。', code='download_time_budget_exhausted')
        validate_url(self.provider,newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

class Session:
    def __init__(self,provider,deadline):
        self.provider=provider;self.deadline=deadline;self.cookies=http.cookiejar.CookieJar();self.last_mime=None
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(self.cookies),
            Redirect(provider,deadline),DeliveryHandler(context=ssl.create_default_context()))
    def fetch(self,url,kind,maximum,progress):
        validate_url(self.provider,url)
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise DownloadError('文献下载已达到30分钟总时限。', code='download_time_budget_exhausted')
        accept='application/pdf' if kind=='pdf' else 'text/html'
        req=urllib.request.Request(url,headers={'Accept':accept,'User-Agent':'CampusResearchMCP/1.9.0'})
        try:
            with self.opener.open(req,timeout=min(IDLE_SECONDS,remaining)) as response:
                validate_url(self.provider,response.url)
                if response.status!=200:raise DownloadError('出版社未返回完整HTTP 200响应，未交付文件。')
                mime=response.headers.get_content_type();self.last_mime=mime
                types={'application/pdf'} if kind=='pdf' else {'text/html','application/xhtml+xml'}
                if kind=='pdf' and self.provider=='mdpi':types.update({'text/plain','application/octet-stream','binary/octet-stream'})
                if mime not in types:raise DownloadError('出版社返回的类型不符，可能为登录、权限或验证页面。')
                if response.headers.get('Content-Encoding','identity').lower() not in ('identity',''):raise DownloadError('出版社返回不支持的压缩传输。')
                length=response.headers.get('Content-Length')
                if length and (not length.isdigit() or not 0<int(length)<=maximum):raise DownloadError('出版社响应长度无效或超过大小限制。', code='file_size_limit')
                total=int(length) if length else None;size=0;blocks=[];last=0.0
                progress(0,total)
                while True:
                    if time.monotonic()>self.deadline:raise DownloadError('文献下载已达到30分钟总时限，未交付不完整文件。', code='download_time_budget_exhausted')
                    block=response.read1(min(65536,maximum+1-size))
                    if not block:break
                    size+=len(block)
                    if size>maximum:raise DownloadError('文献超过大小限制。', code='file_size_limit')
                    blocks.append(block)
                    if time.monotonic()-last>=5:progress(size,total);last=time.monotonic()
                if total is not None and total!=size:raise DownloadError('出版社文件传输不完整，未交付。')
                progress(size,total);return b''.join(blocks)
        except urllib.error.HTTPError as exc:
            code=exc.code;exc.close()
            raise DownloadError('出版社HTTP '+str(code)+'：可能为订阅权限或站点验证；未交付错误页。') from None
        except (OSError,ValueError,http.client.HTTPException):raise DownloadError('校园文件连接失败或连续60秒无数据，未交付不完整文件。') from None
    def close(self):self.cookies.clear()

def verify_pdf(raw,doi,metadata,record):
    if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-2048:]:raise DownloadError('出版社返回的不是完整PDF。')
    try:
        reader=PdfReader(io.BytesIO(raw),strict=False)
        if reader.is_encrypted:raise DownloadError('PDF已加密，无法核验。')
        pages=len(reader.pages)
        if pages<2:raise DownloadError('PDF少于2页，无法排除首页预览。')
        text='\n'.join(reader.pages[i].extract_text() or '' for i in sorted({0,min(1,pages-1),pages-1}))
    except DownloadError:raise
    except Exception:raise DownloadError('PDF结构无法解析。') from None
    if doi.lower() not in re.sub(r'[\s\u200b-\u200d\ufeff]+','',text).lower():raise DownloadError('PDF内未识别到请求DOI，无法核实文章身份。')
    first=metadata.get('citation_firstpage','');last=metadata.get('citation_lastpage','')
    expected=int(last)-int(first)+1 if first.isdigit() and last.isdigit() and int(last)>=int(first) else None
    if expected is None:
        interval=re.fullmatch(r'(\d+)\s*[-–]\s*(\d+)',record.get('page_range') or '')
        if interval and int(interval[2])>=int(interval[1]):expected=int(interval[2])-int(interval[1])+1
    if expected is not None and expected!=pages:raise DownloadError('PDF页数与出版页码不符，未交付。')
    return {'pages':pages,'doi_in_pdf_text':True,'signature_eof_verified':True,'expected_pages':expected}

def prepare_publisher_pdf(provider,doi,update):
    validate_request(provider,doi);session=Session(provider,time.monotonic()+DOWNLOAD_SECONDS)
    try:
        update(phase='metadata');record=get_record(doi)['record']
        if record['doi'].lower()!=doi.lower():raise DownloadError('Crossref返回DOI不符。')
        landing=record['resource_url']
        if landing.startswith('http://'):landing='https://'+landing[7:]
        validate_url(provider,landing,'landing');update(phase='article_page',title=record['title'])
        raw=session.fetch(landing,'page',4*1024*1024,lambda n,t:None)
        parser=CitationMetadata();parser.feed(raw.decode('utf-8','replace'));meta=parser.values
        page_doi=meta.get('citation_doi','')
        if page_doi and page_doi.lower()!=doi.lower():raise DownloadError('出版社文章页DOI与请求不符。')
        source='publisher_article_metadata'
        if page_doi.lower()==doi.lower() and meta.get('citation_pdf_url'):
            pdf_url=urllib.parse.urljoin(landing,meta['citation_pdf_url'])
        elif provider=='mdpi':
            # MDPI's public page may be an Akamai placeholder. Its Crossref-deposited
            # PDF address was independently tested; use only that declared address.
            source='crossref_registered_link';pdf_url=None
            for link in record.get('fulltext_links',[]):
                candidate=link.get('url','')
                if candidate.startswith('http://'):candidate='https://'+candidate[7:]
                try:validate_url(provider,candidate,'pdf')
                except DownloadError:continue
                pdf_url=candidate;break
            if not pdf_url:raise DownloadError('MDPI未提供可核验的登记PDF入口。')
        else:raise DownloadError('文章页未核实DOI和PDF入口，可能需要订阅或站点验证。')
        validate_url(provider,pdf_url,'pdf');update(phase='pdf_download',received_bytes=0,total_bytes=None)
        raw=session.fetch(pdf_url,'pdf',MAX_FILE,lambda n,t:update(received_bytes=n,total_bytes=t));mime=session.last_mime
        update(phase='verifying');verification=verify_pdf(raw,doi,meta,record)
        filename=provider+'-'+re.sub(r'[^A-Za-z0-9._-]','_',doi)[:160]+'.pdf'
        info,token=cache_verified_file(raw,filename,'application/pdf','pdf',doi)
        info.update(provider=provider,title=record['title'],campus_direct_route=True,api_key_used=False,personal_login_used=False,
            verification=verification,pdf_entry_source=source,page_doi_verified=bool(page_doi),upstream_content_type=mime,signed_download_url_exposed=False)
        return info,token
    finally:session.close()
