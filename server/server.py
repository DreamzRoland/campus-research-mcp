#!/usr/bin/env python3
"""Campus-only Elsevier client. STDIO MCP; fixed upstream and bounded outputs."""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations, CallToolResult, TextContent
from article_downloads import prepare_article, read_chunk, DownloadError
from native_file_output import article_link, read_article_resource
from article_bundle import prepare_bundle
import research_sources as research
from springer_bundle import prepare_bundle as prepare_springer_bundle
import crossref_cambridge as discovery
from wiley_downloads import prepare_wiley_pdf
from springer_pdf import prepare_springer_pdf
import aip_jobs
import publisher_jobs
import ai_search
from retrieval import engine as retrieval_engine, providers as retrieval_providers, records as retrieval_records
from retrieval import jobs as retrieval_jobs, files as retrieval_files, exports as retrieval_exports

server = MCPServer('campus-scopus', version='1.13.0', log_level='WARNING')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def secret(name):
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if directory:
        p = Path(directory) / 'elsevier.json'
        if p.is_file():
            return json.loads(p.read_text()).get(name, '')
    return os.environ.get(name, '')

def request(path, params, science=False, access=False):
    key = secret('SCIENCEDIRECT_API_KEY' if science else 'SCOPUS_API_KEY')
    if not key:
        return {'ok': False, 'error': 'missing_api_key'}
    headers = {'X-ELS-APIKey': key, 'Accept': 'application/xml' if access else 'application/json'}
    token = secret('ELSEVIER_INST_TOKEN')
    if token:
        headers['X-ELS-Insttoken'] = token
    url = 'https://api.elsevier.com' + path + '?' + urllib.parse.urlencode(params)
    try:
        with DIRECT.open(urllib.request.Request(url, headers=headers), timeout=35) as response:
            if access:
                # Do not read, store, or return the full text during an access probe.
                return {'ok': True, 'http_status': response.status,
                        'content_type': response.headers.get('Content-Type', ''),
                        'full_view_http_success': response.status == 200,
                        'fulltext_returned': False}
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                return {'ok': False, 'error': 'response_size_limit'}
            data = json.loads(raw)
            return {'ok': True, 'http_status': response.status, 'data': data}
    except urllib.error.HTTPError as exc:
        code = ''
        try:
            body = json.loads(exc.read(16384))
            err = body.get('service-error', {}).get('status', {})
            code = str(err.get('statusCode', ''))[:100]
        except (ValueError, AttributeError):
            pass
        # Never expose raw response bodies, request headers, or exception text.
        return {'ok': False, 'http_status': exc.code, 'error': code or 'upstream_http_error'}
    except (OSError, ValueError):
        return {'ok': False, 'error': 'upstream_connection_or_format_error'}

def record(entry):
    fields = {'dc:title': 'title', 'dc:creator': 'first_author', 'prism:publicationName': 'source',
              'prism:coverDate': 'date', 'prism:doi': 'doi', 'eid': 'eid',
              'citedby-count': 'cited_by', 'prism:url': 'record_url', 'prism:aggregationType': 'type'}
    out = {alias: str(entry[key])[:1000] for key, alias in fields.items()
           if entry.get(key) is not None and isinstance(entry[key], (str, int, float, bool))}
    if out.get('doi'):
        out['doi_url'] = 'https://doi.org/' + urllib.parse.quote(out['doi'], safe='/')
    return out

@server.tool(annotations=READ)
def ping() -> dict:
    """Check campus MCP health and presence of keys; returns no credentials."""
    return {'ok': True, 'service': 'campus-scopus', 'version': '1.13.0',
            'scopus_key_configured': bool(secret('SCOPUS_API_KEY')),
            'sciencedirect_key_configured': bool(secret('SCIENCEDIRECT_API_KEY')),
            'elsevier_uses_direct_campus_route': True,
            'springer_meta_key_configured': bool(secret('SPRINGER_META_API_KEY')),
            'springer_oa_key_configured': bool(secret('SPRINGER_OA_API_KEY')),
            'wos_starter_key_configured': bool(secret('WOS_API_KEY')),
            'providers':['scopus','sciencedirect','springer','wos','crossref','cambridge','wiley','aip','asme','acs','mdpi','arxiv','dblp'],
            'wos_abstracts_available':False,'springer_bundle_available':True,
            'crossref_available':True,'cambridge_pdf_available':True,
            'wiley_tdm_token_configured':bool(secret('WILEY_TDM_TOKEN')),'springer_campus_pdf_available':True,
            'aip_campus_pdf_available':True,'aip_download_deadline_seconds':1800,
            'publisher_campus_pdf_available':['asme','acs','mdpi'],'publisher_download_deadline_seconds':1800,
            'mdpi_xml_available':False,'arxiv_search_available':True,'ai_conference_search_available':True,
            'unified_search_schema':'2','thesis_search_available':True,'research_worker_required':True}

@server.tool(annotations=READ)
def search_scopus(query: str, count: int = 5, start: int = 0) -> dict:
    """Search Scopus using official query syntax. Returns up to 10 bibliographic records."""
    if not query.strip() or len(query) > 1500 or not 1 <= count <= 10 or not 0 <= start <= 5000:
        raise ToolError('Use a query of 1-1500 characters, count 1-10 and start 0-5000.')
    result = request('/content/search/scopus', {'query': query, 'count': count, 'start': start, 'view': 'STANDARD'})
    if not result.get('ok'):
        return result
    data = result['data'].get('search-results', {})
    entries = data.get('entry', [])
    return {'ok': True, 'http_status': result['http_status'], 'total_results': data.get('opensearch:totalResults'),
            'start': start, 'records': [record(x) for x in entries if isinstance(x, dict) and 'error' not in x]}

