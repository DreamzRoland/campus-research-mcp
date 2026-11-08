"""Public Crossref metadata and direct, institution-authorized Cambridge PDFs."""
import copy
import hashlib
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pypdf import PdfReader
from article_downloads import DownloadError, MAX_FILE, cache_verified_file, validate_doi
from research_sources import public_url

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None

def validate_cambridge_url(url,pdf=False):
    try:
        if not isinstance(url,str) or any(ord(c)<33 for c in url) or '\\' in url:raise ValueError()
        p=urllib.parse.urlsplit(url)
        decoded=urllib.parse.unquote(p.path)
        if '\\' in decoded or any(s in ('.','..') for s in decoded.split('/')):raise ValueError()
        if (p.scheme!='https' or p.hostname not in ('www.cambridge.org','resolve.cambridge.org')
                or p.username or p.password or p.port not in (None,443) or not p.path.startswith('/core/') or p.fragment):
            raise ValueError()
        if any(k.lower() in ('api_key','apikey','key','token','access_token','authorization') for k,v in urllib.parse.parse_qsl(p.query)):
            raise ValueError()
        if pdf and '/services/aop-cambridge-core/content/view/' not in p.path:raise ValueError()
    except ValueError:raise DownloadError('只允许Cambridge Core官方HTTPS文章及PDF地址。', code='unregistered_or_unsafe_fulltext_location') from None
    return url

class CambridgeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections=5
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_cambridge_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

CROSSREF=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
CAMBRIDGE=urllib.request.build_opener(urllib.request.ProxyHandler({}),CambridgeRedirect())
_crossref_lock=threading.Lock()
_next_request=0.0
_cache={}
_pdf_slots=threading.BoundedSemaphore(2)

def crossref_request(path,params):
    global _next_request
    if not (path=='/works' or re.fullmatch(r'/works/10\.\d{4,9}%2F[^/?#]+',path,re.I)):
        raise DownloadError('不支持的Crossref接口路径。')
    url='https://api.crossref.org'+path+('?' + urllib.parse.urlencode(params) if params else '')
    with _crossref_lock:
        now=time.monotonic()
        cached=_cache.get(url)
        if cached and cached[0]>now:return copy.deepcopy(cached[1])
        delay=_next_request-now
        if delay>5:raise DownloadError('Crossref限流退避中，请稍后重试。')
        if delay>0:time.sleep(delay)
        _next_request=time.monotonic()+1.05
        request=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'CampusResearchMCP/1.5.0'})
        try:
            with CROSSREF.open(request,timeout=30) as response:
                raw=response.read(4_000_001)
                if len(raw)>4_000_000:raise DownloadError('Crossref响应超过大小限制。')
                data=json.loads(raw)
                if data.get('status')!='ok' or not isinstance(data.get('message'),dict):
                    raise DownloadError('Crossref未返回有效元数据。')
                message=data['message']
            if len(_cache)>=128:
                oldest=min(_cache,key=lambda k:_cache[k][0]);_cache.pop(oldest,None)
            _cache[url]=(time.monotonic()+900,copy.deepcopy(message))
            return message
        except urllib.error.HTTPError as exc:
            status=exc.code
            if status==429:
                retry=exc.headers.get('Retry-After','60')
                seconds=min(300,max(1,int(retry))) if retry.isdigit() else 60
                _next_request=time.monotonic()+seconds
            exc.close()
            raise DownloadError('Crossref请求失败：HTTP '+str(status)+('，请稍后重试。' if status==429 else '。')) from None
        except (OSError,ValueError):raise DownloadError('Crossref连接或JSON格式错误。') from None

