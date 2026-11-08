"""Springer Nature Meta/OA and Clarivate WoS Starter adapters; no secret logging."""
import concurrent.futures
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from article_downloads import DownloadError, MAX_FILE, cache_verified_file, validate_doi

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None

DIRECT=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
ENDPOINTS={'meta':'/meta/v2/json','openaccess':'/openaccess/json','jats':'/openaccess/jats'}

def redact(value,key):
    if isinstance(value,str): return value.replace(key,'[redacted]') if key else value
    if isinstance(value,list): return [redact(v,key) for v in value]
    if isinstance(value,dict):
        return {k:redact(v,key) for k,v in value.items() if k.lower() not in ('apikey','api_key','nextpage')}
    return value

def fetch(provider,path,params,key,xml=False):
    if not key: return {'ok':False,'error':'missing_api_key','provider':provider}
    if provider=='springer' and path in ENDPOINTS.values():
        base='https://api.springernature.com'; params={**params,'api_key':key}; headers={}
    elif provider=='wos' and re.fullmatch(r'/apis/wos-starter/v1/documents(?:/WOS%3A\d{1,25})?',path):
        base='https://api.clarivate.com'; headers={'X-ApiKey':key}
    else: raise DownloadError('不支持的上游接口。')
    headers.update({'Accept':'application/xml' if xml else 'application/json','User-Agent':'CampusResearchMCP/1.3.0'})
    request=urllib.request.Request(base+path+'?'+urllib.parse.urlencode(params),headers=headers)
    maximum=MAX_FILE if xml else 2_000_000
    try:
        with DIRECT.open(request,timeout=40) as response:
            body=response.read(maximum+1)
            mime=response.headers.get_content_type()
            if len(body)>maximum: return {'ok':False,'error':'response_size_limit','provider':provider}
            if xml:
                if mime not in ('application/xml','text/xml','application/jats+xml'):
                    return {'ok':False,'error':'expected_xml','provider':provider}
                return {'ok':True,'body':body,'http_status':response.status}
            data=json.loads(body)
            if not isinstance(data,dict): raise ValueError()
            return {'ok':True,'data':redact(data,key),'http_status':response.status}
    except urllib.error.HTTPError as exc:
        status=exc.code; exc.close()
        error={401:'invalid_api_key',403:'api_permission_denied',404:'not_found',429:'api_rate_limit'}.get(status,'upstream_http_error')
        return {'ok':False,'http_status':status,'error':error,'provider':provider}
    except (OSError,ValueError):
        return {'ok':False,'error':'upstream_connection_or_format_error','provider':provider}

def check_query(query,count,start=1):
    if not isinstance(query,str) or not query.strip() or len(query)>1500 or any(ord(c)<32 for c in query):
        raise DownloadError('检索式须为1至1500个可打印字符。')
    if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=10:
        raise DownloadError('每个来源最多返回10条结果。')
    if isinstance(start,bool) or not isinstance(start,int) or not 1<=start<=5000:
        raise DownloadError('起始位置或页码须在1至5000之间。')

def clean(value,limit=1000):
    return str(value)[:limit] if isinstance(value,(str,int,float)) else ''

def public_url(value):
    if not isinstance(value,str) or len(value)>2000: return ''
    try:
        p=urllib.parse.urlsplit(value)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password: return ''
        query=urllib.parse.parse_qsl(p.query)
        query=[(k,v) for k,v in query if k.lower() not in ('api_key','apikey','key','token','access_token')]
        return urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,urllib.parse.urlencode(query),p.fragment))
    except ValueError: return ''