@server.tool(annotations=READ)
def get_scopus_record(eid: str) -> dict:
    """Get a Scopus bibliographic record by EID. Does not return article body."""
    if not re.fullmatch(r'2-s2\.0-\d{1,20}', eid):
        raise ToolError('Expected Scopus EID in the form 2-s2.0-123456789.')
    result = request('/content/abstract/eid/' + eid, {'view': 'META'})
    if not result.get('ok'):
        return result
    root = result['data'].get('abstracts-retrieval-response', {})
    return {'ok': True, 'http_status': result['http_status'], 'record': record(root.get('coredata', {}))}

@server.tool(annotations=READ)
def check_sciencedirect_access(doi: str) -> dict:
    """Probe the FULL XML API access status of one ScienceDirect DOI; returns no full text."""
    if not re.fullmatch(r'10\.\d{4,9}/[^\s\x00-\x1f]{1,250}', doi):
        raise ToolError('Expected a valid DOI.')
    result = request('/content/article/doi/' + urllib.parse.quote(doi, safe=''),
                     {'view': 'FULL'}, science=True, access=True)
    return {'doi': doi, **result}

WIDGET_URI = 'ui://campus-scopus/article-download-v6.html'

@server.resource('file:///campus-scopus/pdf/{download_id}/{file_name}',
                 name='article-pdf-file', mime_type='application/pdf')
def article_pdf_file(download_id: str, file_name: str) -> bytes:
    return read_article_resource(download_id, file_name, 'pdf')

@server.resource('file:///campus-scopus/xml/{download_id}/{file_name}',
                 name='article-xml-file', mime_type='application/xml')
def article_xml_file(download_id: str, file_name: str) -> bytes:
    return read_article_resource(download_id, file_name, 'xml')

@server.resource('file:///campus-scopus/html/{download_id}/{file_name}',
                 name='article-offline-html', mime_type='text/html')
def article_html_file(download_id: str, file_name: str) -> bytes:
    return read_article_resource(download_id, file_name, 'html')

@server.resource('file:///campus-scopus/zip/{download_id}/{file_name}',
                 name='article-originals-zip', mime_type='application/zip')
def article_zip_file(download_id: str, file_name: str) -> bytes:
    return read_article_resource(download_id, file_name, 'zip')

@server.resource('ui://campus-scopus/article-download-v5.html', name='article-download-v5-compat',
                 mime_type='text/html;profile=mcp-app')
@server.resource('ui://campus-scopus/article-download-v4.html', name='article-download-v4-compat',
                 mime_type='text/html;profile=mcp-app')
@server.resource('ui://campus-scopus/article-download-v3.html', name='article-download-v3-compat',
                 mime_type='text/html;profile=mcp-app')
@server.resource('ui://campus-scopus/article-download-v2.html', name='article-download-v2-compat',
                 mime_type='text/html;profile=mcp-app')
@server.resource('ui://campus-scopus/article-download-v1.html', name='article-download-compat',
                 mime_type='text/html;profile=mcp-app')
@server.resource(WIDGET_URI, name='article-download', title='文献下载',
                 mime_type='text/html;profile=mcp-app',
                 meta={'ui':{'prefersBorder':True, 'csp':{'connectDomains':[], 'resourceDomains':[]}},
                       'openai/ui':{'availableDisplayModes':['inline','fullscreen']},
                       'openai/widgetDescription':'保存授权文献到 ChatGPT Space，或提供本机下载链接。'})
def article_download_widget() -> str:
    return Path(__file__).with_name('download_widget.html').read_text(encoding='utf-8')

@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                       idempotentHint=False, openWorldHint=True),
             meta={'openai/toolInvocation/invoking':'正在获取原始文献附件…',
                   'openai/toolInvocation/invoked':'原始文献附件已返回'})
def download_article(doi: str, format: str = 'pdf', destination: str = 'auto') -> CallToolResult:
    """Download one ScienceDirect PDF/XML as an original native file attachment via MCP ResourceLink. Let ChatGPT materialize the returned resource through MCP resources/read, verify its SHA-256 in code execution, and present that original file as a native downloadable attachment; never transcribe base64 or invent a file link. The host may ask for approval to materialize a file. Native Chat attachments can be saved to Space without a Page or custom widget. destination auto/space/local all use the native attachment channel; actual persistence must be verified in Space. Rejects single-page PDF previews; multi-page validity does not prove completeness. Does not download other publishers."""
    if destination not in ('auto','space','local'):
        raise ToolError('destination 只支持 auto、space 或 local。')
    try:
        data, token = prepare_article(doi, format, secret('SCIENCEDIRECT_API_KEY'), secret('ELSEVIER_INST_TOKEN'))
    except DownloadError as exc:
        raise ToolError(str(exc)) from None
    data = {k: v for k, v in data.items() if k != 'space_receipt'}
    try:
        resource = article_link(data, token)
    except DownloadError as exc:
        raise ToolError(str(exc)) from None
    message = ('原始文献以标准 MCP 文件附件返回。请使用宿主实体化的原始文件，'
               '核验 SHA-256 后提供原生附件；不要将 file:/// 资源 URI 当作下载链接，'
               '不要抄写 base64 重建。服务端成功不代表已确认保存到 Space。')
    return CallToolResult(content=[TextContent(type='text',text=message), resource],
                          structured_content={'ok':True,'article':data,'destination':destination,
                                              'delivery':'mcp_resource_link','space_saved':False},
                          meta={'download_id':token})

