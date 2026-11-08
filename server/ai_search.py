"""Public arXiv Atom and DBLP conference KG search; fixed hosts, bounded queries."""
import copy
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from article_downloads import DownloadError

ENDPOINTS={'arxiv':'https://export.arxiv.org/api/query','dblp':'https://sparql.dblp.org/sparql'}
NS={'a':'http://www.w3.org/2005/Atom','x':'http://arxiv.org/schemas/atom',
    'o':'http://a9.com/-/spec/opensearch/1.1/'}
MAX_RESPONSE=2*1024*1024
CONFERENCES={
 'neurips':('NeurIPS / NIPS','nips','NIPS|NeurIPS','machine_learning'),
 'icml':('ICML','icml','ICML','machine_learning'),
 'iclr':('ICLR','iclr','ICLR','machine_learning'),
 'aistats':('AISTATS','aistats','AISTATS','machine_learning'),
 'uai':('UAI','uai','UAI','machine_learning'),
 'colt':('COLT','colt','COLT','learning_theory'),
 'cvpr':('CVPR','cvpr','CVPR','computer_vision'),
 'iccv':('ICCV','iccv','ICCV','computer_vision'),
 'eccv':('ECCV','eccv','ECCV','computer_vision'),
 'acl':('ACL','acl','ACL','natural_language_processing'),
 'emnlp':('EMNLP','emnlp','EMNLP','natural_language_processing'),
 'naacl':('NAACL','naacl','NAACL|HLT-NAACL|NAACL-HLT','natural_language_processing'),
 'aaai':('AAAI','aaai','AAAI','artificial_intelligence'),
 'ijcai':('IJCAI','ijcai','IJCAI','artificial_intelligence'),
 'kdd':('KDD','kdd','KDD','data_mining'),
 'sigir':('SIGIR','sigir','SIGIR','information_retrieval'),
 'www':('The Web Conference / WWW','www','WWW|The Web Conference','web'),
 'acmmm':('ACM Multimedia','mm','ACM Multimedia|MM','multimedia'),
 'icra':('ICRA','icra','ICRA','robotics'),
 'iros':('IROS','iros','IROS','robotics'),
}
ALIASES={'nips':'neurips','acm-mm':'acmmm','acm mm':'acmmm','acm multimedia':'acmmm','thewebconf':'www'}
_locks={name:threading.Lock() for name in ENDPOINTS}
_next={name:0.0 for name in ENDPOINTS}
_cache={name:{} for name in ENDPOINTS}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None

DIRECT=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

def _request(source,params):
    if source not in ENDPOINTS:raise DownloadError('不支持的AI文献接口。')
    url=ENDPOINTS[source]+'?'+urllib.parse.urlencode(params)
    if len(url.encode())>16000:raise DownloadError('检索式过长，请缩短关键词。')
    with _locks[source]:
        now=time.monotonic();cached=_cache[source].get(url)
        if cached and cached[0]>now:return copy.deepcopy(cached[1])
        delay=_next[source]-now
        if delay>4:raise DownloadError(source+'接口退避中，请稍后重试。')
        if delay>0:time.sleep(delay)
        _next[source]=time.monotonic()+(3.1 if source=='arxiv' else 1.05)
        req=urllib.request.Request(url,headers={'Accept':'application/atom+xml' if source=='arxiv' else 'application/sparql-results+json',
            'User-Agent':'CampusResearchMCP/1.10.0'})
        try:
            with DIRECT.open(req,timeout=30) as response:
                if response.status!=200:raise DownloadError(source+'未返回HTTP 200检索结果。')
                mime=response.headers.get_content_type()
                permitted=('application/atom+xml','application/xml','text/xml') if source=='arxiv' else ('application/json','application/sparql-results+json')
                if mime not in permitted:raise DownloadError(source+'返回类型异常，可能为站点验证页。')
                raw=response.read(MAX_RESPONSE+1)
                if len(raw)>MAX_RESPONSE:raise DownloadError(source+'检索结果超过大小限制。')
            if len(_cache[source])>=128:
                _cache[source].pop(min(_cache[source],key=lambda k:_cache[source][k][0]))
            _cache[source][url]=(time.monotonic()+900,raw)
            return raw
        except urllib.error.HTTPError as exc:
            status=exc.code
            if status in (429,503):
                retry=exc.headers.get('Retry-After','60')
                _next[source]=time.monotonic()+min(300,max(5,int(retry) if retry.isdigit() else 60))
            exc.close()
            raise DownloadError(source+'请求失败：HTTP '+str(status)+'。') from None
        except (OSError,ValueError):raise DownloadError(source+'连接或响应格式异常。') from None

