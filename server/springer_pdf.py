"""Campus-authorized Springer/Nature PDFs via fresh anonymous publisher sessions."""
import http.client
import http.cookiejar
import io
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pypdf import PdfReader
from article_downloads import DownloadError, MAX_FILE, cache_verified_file, validate_doi
from crossref_cambridge import get_record, plain_abstract

PUBLISHERS={'link.springer.com','link.springernature.com','www.nature.com','nature.com'}
IDENTITY={'idp.springer.com','idp.nature.com'}
_slots=threading.BoundedSemaphore(2)

def validate_url(url,role='redirect'):
    try:
        if not isinstance(url,str) or len(url)>16000 or any(ord(c)<33 for c in url) or '\\' in url:raise ValueError()
        p=urllib.parse.urlsplit(url)
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443) or p.fragment:raise ValueError()
        path=urllib.parse.unquote(p.path)
        if '\\' in path or any(part in ('.','..') for part in path.split('/')):raise ValueError()
        if p.hostname in IDENTITY:
            if role!='redirect' or p.path not in ('/authorize','/transit'):raise ValueError()
        elif p.hostname in ('link.springer.com','link.springernature.com'):
            if not p.path.startswith(('/article/','/content/pdf/','/10.')):raise ValueError()
            if role=='pdf' and not p.path.startswith('/content/pdf/'):raise ValueError()
        elif p.hostname in ('www.nature.com','nature.com'):
            if not p.path.startswith('/articles/'):raise ValueError()
            if role=='pdf' and not p.path.lower().endswith('.pdf'):raise ValueError()
        else:raise ValueError()
    except ValueError:raise DownloadError('只允许Springer/Nature官方文章、PDF及已核验的匿名机构识别跳转。', code='unregistered_or_unsafe_fulltext_location') from None
    return url

def public_url(url):
    p=urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,'',''))

class PublisherCookies(http.cookiejar.DefaultCookiePolicy):
    def set_ok(self,cookie,request):
        domain=cookie.domain.lstrip('.')
        return (domain in PUBLISHERS|IDENTITY|{'springer.com','springernature.com'}
                and urllib.parse.urlsplit(request.full_url).scheme=='https'
                and super().set_ok(cookie,request))

class PublisherRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections=6
    def __init__(self,deadline):self.deadline=deadline
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if time.monotonic()>self.deadline:raise DownloadError('Springer/Nature下载超时。', code='download_time_budget_exhausted')
        validate_url(newurl)
        if code not in (301,302,303,307,308):raise DownloadError('Springer/Nature返回不支持的跳转。', code='unregistered_or_unsafe_fulltext_location')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

class PublisherSession:
    def __init__(self,deadline):
        self.deadline=deadline
        self.cookies=http.cookiejar.CookieJar(policy=PublisherCookies())
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(self.cookies),PublisherRedirect(deadline))

    def fetch(self,url,accept,maximum):
        validate_url(url)
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise DownloadError('Springer/Nature下载超时。', code='download_time_budget_exhausted')
        req=urllib.request.Request(url,headers={'Accept':accept,'User-Agent':'CampusResearchMCP/1.7.0'})
        try:
            with self.opener.open(req,timeout=min(30,remaining)) as response:
                validate_url(response.url)
                if response.status!=200:raise DownloadError('Springer/Nature未返回完整HTTP 200响应。')
                if response.headers.get('Content-Encoding','identity').lower() not in ('','identity'):
                    raise DownloadError('Springer/Nature返回不支持的压缩传输。')
                length=response.headers.get('Content-Length')
                if length and (not length.isdigit() or int(length)>maximum):raise DownloadError('Springer/Nature响应过大或长度无效。', code='file_size_limit')
                size=0;blocks=[]
                while True:
                    if time.monotonic()>self.deadline:raise DownloadError('Springer/Nature下载超时。', code='download_time_budget_exhausted')
                    block=response.read(min(1024*1024,maximum+1-size))
                    if not block:break
                    size+=len(block)
                    if size>maximum:raise DownloadError('Springer/Nature文件超过大小限制。', code='file_size_limit')
                    blocks.append(block)
                if length and int(length)!=size:raise DownloadError('Springer/Nature文件传输不完整。')
                return b''.join(blocks),response.headers.get_content_type(),response.url
        except urllib.error.HTTPError as exc:
            status=exc.code;exc.close()
            raise DownloadError('Springer/Nature HTTP '+str(status)+'：可能为订阅授权、站点验证或服务限制，未交付错误页。') from None
        except (OSError,ValueError,http.client.HTTPException):raise DownloadError('Springer/Nature校园直连网络、TLS或传输失败。') from None

    def close(self):self.cookies.clear()