@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                       idempotentHint=False, openWorldHint=True),
             meta={'openai/toolInvocation/invoking':'正在获取原图并生成离线文献…',
                   'openai/toolInvocation/invoked':'原始XML、原图及离线HTML已就绪'})
def download_article_bundle(doi: str) -> CallToolResult:
    """Download ScienceDirect XML plus ALL original HIGHRES figure files on the campus server, and build offline HTML. Use this tool when the user needs figures, an illustrated HTML version, or an offline article; ChatGPT's own network need not contact Elsevier/S3. Returns three native MCP ResourceLinks: unchanged XML, self-contained HTML with original figure bytes embedded and native MathML, and a ZIP containing article.xml, index.html, original images/ and checksum manifest.json. Never substitutes thumbnails or screenshots. Fails if any referenced high-resolution figure is missing or invalid. Materialize the original returned files and present native attachments for Space/local use. Do not re-download images or rewrite the generated HTML. Tool success alone does not verify Space persistence. Does not start a Work task."""
    try:
        files, manifest = prepare_bundle(doi, secret('SCIENCEDIRECT_API_KEY'), secret('ELSEVIER_INST_TOKEN'))
    except DownloadError as exc:
        raise ToolError(str(exc)) from None
    safe_files=[{k:v for k,v in info.items() if k!='space_receipt'} for info,_ in files]
    return CallToolResult(
        content=[TextContent(type='text',text='原始 XML、含原图的独立离线 HTML、原图 ZIP 已在校园主机准备完成。请实体化返回的原生附件；原图无需由 ChatGPT 再次联网下载。全部图形已取得 HIGHRES 原始字节，未使用缩略图或截图。保存到 Space 仍需以宿主实际结果为准。')]
                +[article_link(info,token) for info,token in files],
        structured_content={'ok':True,'doi':doi,'figure_count':manifest['figure_count'],
                            'all_highres_downloaded':True,'image_bytes_unchanged':True,
                            'delivery':'mcp_resource_link','space_saved':False,'files':safe_files})

@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                       idempotentHint=True, openWorldHint=False),
             meta={'ui':{'visibility':['app']},'openai/widgetAccessible':True,
                   'openai/visibility':'private'})
def read_article_chunk(download_id: str, offset: int = 0) -> CallToolResult:
    """Transfer a bounded file chunk to the download card only. Binary data is hidden from the model."""
    try:
        info, data = read_chunk(download_id, offset)
    except DownloadError as exc:
        raise ToolError(str(exc)) from None
    return CallToolResult(content=[TextContent(type='text',text='文件分片已传输。')],
                          structured_content=info,meta={'data_base64':data})

@server.tool(annotations=READ)
def search_springer(query: str, collection: str = 'meta', count: int = 5, start: int = 1) -> dict:
    """Search Springer Nature Meta v2 or Open Access metadata. collection='meta' or 'openaccess'. Use Springer query syntax, e.g. keyword:combustion, doi:10.1038/... or an unfielded phrase. Returns up to 10 records, abstracts if present, and publisher links; no full article body. Constraints available depend on API plan. start is one-based."""
    try:
        key=secret('SPRINGER_OA_API_KEY' if collection=='openaccess' else 'SPRINGER_META_API_KEY')
        return research.search_springer(query,key,collection,count,start)
    except DownloadError as exc: raise ToolError(str(exc)) from None

@server.tool(annotations=READ)
def get_springer_record(doi: str, collection: str = 'meta') -> dict:
    """Get Springer Nature metadata by exact DOI; choose meta or openaccess. Does not imply subscription/fulltext entitlement. For an OA full-text JATS attachment use download_springer_xml."""
    try:
        key=secret('SPRINGER_OA_API_KEY' if collection=='openaccess' else 'SPRINGER_META_API_KEY')
        return research.get_springer_record(doi,key,collection)
    except DownloadError as exc: raise ToolError(str(exc)) from None

@server.tool(annotations=READ)
def search_wos(query: str, limit: int = 5, page: int = 1, sort: str = 'relevance') -> dict:
    """Search Web of Science Core Collection via Starter API, e.g. TS=(combustion) AND PY=(2021-2026). limit 1-10; page one-based. sort relevance/newest. Returns metadata and licensed citation counts, but no abstracts or full text. One upstream request per call."""
    try:return research.search_wos(query,secret('WOS_API_KEY'),limit,page,sort)
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=READ)
def get_wos_record(uid: str) -> dict:
    """Get Web of Science Starter metadata by exact WOS: UID. Does not return abstracts or article full text."""
    try:return research.get_wos_record(uid,secret('WOS_API_KEY'))
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
def download_springer_xml(doi: str) -> CallToolResult:
    """Download only Springer Nature Open Access article/chapter full-text JATS XML as a native MCP ResourceLink. Verifies exact DOI and body; removes credential-bearing API envelope. For images or offline HTML, use download_springer_bundle instead; this XML-only tool does not fetch image bytes. Materialize the original resource as a native attachment for Space/local use; confirm host persistence before claiming Space saved. Does not launch a Work task."""
    try:info,token=research.prepare_springer_xml(doi,secret('SPRINGER_OA_API_KEY'))
    except DownloadError as exc:raise ToolError(str(exc)) from None
    return CallToolResult(content=[TextContent(type='text',text='Springer OA JATS全文已核验DOI和正文。请实体化原生XML附件；图片仅保留原文引用，尚未下载图片文件。Space保存需以宿主实际结果为准。'),article_link(info,token)],
                          structured_content={'ok':True,'provider':'springer','delivery':'mcp_resource_link','space_saved':False,'file':info})