class AbstractText(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[]
    def handle_data(self,data):self.parts.append(data)
    def handle_endtag(self,tag):
        if tag.rsplit(':',1)[-1] in ('p','title','sec','abstract'):self.parts.append('\n')

def plain_abstract(raw):
    parser=AbstractText();parser.feed(raw)
    return ' '.join(''.join(parser.parts).split())

def scalar(value,limit=1000):return str(value)[:limit] if isinstance(value,(str,int,float)) else ''
def first(value):return scalar(value[0]) if isinstance(value,list) and value else ''

def normalized_record(data,abstract_limit=12000,markup=False):
    doi=scalar(data.get('DOI'),300)
    title=first(data.get('title'))
    parts=(data.get('issued') or {}).get('date-parts') or [[]]
    date='-'.join(str(n) if i==0 else str(n).zfill(2) for i,n in enumerate(parts[0][:3]))
    raw=data.get('abstract') if isinstance(data.get('abstract'),str) else ''
    # Limit input work as well as model-visible output; never invent a missing abstract.
    clipped=raw[:100000];plain=plain_abstract(clipped)
    result={'provider':'crossref','doi':doi,'title':title,'source':first(data.get('container-title')),
            'date':date,'publisher':scalar(data.get('publisher')),'type':scalar(data.get('type')),'page_range':scalar(data.get('page'),100),
            'issn':[scalar(v,20) for v in (data.get('ISSN') or [])[:8]],'crossref_member_id':scalar(data.get('member'),30),
            'doi_url':'https://doi.org/'+urllib.parse.quote(doi,safe='/'),
            'resource_url':public_url(((data.get('resource') or {}).get('primary') or {}).get('URL')),
            'authors':[(' '.join(scalar(a.get(k),200) for k in ('given','family')).strip() or scalar(a.get('name'),400)) for a in (data.get('author') or [])[:30] if isinstance(a,dict)],
            'abstract_available':bool(plain),'abstract':plain[:abstract_limit],
            'abstract_truncated':len(plain)>abstract_limit or len(raw)>len(clipped),
            'crossref_cited_by':data.get('is-referenced-by-count'),
            'licenses':[public_url(e.get('URL')) for e in (data.get('license') or [])[:8] if isinstance(e,dict) and public_url(e.get('URL'))],
            'fulltext_links':[{'url':public_url(e.get('URL')),'content_type':scalar(e.get('content-type'),100)} for e in (data.get('link') or [])[:8] if isinstance(e,dict) and public_url(e.get('URL'))]}
    if markup:
        result['abstract_markup']=raw[:24000]
        result['abstract_markup_truncated']=len(raw)>24000
    return result

def get_record(doi,include_abstract_markup=False):
    validate_doi(doi)
    data=crossref_request('/works/'+urllib.parse.quote(doi,safe=''),{})
    if scalar(data.get('DOI')).lower()!=doi.lower():raise DownloadError('Crossref返回的DOI与请求不一致。')
    return {'ok':True,'provider':'crossref','record':normalized_record(data,markup=include_abstract_markup)}

def search(query='',count=5,offset=0,issn='',has_abstract=False,from_year=0,to_year=0,member_id=0):
    if (not isinstance(query,str) or len(query)>1500 or any(ord(c)<32 for c in query)
            or (not query.strip() and not issn and not member_id)):
        raise DownloadError('请提供检索词、ISSN或Crossref成员ID；检索词最长1500字符。')
    if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=10:raise DownloadError('count须为1至10。')
    if isinstance(offset,bool) or not isinstance(offset,int) or not 0<=offset<=5000:raise DownloadError('offset须为0至5000。')
    if issn and not re.fullmatch(r'\d{4}-\d{3}[\dXx]',issn):raise DownloadError('ISSN格式应为0022-1120。')
    if isinstance(member_id,bool) or not isinstance(member_id,int) or not 0<=member_id<=9999999:raise DownloadError('Crossref成员ID无效。')
    if any(isinstance(v,bool) or not isinstance(v,int) or (v and not 1000<=v<=2100) for v in (from_year,to_year)) or (from_year and to_year and from_year>to_year):
        raise DownloadError('年份范围无效。')
    params={'rows':count,'offset':offset,'select':'DOI,title,author,container-title,issued,publisher,type,ISSN,URL,abstract,link,resource,license,member,is-referenced-by-count,page'}
    if query.strip():params['query.bibliographic']=query.strip()
    filters=[]
    if issn:filters.append('issn:'+issn.upper())
    if member_id:filters.append('member:'+str(member_id))
    if has_abstract:filters.append('has-abstract:true')
    if from_year:filters.append('from-pub-date:'+str(from_year)+'-01-01')
    if to_year:filters.append('until-pub-date:'+str(to_year)+'-12-31')
    if filters:params['filter']=','.join(filters)
    data=crossref_request('/works',params)
    if not isinstance(data.get('items'),list):raise DownloadError('Crossref检索响应格式无效。')
    limit=min(6000,max(800,12000//count))
    return {'ok':True,'provider':'crossref','total_results':data.get('total-results'),'offset':offset,
            'records':[normalized_record(item,limit) for item in data['items'][:count] if isinstance(item,dict)],
            'query_scope':'bibliographic_metadata','abstract_character_limit_per_record':limit}

class CitationMetadata(HTMLParser):
    def __init__(self):super().__init__();self.values={}
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='meta' and attrs.get('name','').lower() in ('citation_doi','citation_pdf_url','citation_firstpage','citation_lastpage'):
            self.values[attrs['name'].lower()]=attrs.get('content','')

def cambridge_fetch(url,accept,maximum,deadline):
    validate_cambridge_url(url)
    remaining=deadline-time.monotonic()
    if remaining<=0:raise DownloadError('Cambridge下载超时。', code='download_time_budget_exhausted')
    request=urllib.request.Request(url,headers={'Accept':accept,'User-Agent':'CampusResearchMCP/1.5.0'})
    try:
        with CAMBRIDGE.open(request,timeout=min(25,remaining)) as response:
            validate_cambridge_url(response.url)
            length=response.headers.get('Content-Length')
            if length and length.isdigit() and int(length)>maximum:raise DownloadError('Cambridge文件超过大小限制。', code='file_size_limit')
            blocks=[];size=0
            while True:
                if time.monotonic()>deadline:raise DownloadError('Cambridge下载超时。', code='download_time_budget_exhausted')
                block=response.read(min(1024*1024,maximum+1-size))
                if not block:break
                size+=len(block)
                if size>maximum:raise DownloadError('Cambridge文件超过大小限制。', code='file_size_limit')
                blocks.append(block)
            raw=b''.join(blocks)
            if length and length.isdigit() and len(raw)!=int(length):raise DownloadError('Cambridge响应不完整。')
            return raw,response.headers.get_content_type(),response.url
    except urllib.error.HTTPError as exc:
        code=exc.code;exc.close()
        raise DownloadError('Cambridge HTTP '+str(code)+'：可能是访问授权、站点验证或服务限制；未绕过或交付错误页。') from None
    except OSError:raise DownloadError('Cambridge直连网络或TLS连接失败。') from None

def verify_pdf(raw,doi,metadata):
    if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-2048:]:raise DownloadError('返回文件不是完整PDF，未交付网页或登录页。')
    try:
        pdf=PdfReader(io.BytesIO(raw),strict=False)
        if pdf.is_encrypted:raise DownloadError('Cambridge PDF已加密，无法校验。')
        pages=len(pdf.pages)
        if pages<2:raise DownloadError('Cambridge返回的PDF少于2页，无法排除单页预览。')
        sample='\n'.join(pdf.pages[i].extract_text() or '' for i in sorted({0,min(1,pages-1),pages-1}))
    except DownloadError:raise
    except Exception:raise DownloadError('Cambridge PDF结构无法解析。') from None
    first_page=metadata.get('citation_firstpage','');last_page=metadata.get('citation_lastpage','')
    expected=int(last_page)-int(first_page)+1 if first_page.isdigit() and last_page.isdigit() and int(last_page)>=int(first_page) else None
    if expected is not None and expected!=pages:raise DownloadError('PDF页数与出版社文章页码范围不符，未宣称完整下载。')
    doi_match=doi.lower() in sample.lower()
    if not doi_match and expected is None:raise DownloadError('PDF中未识别到请求DOI，也缺少可核对的页码范围。')
    return {'pages':pages,'doi_in_pdf_text':doi_match,'expected_pages':expected,'page_range_verified':expected==pages if expected is not None else None,
            'institution_stamp':'University Town of Shenzhen' if 'University Town of Shenzhen' in ' '.join(sample.split()) else None}

def prepare_cambridge_pdf(doi):
    validate_doi(doi)
    if not _pdf_slots.acquire(blocking=False):raise DownloadError('已有两个Cambridge PDF正在下载，请稍后重试。')
    try:
        deadline=time.monotonic()+100
        record=get_record(doi)['record']
        landing=record['resource_url']
        # Use the publisher-deposited resource, never construct a guessed PDF URL.
        if landing.startswith('http://'):landing='https://'+landing[7:]
        validate_cambridge_url(landing)
        raw,mime,final=cambridge_fetch(landing,'text/html',3*1024*1024,deadline)
        if mime!='text/html':raise DownloadError('Cambridge文章入口未返回可核对的HTML。')
        parser=CitationMetadata();parser.feed(raw.decode('utf-8','replace'));metadata=parser.values
        if metadata.get('citation_doi','').lower()!=doi.lower():raise DownloadError('Cambridge文章页DOI不匹配。')
        pdf_url=urllib.parse.urljoin(final,metadata.get('citation_pdf_url',''))
        if not metadata.get('citation_pdf_url'):raise DownloadError('Cambridge文章页没有PDF下载入口，可能需要机构授权。')
        validate_cambridge_url(pdf_url,pdf=True)
        body,mime,final_pdf=cambridge_fetch(pdf_url,'application/pdf',MAX_FILE,deadline)
        if mime!='application/pdf':raise DownloadError('Cambridge未返回PDF，可能是登录或验证页面。')
        verification=verify_pdf(body,doi,metadata)
        filename='cambridge-'+re.sub(r'[^A-Za-z0-9._-]','_',doi)[:160]+'.pdf'
        info,token=cache_verified_file(body,filename,'application/pdf','pdf',doi)
        info.update({'provider':'cambridge','title':record['title'],'publisher_url':final,'pdf_url':final_pdf,
                     'campus_direct_route':True,'personal_login_used':False,'verification':verification})
        return info,token
    finally:_pdf_slots.release()