def springer_record(entry,collection):
    result={'provider':'springer','collection':collection}
    for field in ('title','doi','identifier','publicationName','publicationDate','contentType','openaccess','volume','number','startingPage','endingPage','issn','eIssn'):
        if field in entry: result[field]=clean(entry[field])
    if entry.get('abstract'): result['abstract']=clean(entry['abstract'],5000)
    result['authors']=[clean(e.get('creator')) for e in entry.get('creators',[])[:30] if isinstance(e,dict)]
    result['urls']=[{'format':clean(e.get('format'),40),'url':public_url(e.get('value'))}
                    for e in entry.get('url',[])[:8] if isinstance(e,dict) and public_url(e.get('value'))]
    if result.get('doi'):result['doi_url']='https://doi.org/'+urllib.parse.quote(result['doi'],safe='/')
    return result

def search_springer(query,key,collection='meta',count=5,start=1):
    check_query(query,count,start)
    if collection not in ('meta','openaccess'): raise DownloadError('collection须为meta或openaccess。')
    reply=fetch('springer',ENDPOINTS[collection],{'q':query,'p':count,'s':start},key)
    if not reply.get('ok'): return reply
    data=reply['data']
    if not isinstance(data.get('records'),list) or not isinstance(data.get('result'),list):
        return {'ok':False,'provider':'springer','error':'unexpected_response_schema'}
    result=(data['result'] or [{}])[0]
    return {'ok':True,'provider':'springer','collection':collection,'http_status':reply['http_status'],
            'total_results':clean(result.get('total')),'start':start,
            'records':[springer_record(e,collection) for e in data['records'][:count] if isinstance(e,dict)]}

def wos_record(entry):
    identifiers=entry.get('identifiers') or {}; source=entry.get('source') or {}
    names=entry.get('names') or {};links=entry.get('links') or {}
    result={'provider':'wos','uid':clean(entry.get('uid')),'title':clean(entry.get('title')),
            'doi':clean(identifiers.get('doi')),'source':clean(source.get('sourceTitle')),
            'date':clean(source.get('publishYear')),'record_url':public_url(links.get('record')),
            'authors':[clean(a.get('displayName') or a.get('fullName')) for a in names.get('authors',[])[:30] if isinstance(a,dict)],
            'citations':[{'database':clean(c.get('db')),'count':c.get('count')} for c in entry.get('citations',[])[:10] if isinstance(c,dict) and isinstance(c.get('count'),int)],
            'abstract_available':False}
    if result['doi']: result['doi_url']='https://doi.org/'+urllib.parse.quote(result['doi'],safe='/')
    return result

def search_wos(query,key,count=5,page=1,sort='relevance'):
    check_query(query,count,page)
    if sort not in ('relevance','newest'): raise DownloadError('sort须为relevance或newest。')
    reply=fetch('wos','/apis/wos-starter/v1/documents',{'q':query,'db':'WOS','limit':count,'page':page,'sortField':'RS+D' if sort=='relevance' else 'PY+D'},key)
    if not reply.get('ok'):return reply
    data=reply['data']
    if not isinstance(data.get('hits'),list):return {'ok':False,'provider':'wos','error':'unexpected_response_schema'}
    return {'ok':True,'provider':'wos','http_status':reply['http_status'],'total_results':(data.get('metadata') or {}).get('total'),
            'page':page,'abstract_available':False,'records':[wos_record(e) for e in data['hits'][:count] if isinstance(e,dict)]}

def get_wos_record(uid,key):
    if not re.fullmatch(r'WOS:\d{1,25}',uid):raise DownloadError('请提供WOS:开头的记录UID。')
    reply=fetch('wos','/apis/wos-starter/v1/documents/'+urllib.parse.quote(uid,safe=''),{},key)
    if not reply.get('ok'):return reply
    if reply['data'].get('uid')!=uid:return {'ok':False,'error':'record_identity_mismatch','provider':'wos'}
    return {'ok':True,'http_status':reply['http_status'],'record':wos_record(reply['data'])}

def get_springer_record(doi,key,collection='meta'):
    validate_doi(doi)
    result=search_springer('doi:'+doi,key,collection,1,1)
    if not result.get('ok'):return result
    matches=[r for r in result['records'] if r.get('doi','').lower()==doi.lower()]
    return {'ok':bool(matches),'provider':'springer','collection':collection,
            **({'record':matches[0]} if matches else {'error':'doi_not_found'})}