@server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
def download_springer_bundle(doi: str) -> CallToolResult:
    """Download Springer Nature OA JATS XML AND all referenced figure files on the campus server; build offline HTML with embedded publisher images and preserved MathML/tables. Use for Springer/Nature pictures, illustrated HTML or offline article packages, instead of XML-only download_springer_xml. Returns THREE native ResourceLinks: XML, standalone HTML, and ZIP containing XML, index.html, images/ and SHA-256 manifest. ChatGPT must materialize these returned files; it need not download images from its own network or rewrite HTML. Files are the publisher's full-size published images; author-submitted TIFF/EPS originals are not verified. Any missing/invalid figure fails the complete package. Does not launch Work tasks; tool success alone does not confirm Space persistence."""
    try:files,manifest=prepare_springer_bundle(doi,secret('SPRINGER_OA_API_KEY'))
    except DownloadError as exc:raise ToolError(str(exc)) from None
    return CallToolResult(content=[TextContent(type='text',text='校园主机已下载Springer OA全文及全部图形文件，生成内嵌图片的独立HTML和ZIP。请实体化返回的三个原生附件；Chat无需再联网下载图片或重写HTML。图片为出版社发布文件，保留原始字节；作者提交的TIFF/EPS源文件未验证。Space保存需以宿主实际结果为准。')]+[article_link(info,token) for info,token in files],
                          structured_content={'ok':True,'provider':'springer','doi':doi,'figure_count':manifest['figure_count'],
                                              'image_count':manifest['image_count'],'all_figures_downloaded':True,
                                              'image_bytes_unchanged':True,'author_originals_verified':False,
                                              'delivery':'mcp_resource_link','space_saved':False,'files':[info for info,_ in files]})

@server.tool(annotations=READ)
def search_crossref(query: str = '', count: int = 5, offset: int = 0, issn: str = '', has_abstract: bool = False, from_year: int = 0, to_year: int = 0, member_id: int = 0) -> dict:
    """Search public Crossref bibliographic metadata and publisher-deposited abstracts, no API key needed. query is bibliographic text, not Scopus/WoS syntax or guaranteed full-text/abstract Boolean search. count 1-10; offset 0-5000. Filter by journal ISSN (JFM: 0022-1120), publication years, has_abstract, or Crossref member_id. Abstracts are optional; truncated abstracts are explicitly marked. Use get_crossref_record for a longer abstract or original JATS markup. Returned citation counts are Crossref-specific. For Cambridge full-text PDFs use download_cambridge_pdf."""
    try:return discovery.search(query,count,offset,issn,has_abstract,from_year,to_year,member_id)
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=READ)
def get_crossref_record(doi: str, include_abstract_markup: bool = False) -> dict:
    """Get exact-DOI Crossref metadata and publisher-deposited abstract. Returns title/authors/journal/date/publisher/licenses/links and plain-text abstract; optional original abstract_markup preserves JATS/MathML. Missing or truncated abstracts are explicit; never synthesize one. Public API needs no credentials. Metadata access does not grant fulltext rights."""
    try:return discovery.get_record(doi,include_abstract_markup)
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
def download_cambridge_pdf(doi: str) -> CallToolResult:
    """Download a Cambridge Core PDF through research-host's direct institution-authorized network. Supports OA and subscription content only when the current campus IP is authorized; no personal login, proxy or publisher API key is used. Resolves the publisher-deposited Cambridge landing page through Crossref, verifies page DOI, follows its citation_pdf_url and checks PDF MIME/signature/page structure/DOI or publisher page range, then SHA-256. Rejects login/error pages, single-page previews and non-Cambridge URLs. Returns one native MCP ResourceLink: materialize it, use original bytes and present a native attachment for Space/local use. Do not invent a download link or claim Space persistence without host confirmation. Does not launch Work, provide fulltext XML, or bypass site protections."""
    try:info,token=discovery.prepare_cambridge_pdf(doi)
    except DownloadError as exc:raise ToolError(str(exc)) from None
    return CallToolResult(content=[TextContent(type='text',text='Cambridge PDF已由校园主机直连下载，并核对DOI、PDF结构及SHA-256。请实体化原生附件并直接提供原文件。机构授权以本次校园出口的实际返回为准；Space保存需另行确认。'),article_link(info,token)],
                          structured_content={'ok':True,'provider':'cambridge','delivery':'mcp_resource_link','space_saved':False,'file':info})

@server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
def download_springer_pdf(doi: str) -> CallToolResult:
    """Download an original Springer Link OR Nature PDF through the campus IP, including non-OA subscription articles when authorized. Uses fresh anonymous publisher cookies and normal idp.springer.com/idp.nature.com redirects; no API key or personal login, no bypass of site challenges. Resolves publisher metadata through Crossref, checks article-page DOI, uses citation_pdf_url, verifies PDF structure, DOI, page range and SHA-256. Nature PDFs may include Methods beyond their main article page range; single-page/unverifiable PDFs are conservatively rejected. Returns ONE native MCP ResourceLink: materialize through resources/read and present the original PDF as a native downloadable attachment for Space/local use. Do not claim Space persistence without host confirmation. For OA XML or illustrated offline bundles use download_springer_xml/download_springer_bundle; this tool does not provide subscription XML, separate original images, or start Work."""
    try:info,token=prepare_springer_pdf(doi)
    except DownloadError as exc:raise ToolError(str(exc)) from None
    return CallToolResult(content=[TextContent(type='text',text='Springer/Nature原始PDF已由校园主机直连下载，核对文章DOI、PDF结构、可用页码及SHA-256。请实体化返回的原生附件，直接提供原文件；Space保存需以宿主实际结果为准。'),article_link(info,token)],
                          structured_content={'ok':True,'provider':'springer_nature','delivery':'mcp_resource_link','space_saved':False,'file':info})