class CitationMetadata(HTMLParser):
    def __init__(self):super().__init__();self.values={}
    def handle_starttag(self,tag,attrs):
        a=dict(attrs);name=a.get('name','').lower()
        if tag=='meta' and name in ('citation_doi','citation_pdf_url','citation_title','citation_firstpage','citation_lastpage'):
            self.values[name]=a.get('content','')

def verify_pdf(raw,record,metadata,host):
    if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-2048:]:raise DownloadError('Springer/Nature未返回完整PDF，未交付登录页或预览页。')
    try:
        reader=PdfReader(io.BytesIO(raw),strict=False)
        if reader.is_encrypted:raise DownloadError('Springer/Nature PDF已加密，无法核验。')
        pages=len(reader.pages)
        if pages<2:raise DownloadError('Springer/Nature PDF少于2页，无法排除单页预览。')
        sample='\n'.join(reader.pages[i].extract_text() or '' for i in sorted({0,min(1,pages-1),pages-1}))
    except DownloadError:raise
    except Exception:raise DownloadError('Springer/Nature PDF结构无法解析。') from None
    compact=re.sub(r'[\s\u200b-\u200d\ufeff]+','',sample).lower()
    if record['doi'].lower() not in compact:raise DownloadError('PDF中未识别到请求DOI，无法确认文章身份。')
    norm=lambda text:re.sub(r'[^a-z0-9]','',text.lower())
    title=norm(plain_abstract(record.get('title','')))
    title_match=bool(title) and title in norm(sample)
    first=metadata.get('citation_firstpage','');last=metadata.get('citation_lastpage','')
    interval=re.fullmatch(r'(\d+)\s*[-–]\s*(\d+)',record.get('page_range',''))
    expected=(int(last)-int(first)+1 if first.isdigit() and last.isdigit() and int(last)>=int(first)
              else int(interval[2])-int(interval[1])+1 if interval and int(interval[2])>=int(interval[1]) else None)
    # Nature's original PDF may include Methods beyond the main article page range.
    nature=host in ('www.nature.com','nature.com')
    if expected is not None and (pages<expected or (not nature and pages!=expected)):
        raise DownloadError('PDF页数与出版社文章页码不符，未作为完整文章交付。')
    return {'pages':pages,'doi_in_pdf_text':True,'title_match':title_match,'signature_eof_verified':True,
            'publisher_expected_main_pages':expected,'pdf_includes_extra_pages':pages>expected if expected else None,
            'identity_method':'doi_text','full_pdf_verified':True}

def prepare_springer_pdf(doi):
    validate_doi(doi)
    if not _slots.acquire(blocking=False):raise DownloadError('已有两个Springer/Nature PDF下载任务，请稍后重试。')
    session=None
    try:
        record=get_record(doi)['record']
        landing=record['resource_url']
        if landing.startswith('http://'):landing='https://'+landing[7:]
        validate_url(landing,'landing')
        session=PublisherSession(time.monotonic()+100)
        raw,mime,final_page=session.fetch(landing,'text/html',4*1024*1024)
        validate_url(final_page,'landing')
        if mime!='text/html':raise DownloadError('Springer/Nature文章页未返回可核对的HTML。')
        parser=CitationMetadata();parser.feed(raw.decode('utf-8','replace'));metadata=parser.values
        if metadata.get('citation_doi','').lower()!=doi.lower():raise DownloadError('文章页DOI不匹配或尚未通过机构识别，无法下载。')
        pdf_url=urllib.parse.urljoin(final_page,metadata.get('citation_pdf_url',''))
        if not metadata.get('citation_pdf_url'):raise DownloadError('文章页未提供PDF入口，可能需要机构订阅。')
        validate_url(pdf_url,'pdf')
        body,mime,final_pdf=session.fetch(pdf_url,'application/pdf',MAX_FILE)
        validate_url(final_pdf,'pdf')
        if mime!='application/pdf':raise DownloadError('出版社返回的不是PDF，可能是登录、权限提示或站点验证页面。')
        verification=verify_pdf(body,record,metadata,urllib.parse.urlsplit(final_pdf).hostname)
        filename='springer-'+re.sub(r'[^A-Za-z0-9._-]','_',doi)[:160]+'.pdf'
        info,token=cache_verified_file(body,filename,'application/pdf','pdf',doi)
        info.update({'provider':'springer_nature','title':record['title'],'publisher_url':public_url(final_page),
                     'pdf_url':public_url(final_pdf),'campus_direct_route':True,'api_key_used':False,
                     'personal_login_used':False,'user_cookies_loaded':False,'anonymous_publisher_session':True,
                     'verification':verification})
        return info,token
    finally:
        if session:session.close()
        _slots.release()