def _integer(value,low,high,label):
    if isinstance(value,bool) or not isinstance(value,int) or not low<=value<=high:raise DownloadError(label+'超出允许范围。')

def _query(query,allow_blank=False):
    if not isinstance(query,str) or len(query)>500 or any(ord(c)<32 for c in query) or (not query.strip() and not allow_blank):
        raise DownloadError('请提供1至500字符检索式，不能包含控制字符。')
    return query.strip()

def _clean(value,limit=2000):return ' '.join(str(value or '').split())[:limit]

def _link(value):
    if not isinstance(value,str) or len(value)>2000 or any(ord(c)<33 for c in value):return ''
    try:
        p=urllib.parse.urlsplit(value)
        if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.port not in (None,443,80):return ''
        if any(k.lower() in ('api_key','apikey','token','access_token','key') for k,_ in urllib.parse.parse_qsl(p.query)):return ''
        return urllib.parse.urlunsplit(('https',p.netloc,p.path,p.query,''))
    except ValueError:return ''

def arxiv_id(value):
    if not isinstance(value,str):raise DownloadError('请提供有效arXiv编号。')
    candidate=value.strip()
    if candidate.startswith(('https://','http://')):
        p=urllib.parse.urlsplit(candidate)
        if p.hostname not in ('arxiv.org','www.arxiv.org','export.arxiv.org') or p.username or p.password or p.query or p.fragment or p.port not in (None,443,80):
            raise DownloadError('只接受官方arXiv文章链接。')
        match=re.fullmatch(r'/(?:abs|pdf)/(.+?)(?:\.pdf)?',p.path)
        if not match:raise DownloadError('arXiv链接路径无效。')
        candidate=match[1]
    candidate=re.sub(r'^arxiv:', '',candidate,flags=re.I)
    if not re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v[1-9]\d*)?',candidate):
        raise DownloadError('arXiv编号格式无效。')
    return candidate