@server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
def download_wiley_pdf(doi: str) -> CallToolResult:
    """Download an original Wiley full-text PDF via its official TDM API using the campus IP and server-side TDM token. For Wiley literature search/metadata/available abstracts reuse search_crossref and get_crossref_record. Token does not grant subscription rights: OA or current institutional access is required. Handles approved HTTPS redirects without forwarding the token and respects persistent 10-second pacing/Retry-After. Verifies publisher metadata, PDF structure, at least two pages, requested DOI text OR exact title plus publisher page range, then SHA-256; identity_method explicitly records which check passed. Returns one native MCP ResourceLink; materialize the original resource and present a native attachment for Space/local use. Never invent a file link or claim Space saved without host confirmation. No XML, separate images, supporting information, login scraping, or Work task. Single-page articles or unverifiable PDFs are conservatively rejected."""
    try:info,token=prepare_wiley_pdf(doi,secret('WILEY_TDM_TOKEN'))
    except DownloadError as exc:raise ToolError(str(exc)) from None
    return CallToolResult(content=[TextContent(type='text',text='Wiley原始PDF已由校园主机通过官方TDM API下载，核对出版社元数据、PDF结构、文章身份与SHA-256。身份核验方法见verification；请实体化返回的原生附件，直接提供原文件。Space保存需宿主另行确认。'),article_link(info,token)],
                          structured_content={'ok':True,'provider':'wiley','delivery':'mcp_resource_link','space_saved':False,'file':info})

@server.tool(annotations=READ)
def get_aip_download(job_id: str, wait_seconds: int = 20) -> CallToolResult:
    """Poll a campus AIP PDF job started by download_aip_pdf. KEEP POLLING this job until completed/failed; a pending response is NOT a failed download. Each request waits at most 20 seconds to avoid host/Tunnel timeouts, while the independent download runs for up to 30 minutes. Returns actual phase/received_bytes/total_bytes (null means unknown, do not invent a percentage). On completed, returns ONE native PDF ResourceLink; materialize via resources/read and present the original attachment for Space/local use. Tool success does not verify Space persistence. Do not start Work, recreate PDF, guess links or restart the same pending job. Download and job state are private and expire after 24h. A service restart may interrupt the worker; failed jobs explicitly report that."""
    try:data,token=aip_jobs.status(job_id,wait_seconds)
    except DownloadError as exc:raise ToolError(str(exc)) from None
    if data['status']=='completed':
        return CallToolResult(content=[TextContent(type='text',text='AIP原始PDF已由校园主机下载并核验DOI、页数和SHA-256。请实体化原生附件；Space保存以宿主实际结果为准。'),article_link(data['file'],token)],structured_content=data)
    return CallToolResult(content=[TextContent(type='text',text=json.dumps(data,ensure_ascii=False))],structured_content=data)