def prepare_springer_xml(doi,key):
    validate_doi(doi)
    response=fetch('springer',ENDPOINTS['jats'],{'q':'doi:'+doi,'p':1,'s':1},key,xml=True)
    if not response.get('ok'):raise DownloadError('Springer OA全文请求失败：'+response['error'])
    raw=response['body']
    # Springer declares external article/book DTD parameter entities. Never load them.
    # Reject general entities and internal parameter entities before any expansion.
    guard=expat.ParserCreate()
    guard.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    def entity(name,is_parameter,value,base,system,public,notation):
        if not (is_parameter and system and value is None):
            raise DownloadError('XML含不支持的实体声明，未读取。')
    guard.EntityDeclHandler=entity
    guard.ExternalEntityRefHandler=lambda *args:0
    try:
        guard.Parse(raw,True)
        root=ET.fromstring(raw)
    except (expat.ExpatError,ET.ParseError):raise DownloadError('Springer未返回有效JATS XML。') from None
    local=lambda e:e.tag.rsplit('}',1)[-1]
    matches=[]
    for article in root.iter():
        if local(article)=='article':
            metas=[m for front in article if local(front)=='front' for m in front if local(m)=='article-meta']
            ids=[(e.text or '').strip().lower() for meta in metas for e in meta if local(e)=='article-id' and e.get('pub-id-type')=='doi']
        elif local(article)=='book-part':
            metas=[e for e in article if local(e)=='book-part-meta']
            ids=[(e.text or '').strip().lower() for meta in metas for e in meta if local(e)=='book-part-id']
        else:continue
        if doi.lower() in ids:matches.append(article)
    if len(matches)!=1:raise DownloadError('OA JATS中没有唯一匹配的文章或章节DOI；可能不在OA全文集合中。')
    article=matches[0]
    bodies=[e for e in article if local(e)=='body']
    if not bodies or len(''.join(bodies[0].itertext()).strip())<100:
        raise DownloadError('JATS缺少可核验的正文，未作为全文交付。')
    # API envelopes may echo apiKey/nextPage. Deliver only the matching JATS article.
    body=raw if article is root else ET.tostring(article,encoding='utf-8',xml_declaration=True)
    if key and (key.encode() in body or urllib.parse.quote(key,safe='').encode() in body):
        raise DownloadError('响应包含认证字段，已停止附件交付。')
    filename='springer-'+re.sub(r'[^A-Za-z0-9._-]','_',doi)[:160]+'.jats.xml'
    info,token=cache_verified_file(body,filename,'application/xml','xml',doi)
    info['provider']='springer';info['api_envelope_removed']=article is not root
    info['jats_document_type']=local(article)
    return info,token

def merge_results(results):
    merged={}
    for provider,response in results.items():
        if not response.get('ok'):continue
        for record in response.get('records',[]):
            doi=record.get('doi','').strip().lower()
            identity=('doi',doi) if doi else (provider,record.get('uid') or record.get('eid') or record.get('identifier') or len(merged))
            item=merged.setdefault(identity,{'doi':record.get('doi',''),'title':record.get('title',''),'sources':{}})
            item['sources'][provider]=record
    return {'ok':any(r.get('ok') for r in results.values()),'partial_failure':any(not r.get('ok') for r in results.values()),
            'deduplicated_by':'exact_doi_only','results':list(merged.values()),
            'source_status':{p:{k:v for k,v in r.items() if k!='records'} for p,r in results.items()}}

def federated_search(jobs):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures={name:pool.submit(call) for name,call in jobs.items()}
        results={}
        for name,future in futures.items():
            try:results[name]=future.result()
            except Exception:results[name]={'ok':False,'error':'source_execution_failed'}
    return merge_results(results)