def _atom(raw,count):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise DownloadError('arXiv XML含不支持的实体声明。')
    try:root=ET.fromstring(raw)
    except ET.ParseError:raise DownloadError('arXiv未返回有效Atom XML。') from None
    if root.tag!='{'+NS['a']+'}feed':raise DownloadError('arXiv响应不是Atom feed。')
    records=[];limit=max(1000,12000//count)
    for entry in root.findall('a:entry',NS):
        identifier=entry.findtext('a:id','',NS)
        if '/api/errors' in identifier:raise DownloadError('arXiv拒绝该检索式或文章编号。')
        try:ident=arxiv_id(identifier)
        except DownloadError:raise DownloadError('arXiv返回无效文章标识。') from None
        abstract=_clean(entry.findtext('a:summary','',NS),100000)
        doi=_clean(entry.findtext('x:doi','',NS),300)
        if doi and not re.fullmatch(r'10\.\d{4,9}/[^\s]+',doi):doi=''
        records.append({'provider':'arxiv','identifier':ident,'arxiv_id':ident,'title':_clean(entry.findtext('a:title','',NS)),
            'authors':[_clean(e.findtext('a:name','',NS),200) for e in entry.findall('a:author',NS)][:50],
            'authors_truncated':len(entry.findall('a:author',NS))>50,'published':entry.findtext('a:published','',NS),
            'updated':entry.findtext('a:updated','',NS),'doi':doi,'abstract':abstract[:limit],
            'abstract_available':bool(abstract),'abstract_truncated':len(abstract)>limit,
            'categories':[e.get('term','') for e in entry.findall('a:category',NS)][:30],
            'primary_category':(entry.find('x:primary_category',NS).get('term','') if entry.find('x:primary_category',NS) is not None else ''),
            'journal_reference':_clean(entry.findtext('x:journal_ref','',NS)),
            'comment':_clean(entry.findtext('x:comment','',NS)),
            'abstract_url':'https://arxiv.org/abs/'+ident,'pdf_url':'https://arxiv.org/pdf/'+ident,
            'publication_status':'preprint_or_author_version','conference_acceptance_verified':False})
    try:total=int(root.findtext('o:totalResults','0',NS))
    except ValueError:raise DownloadError('arXiv总记录数无效。') from None
    return records[:count],total

def search_arxiv(query,count=5,start=0,categories='',from_year=0,to_year=0,sort_by='relevance'):
    query=_query(query);_integer(count,1,10,'count');_integer(start,0,5000,'start')
    for year in (from_year,to_year):
        if year:_integer(year,1991,2100,'年份')
        elif isinstance(year,bool) or not isinstance(year,int):raise DownloadError('年份必须为整数。')
    if from_year and to_year and from_year>to_year:raise DownloadError('年份区间无效。')
    if sort_by not in ('relevance','submittedDate','lastUpdatedDate'):raise DownloadError('sort_by无效。')
    if not isinstance(categories,str):raise DownloadError('categories应为逗号分隔字符串。')
    cats=[c.strip() for c in categories.split(',') if c.strip()]
    if len(cats)>6 or any(not re.fullmatch(r'[a-z-]+(?:\.[A-Za-z-]+)?',c) for c in cats):raise DownloadError('分类格式无效或超过6个。')
    expression='('+query+')'
    if cats:expression+=' AND ('+' OR '.join('cat:'+c for c in cats)+')'
    if from_year or to_year:expression+=' AND submittedDate:['+str(from_year or 1991)+'01010000 TO '+str(to_year or 2100)+'12312359]'
    raw=_request('arxiv',{'search_query':expression,'start':start,'max_results':count,'sortBy':sort_by,'sortOrder':'descending'})
    records,total=_atom(raw,count)
    return {'ok':True,'provider':'arxiv','query':expression,'query_scope':'arxiv_native_query','total_results':total,
        'start':start,'count':len(records),'next_start':start+len(records) if records and start+len(records)<total else None,
        'records':records,'conference_acceptance_verified':False}

def get_arxiv_record(identifier):
    ident=arxiv_id(identifier);records,_=_atom(_request('arxiv',{'id_list':ident,'max_results':1}),1)
    matching=[r for r in records if (r['arxiv_id']==ident if re.search(r'v\d+$',ident) else re.sub(r'v\d+$','',r['arxiv_id'])==ident)]
    if len(matching)!=1:return {'ok':False,'provider':'arxiv','error':'arxiv_id_not_found'}
    return {'ok':True,'provider':'arxiv','record':matching[0]}

def conference_codes(value):
    if not isinstance(value,str):raise DownloadError('conferences应为逗号分隔的会议代码。')
    if value.strip().lower()=='all':return list(CONFERENCES)
    codes=[]
    for token in value.split(','):
        code=ALIASES.get(token.strip().lower(),token.strip().lower())
        if code not in CONFERENCES:raise DownloadError('会议代码无效；请调用list_ai_conferences。')
        if code not in codes:codes.append(code)
    if not 1<=len(codes)<=6:raise DownloadError('请选择1至6个会议，或使用all。')
    return codes

def list_ai_conferences():
    return {'ok':True,'provider':'dblp','conferences':[{'code':code,'name':values[0],'area':values[3],
        'dblp_stream':'https://dblp.org/streams/conf/'+values[1],
        'coverage':'main_venue_indexed_papers; workshops/Findings excluded by venue label'} for code,values in CONFERENCES.items()],
        'notes':'Curated venue list, not a ranking. Indexing can lag; absence is not proof of rejection. No submissions/reviews/decision records are queried.'}

def _literal(value):return json.dumps(value,ensure_ascii=False)

def _terms(query):
    tokens=[a or b for a,b in re.findall(r'"([^"\n]+)"|(\S+)',query)]
    if len(tokens)>12:raise DownloadError('会议检索最多12个关键词或引号短语。')
    return tokens

def conference_query(query,conferences,count,offset,from_year,to_year):
    rows=[]
    for code in conferences:
        _,stream,label,_=CONFERENCES[code]
        pattern='^('+label+')( \\(([0-9]+|Poster|Oral|Spotlight)\\))?$'
        rows.append('(<https://dblp.org/streams/conf/'+stream+'> '+_literal(code)+' '+_literal(pattern)+')')
    filters=['FILTER(CONTAINS(LCASE(STR(?title)), LCASE('+_literal(term)+')))' for term in _terms(query)]
    if from_year:filters.append('FILTER(STR(?year) >= '+_literal(str(from_year))+')')
    if to_year:filters.append('FILTER(STR(?year) <= '+_literal(str(to_year))+')')
    return '''PREFIX dblp: <https://dblp.org/rdf/schema#>
SELECT ?paper ?title ?year ?publication_year ?conference ?stream ?venue
 (GROUP_CONCAT(DISTINCT ?author_name; separator=" | ") AS ?authors)
 (GROUP_CONCAT(DISTINCT STR(?doi); separator=" | ") AS ?dois)
 (GROUP_CONCAT(DISTINCT STR(?document); separator=" | ") AS ?documents)
WHERE {
 { SELECT DISTINCT ?paper ?title ?year ?publication_year ?conference ?stream ?venue WHERE {
  VALUES (?stream ?conference ?venue_pattern) { '''+' '.join(rows)+''' }
  ?paper a dblp:Inproceedings; dblp:publishedInStream ?stream; dblp:title ?title;
   dblp:yearOfPublication ?publication_year; dblp:publishedIn ?venue.
  OPTIONAL { ?paper dblp:yearOfEvent ?event_year }
  BIND(COALESCE(?event_year, ?publication_year) AS ?year)
  FILTER(REGEX(STR(?venue), ?venue_pattern, "i"))
  '''+'\n  '.join(filters)+'''
 } ORDER BY DESC(?year) LCASE(STR(?title)) STR(?paper) ?conference
 LIMIT '''+str(count+1)+' OFFSET '+str(offset)+''' }
 OPTIONAL { ?paper dblp:authoredBy ?creator. ?creator dblp:primaryCreatorName ?author_name }
 OPTIONAL { ?paper dblp:doi ?doi }
 OPTIONAL { ?paper dblp:primaryDocumentPage ?document }
}
GROUP BY ?paper ?title ?year ?publication_year ?conference ?stream ?venue
ORDER BY DESC(?year) LCASE(STR(?title)) STR(?paper) ?conference'''

def search_ai_conferences(query='',conferences='neurips,icml,iclr',count=5,offset=0,from_year=0,to_year=0):
    query=_query(query,allow_blank=True);codes=conference_codes(conferences)
    _integer(count,1,10,'count');_integer(offset,0,1000,'offset')
    for year in (from_year,to_year):
        if year:_integer(year,1950,2100,'年份')
        elif isinstance(year,bool) or not isinstance(year,int):raise DownloadError('年份必须为整数。')
    if from_year and to_year and from_year>to_year:raise DownloadError('年份区间无效。')
    sparql=conference_query(query,codes,count,offset,from_year,to_year)
    raw=_request('dblp',{'query':sparql,'format':'json'})
    try:
        payload=json.loads(raw);bindings=payload['results']['bindings']
        if not isinstance(bindings,list):raise ValueError()
        records=[]
        for row in bindings[:count]:
            val=lambda key:row.get(key,{}).get('value','')
            code=val('conference');ident=val('paper');stream=val('stream')
            if code not in codes or stream!='https://dblp.org/streams/conf/'+CONFERENCES[code][1] or not re.fullmatch(r'https://dblp\.org/rec/conf/[A-Za-z0-9_./-]+',ident):raise ValueError()
            _,_,labels,_=CONFERENCES[code]
            if not re.fullmatch('('+labels+r')( \(([0-9]+|Poster|Oral|Spotlight)\))?',val('venue'),re.I):raise ValueError()
            year=int(val('year'));publication_year=int(val('publication_year'))
            if (from_year and year<from_year) or (to_year and year>to_year):raise ValueError()
            dois=[_link(x) for x in val('dois').split(' | ') if x]
            doi=next((urllib.parse.unquote(urllib.parse.urlsplit(d).path.lstrip('/')) for d in dois if urllib.parse.urlsplit(d).hostname=='doi.org'),'')
            authors=[_clean(x,200) for x in val('authors').split(' | ') if x]
            records.append({'provider':'dblp','identifier':ident.removeprefix('https://dblp.org/rec/'),'title':_clean(val('title')),
                'authors':authors[:50],'authors_truncated':len(authors)>50,'author_order':'not_guaranteed',
                'year':year,'publication_year':publication_year,'conference':code,'venue':val('venue'),
                'doi':doi,'record_url':ident,'official_links':list(dict.fromkeys(x for x in (_link(s) for s in val('documents').split(' | ')) if x))[:8],
                'abstract_available':False,'abstract':'','abstract_source':'not_provided_by_dblp',
                'publication_status':'indexed_conference_paper','conference_evidence':{'source':'dblp_knowledge_graph','stream':stream,'venue':val('venue')},
                'conference_acceptance_verified':False,'main_venue_filter_applied':True})
    except (ValueError,KeyError,TypeError,AttributeError):raise DownloadError('DBLP返回无效或不匹配的会议论文记录。') from None
    return {'ok':True,'provider':'dblp','query':query,'query_scope':'title_keywords_and_phrases_AND','conferences':codes,
        'offset':offset,'count':len(records),'has_more':len(bindings)>count,'next_offset':offset+len(records) if len(bindings)>count else None,
        'total_results':None,'records':records,'sort':'year_desc_then_title','abstracts_available':False,
        'indexing_caveat':'DBLP indexing can lag; indexed venue membership is not an independently verified acceptance decision.'}