@server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
def download_aip_pdf(doi: str) -> dict:
    """START an AIP Publishing original PDF download (Physics of Fluids, APL, etc.) through research-host campus IP. Supports OA or actual institutional subscription access, no API key/personal login. Returns a job_id immediately: then call get_aip_download(job_id,wait_seconds=20) repeatedly UNTIL completed or failed and materialize its ResourceLink. Do not stop at queued/running or claim a download link exists yet. Independent campus worker continues between MCP calls, up to 1800 seconds (30 minutes), with 60-second network idle timeout, 64KiB streaming and normal TLS/DNS endpoint failover on official file delivery. Slow 3-minute transfers do not have to fit a single host request. Reuse active same-DOI jobs. No Work tasks, subscription XML, separate original images, proxy, challenge bypass or public file endpoint. File limit20MiB;2 concurrent jobs. Service restart can interrupt active workers; ordinary MCP call timeout does not cancel them."""
    try:return aip_jobs.start(doi)
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
def download_asme_pdf(doi: str) -> dict:
    """START an ASME Digital Collection original PDF download via research-host campus access, including non-OA when institutionally authorized. Returns job_id immediately; keep calling get_publisher_download(job_id,wait_seconds=20) UNTIL completed/failed. Slow downloads run independently for up to 30 minutes; pending is not failure. Materialize the completed native ResourceLink via resources/read and present the unchanged attachment for Space/local download. Reuse existing search_crossref/get_crossref_record for metadata and available abstracts. No API key, personal login, Work task, XML, separate figures or challenge bypass. Up to20MiB/file; ASME/ACS/MDPI share2 active jobs. Service restart can interrupt workers. Verify host persistence before claiming Space saved."""
    try:return publisher_jobs.start('asme',doi)
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
def download_acs_pdf(doi: str) -> dict:
    """START an ACS Publications original PDF download via research-host campus access, OA or actual institutional subscription. This is publisher website PDF access, not an ACS TDM API/XML entitlement. Returns job_id immediately; keep calling get_publisher_download(job_id,wait_seconds=20) UNTIL completed/failed, then materialize native ResourceLink via resources/read. Download runs independently for up to30 minutes with60-second network idle limit. Reuse search_crossref/get_crossref_record for metadata and available abstracts. Preserve original bytes for Space/local attachment; confirm host persistence before claiming saved. No API key, personal login, Work, XML, separate figures or challenge bypass. Reject single-page/unverifiable PDFs. ASME/ACS/MDPI share2 active jobs;20MiB/file. Service restart can interrupt workers."""
    try:return publisher_jobs.start('acs',doi)
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True))
def download_mdpi_pdf(doi: str) -> dict:
    """START an original MDPI open-access PDF download on research-host. Returns job_id immediately; keep calling get_publisher_download(job_id,wait_seconds=20) UNTIL completed/failed. Uses official article metadata or its exact Crossref-registered PDF entry; MDPI can label real PDFs text/plain, so verify actual PDF signature/EOF/DOI/pages/SHA-256 before attachment. Independent30-minute download,60-second network idle,20MiB/file; ASME/ACS/MDPI share2 active jobs. Materialize completed native ResourceLink unchanged for Space/local use, confirm actual host save. Reuse search_crossref/get_crossref_record. Does NOT provide MDPI XML, images or offline HTML: the XML route has not passed testing. No API key, personal login, Work or challenge bypass. Service restart can interrupt workers."""
    try:return publisher_jobs.start('mdpi',doi)
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=READ)
def get_publisher_download(job_id: str, wait_seconds: int = 20) -> CallToolResult:
    """Poll an ASME/ACS/MDPI PDF job started by download_asme_pdf, download_acs_pdf or download_mdpi_pdf. KEEP POLLING the same job until completed/failed; queued/running is not failure and not an attachment. Each call waits at most20 seconds while campus download runs up to30 minutes. Returns actual phase/received_bytes/total_bytes (null means unknown). On completed, ONE native original PDF ResourceLink is returned; materialize via resources/read, do not recreate/transcribe bytes or invent links. Native attachments support Space/local use; server space_saved=false means persistence has not been verified here. No Work. Jobs/files expire after24h; service restart may interrupt workers. AIP jobs use get_aip_download instead."""
    try:data,token=publisher_jobs.status(job_id,wait_seconds)
    except DownloadError as exc:raise ToolError(str(exc)) from None
    if data['status']=='completed':
        return CallToolResult(content=[TextContent(type='text',text='校园主机已下载出版社原始PDF并核验DOI、页数和SHA-256。请实体化此原生附件，保留原文件；Space保存以宿主实际结果为准。'),article_link(data['file'],token)],structured_content=data)
    return CallToolResult(content=[TextContent(type='text',text=json.dumps(data,ensure_ascii=False))],structured_content=data)

@server.tool(annotations=READ)
def search_arxiv(query: str, count: int = 5, start: int = 0, categories: str = '', from_year: int = 0, to_year: int = 0, sort_by: str = 'relevance') -> dict:
    """Search the official arXiv Atom API, no key. Native syntax supports ti:,au:,abs:,cat:,AND/OR and quoted phrases. Examples query='ti:"attention"', categories='cs.AI,cs.LG,stat.ML'. count1-10,start0-5000; years filter original submission date; sort_by relevance/submittedDate/lastUpdatedDate. Returns abstracts, authors, dates, categories, arXiv IDs, DOI/journal references when supplied and official PDF links. These are preprints/author versions: a journal_ref/comment is NOT independently verified conference acceptance. For indexed main-conference papers use search_ai_conferences. Rate-limited3.1s with15min process cache; errors stay explicit. Metadata only, no download/Space write."""
    try:return ai_search.search_arxiv(query,count,start,categories,from_year,to_year,sort_by)
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=READ)
def get_arxiv_record(arxiv_id: str) -> dict:
    """Fetch one official arXiv metadata record by modern/legacy ID or official abs/pdf URL, optionally versioned. Example1706.03762 or1706.03762v7. Verifies returned ID/version; returns abstract, authors, dates, categories, journal reference, DOI and PDF link. Unversioned ID resolves latest. Author-supplied conference references are not verified acceptance. No fulltext download or Space write."""
    try:return ai_search.get_arxiv_record(arxiv_id)
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=READ)
def list_ai_conferences() -> dict:
    """List20 curated AI/ML conference codes, areas and exact DBLP streams (not a ranking). Includes NeurIPS/NIPS,ICML,ICLR,AISTATS,UAI,COLT,CVPR,ICCV,ECCV,ACL,EMNLP,NAACL,AAAI,IJCAI,KDD,SIGIR,WWW,ACM MM,ICRA,IROS. Conference search filters main venue labels and excludes workshops/Findings. Indexing can lag; no submission/decision data is queried."""
    return ai_search.list_ai_conferences()

@server.tool(annotations=READ)
def search_ai_conferences(query: str = '', conferences: str = 'neurips,icml,iclr', count: int = 5, offset: int = 0, from_year: int = 0, to_year: int = 0) -> dict:
    """Search indexed AI/ML main-conference papers via official DBLP SPARQL API, no key. conferences is1-6 comma-separated curated codes or'all'; list_ai_conferences lists20. Plain title keywords use AND, quoted phrases match substrings; NOT abstract/fulltext/semantic search. Empty query browses selected venues. count1-10,offset0-1000; years use conference event year when available, publication year otherwise. Results sort year descending then title, include authors (order not guaranteed), DOI/official links when present, exact venue/stream evidence. Excludes workshop/Findings venue labels. Indexed membership does not independently verify an acceptance decision; no submissions/reviews are returned. DBLP has no abstracts; use arXiv separately for preprints/abstracts. Metadata only, no download/Space write."""
    try:return ai_search.search_ai_conferences(query,conferences,count,offset,from_year,to_year)
    except DownloadError as exc:raise ToolError(str(exc)) from None

@server.tool(annotations=READ)
def search_literature(scopus_query: str = '', wos_query: str = '', springer_query: str = '', count_per_source: int = 5, crossref_query: str = '', crossref_issn: str = '', arxiv_query: str = '', conference_query: str = '', conferences: str = 'neurips,icml,iclr', conference_year: int = 0, query: str = '', profile: str = 'general', sources: str = 'auto', filters: dict | None = None, query_variants: list[str] | None = None, continuation_id: str = '') -> dict:
    """Unified daily search: query, profile general/ai/theses/biomedical, sources auto or comma-separated registry IDs, filters from_year/to_year/language/degree/institution/conferences/track/publisher/type/sort.25s/12requests,exact-identity dedup,source-attributed RRF,original abstracts and run_id; continuation_id continues frozen source pages. Use start_research_search for explicit variants/deep pagination. Legacy source-specific query arguments retain their old response; NEVER combine with unified query/filters. Missing abstracts and source errors remain explicit; no global completeness claim."""
    native = any((scopus_query,wos_query,springer_query,crossref_query,crossref_issn,arxiv_query,conference_query))
    if query or continuation_id:
        if native:raise ToolError('统一query/continuation_id不能与旧来源检索式混用。')
        try:return retrieval_engine.search(query,profile,sources,filters,query_variants,continuation_id,count_per_source,secret)
        except ValueError as exc:raise ToolError(str(exc)) from None
    if filters is not None or query_variants is not None or profile!='general' or sources!='auto':
        raise ToolError('新增统一筛选须配合query使用；原生来源检索式的行为保持兼容。')
    if not native:raise ToolError('请提供统一query、continuation_id或至少一个原生来源检索式。')
    try:
        ai_search._integer(count_per_source,1,10,'count_per_source')
        if arxiv_query:ai_search._query(arxiv_query)
        if conference_query:
            ai_search._query(conference_query);ai_search.conference_codes(conferences)
            ai_search._integer(conference_year,0,2100,'conference_year')
            if conference_year and conference_year<1950:raise DownloadError('conference_year须为1950至2100或0。')
        for query in (scopus_query,wos_query,springer_query):
            if query:research.check_query(query,count_per_source)
    except DownloadError as exc:raise ToolError(str(exc)) from None
    jobs={}
    if scopus_query:jobs['scopus']=lambda:search_scopus(scopus_query,count_per_source,0)
    if wos_query:jobs['wos']=lambda:research.search_wos(wos_query,secret('WOS_API_KEY'),count_per_source)
    if springer_query:jobs['springer']=lambda:research.search_springer(springer_query,secret('SPRINGER_META_API_KEY'),count=count_per_source)
    if crossref_query or crossref_issn:
        def crossref_job():
            try:return discovery.search(crossref_query,count_per_source,issn=crossref_issn)
            except DownloadError as exc:return {'ok':False,'provider':'crossref','error':str(exc)}
        jobs['crossref']=crossref_job
    if arxiv_query:jobs['arxiv']=lambda:ai_search.search_arxiv(arxiv_query,count=count_per_source)
    if conference_query:jobs['dblp']=lambda:ai_search.search_ai_conferences(conference_query,conferences,count=count_per_source,from_year=conference_year,to_year=conference_year)
    return research.federated_search(jobs)

@server.resource('file:///campus-research/{file_id}/{file_name}', name='research-original-file')
def research_original_file(file_id: str, file_name: str) -> bytes:
    try:return retrieval_files.read_native(file_id,file_name)
    except ValueError as exc:raise ToolError(str(exc)) from None


@server.tool(annotations=READ)
def list_research_sources() -> dict:
    """List actual source capabilities/configuration/last verification. needs_key, needs_institutional_api, link_only and temporary failures are NOT successful empty searches. Campus web subscriptions never imply API or PDF rights. Includes Chinese/international theses and conference coverage."""
    from retrieval import scihub
    return {'ok':True,'schema_version':'2','sources':retrieval_providers.catalogue(secret),
            'download_fallbacks':[scihub.describe()]}


@server.tool(annotations=READ)
def search_theses(query: str = '', sources: str = 'auto', filters: dict | None = None, count_per_source: int = 5, query_variants: list[str] | None = None, continuation_id: str = '') -> dict:
    """Search Chinese/international master's/doctoral theses. DataCite/OpenAIRE/theses.fr/public repository index plus configured CORE/Wanfang. filters supports degree masters/doctoral/other/unknown, from_year,to_year,language,institution,include_unknown,include_in_progress. Unknown degrees are separately listed; bachelor/in-progress excluded by default. No fulltext or Space write. Use record_id with resolve_fulltext/download_literature. Daily25s/12requests, deep search via start_research_search(profile='theses')."""
    try:return retrieval_engine.search(query,'theses',sources,filters,query_variants,continuation_id,count_per_source,secret)
    except ValueError as exc:raise ToolError(str(exc)) from None


@server.tool(annotations=READ)
def get_literature_record(record_id: str = '', identifier: str = '', source: str = 'auto', enrich: bool = False) -> dict:
    """Read a stored canonical record including original abstract/provenance, or exactly retrieve DOI/arXiv/OpenAlex/Scopus EID/theses.fr NNT. enrich=true performs bounded exact lookup; never match a record by fuzzy title. Scopus FULL abstract view is attempted only with configured rights, with META fallback. Missing abstracts remain missing."""
    try:return retrieval_records.get(record_id,identifier,source,enrich,secret)
    except ValueError as exc:raise ToolError(str(exc)) from None


WRITE_RESEARCH=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True)


@server.tool(annotations=WRITE_RESEARCH)
def start_research_search(query: str, profile: str = 'general', sources: str = 'auto', filters: dict | None = None, query_variants: list[str] | None = None, citation_expansion: bool = False) -> dict:
    """Start a persistent deep literature search: profile general/ai/theses/biomedical, explicit sources or auto, filters, up to4 explicit query variants. Worker pages sources within20min/150requests/500records. Optional one-hop citations max20seeds/200edges shares budget. Returns job_id immediately; keep polling get_research_job(wait_seconds=20) until terminal. No Work, public HTTP, paid query rewriting or implicit translation. Partial result/stop reason preserved; source pages exhausted never means global completeness."""
    try:return retrieval_jobs.enqueue('search',{'plan':retrieval_engine.plan(query,profile,sources,filters,query_variants),'citation_expansion':citation_expansion})
    except ValueError as exc:raise ToolError(str(exc)) from None


def research_result(payload):
    """Resource links are separate content blocks; private legacy cache tokens aren't model fields."""
    content=[]
    result=payload.get('result',payload)
    legacy=result.get('legacy_file')
    if legacy:
        content.append(article_link(legacy['metadata'],legacy['token']))
        result['legacy_file']={'metadata':legacy['metadata']}
    info=result.get('file')
    if info:
        resource=retrieval_files.link(info)
        if resource:content.append(resource)
    for info in result.get('files',[]) + ([result['manifest']] if result.get('manifest') else []):
        resource=retrieval_files.link(info)
        if resource:content.append(resource)
    content.insert(0,TextContent(type='text',text=json.dumps(payload,ensure_ascii=False)))
    return CallToolResult(content=content,structured_content=payload)


@server.tool(annotations=READ)
def get_research_job(job_id: str, wait_seconds: int = 0) -> CallToolResult:
    """Poll the SAME persistent search/download job, wait0-20seconds. queued/running is not failure; continue polling. Completed downloads include native ResourceLink when within20MiB implementation threshold; materialize original bytes and present attachment. Larger thesis originals remain available through private SSH instructions. Never assert Space saved solely from resource creation. Search partial results have run_id for export; worker survives Tunnel restart."""
    try:return research_result(retrieval_jobs.status(job_id,wait_seconds))
    except ValueError as exc:raise ToolError(str(exc)) from None


@server.tool(annotations=WRITE_RESEARCH)
def cancel_research_job(job_id: str) -> dict:
    """Cancel a queued/running research task; preserve committed partial search records and run_id. Already completed jobs remain unchanged."""
    try:return retrieval_jobs.cancel(job_id)
    except ValueError as exc:raise ToolError(str(exc)) from None


@server.tool(annotations=READ)
def get_related_papers(record_id: str, direction: str = 'references', source: str = 'auto', count: int = 20) -> dict:
    """Get source-attributed references, citing papers or OpenAlex similar works. count1-50; direction references/citations/similar. Exact identities only, unresolved targets explicitly marked. Citation counts from different databases are never added. Semantic Scholar requires separately configured key."""
    try:return retrieval_records.related(record_id,direction,source,count,secret)
    except ValueError as exc:raise ToolError(str(exc)) from None


@server.tool(annotations=READ)
def resolve_fulltext(record_id: str) -> dict:
    """Resolve stored record to registered official repository/OA/publisher routes. Discovery is not proof of download rights. Restricted/unregistered routes return official links and reasons; no arbitrary URL fetching or login/challenge bypass."""
    try:return retrieval_files.resolve(record_id,secret)
    except ValueError as exc:raise ToolError(str(exc)) from None


@server.tool(annotations=WRITE_RESEARCH)
def download_literature(record_id: str) -> dict:
    """Start original PDF download for a STORED record_id. Independent worker up to30min/60s idle; thesis100MiB, articles20MiB; original structure/identity/hash verified, previews refused. Poll get_research_job until completed/failed. Returns native attachment for files<=20MiB; larger originals via existing private SSH/SCP. No invented PDF, CAJ conversion, public file server or Work. Space persistence requires host confirmation."""
    try:
        retrieval_files.resolve(record_id,secret)
        return retrieval_jobs.enqueue('download',{'record_id':record_id})
    except ValueError as exc:raise ToolError(str(exc)) from None


@server.tool(annotations=WRITE_RESEARCH)
def export_search_run(run_id: str, format: str = 'json') -> CallToolResult:
    """Export actual saved search results and provenance as json/csv/ris/bibtex/report. Returns native file resource; materialize original bytes. Report includes source queries, filters, budgets and stop reasons. Does not claim global coverage or confirmed Space persistence."""
    try:return research_result({'ok':True,'file':retrieval_exports.export(run_id,format),'space_saved':False})
    except ValueError as exc:raise ToolError(str(exc)) from None


@server.tool(annotations=WRITE_RESEARCH)
def import_references(content: str, format: str = 'ris') -> dict:
    """Import user-exported RIS or literal BibTeX references (<=1MiB,1-200records), e.g. CNKI/Wanfang/CQVIP/ProQuest. Keeps imported claims unverified, preserves master's/doctoral/unknown distinction. No website login, arbitrary local file read, TeX execution or automatic PDF download."""
    try:return retrieval_exports.import_refs(content,format)
    except ValueError as exc:raise ToolError(str(exc)) from None


if __name__ == '__main__':
    server.run(transport='stdio')
