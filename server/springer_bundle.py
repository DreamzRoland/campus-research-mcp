"""Publisher figure files and offline JATS reading editions, built on campus."""
import base64
import concurrent.futures
import hashlib
import html
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from article_bundle import CSS, Renderer, image_dimensions
from article_downloads import DownloadError, MAX_FILE, cache_generated_file
from native_file_output import article_bytes
from research_sources import prepare_springer_xml, public_url

XLINK='{http://www.w3.org/1999/xlink}href'
MAX_IMAGE=8*1024*1024
MAX_TOTAL=40*1024*1024
SLOTS=threading.BoundedSemaphore(2)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None

MEDIA=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def local(node):return node.tag.rsplit('}',1)[-1]
def text(node):return ' '.join(''.join(node.itertext()).split())

def figure_jobs(root,doi):
    kind={'article':'art','book-part':'chp'}.get(local(root))
    if not kind:raise DownloadError('不支持的Springer JATS文档类型。')
    figures=[e for e in root.iter() if local(e)=='fig']
    if len(figures)>30:raise DownloadError('文献超过30幅图，未生成完整离线包。')
    jobs={};by_figure={}
    for number,figure in enumerate(figures,1):
        graphics=[e for e in figure if local(e)=='graphic']
        for alternatives in figure:
            if local(alternatives)!='alternatives':continue
            choices=[e for e in alternatives if local(e)=='graphic']
            supported=[e for e in choices if re.fullmatch(r'MediaObjects/[A-Za-z0-9_.-]+\.(?:png|jpg|jpeg|gif)',e.get(XLINK,''),re.I)]
            if not supported:raise DownloadError('图形的候选文件格式暂不支持，未替换为截图。')
            graphics.append(next((e for e in supported if e.get('specific-use') in ('web','web-only')),supported[0]))
        if not graphics:raise DownloadError('有图形缺少可下载文件引用，未生成完整离线包。')
        references=[]
        for graphic in graphics:
            ref=graphic.get(XLINK,'')
            if not re.fullmatch(r'MediaObjects/[A-Za-z0-9_.-]+\.(?:png|jpg|jpeg|gif)',ref,re.I):
                raise DownloadError('图片引用不是受支持的出版社MediaObjects文件。')
            references.append(ref)
            if ref not in jobs:
                name=ref.split('/')[-1]
                jobs[ref]={'xml_reference':ref,'filename':name,'specific_use':graphic.get('specific-use',''),
                           'url':'https://media.springernature.com/full/springer-static/image/'+urllib.parse.quote(kind+':'+doi,safe='')+'/'+ref}
        by_figure[id(figure)]=references
    if len(jobs)>30:raise DownloadError('图片文件超过30个，未生成完整离线包。')
    return figures,list(jobs.values()),by_figure

def fetch_image(job,deadline):
    p=urllib.parse.urlsplit(job['url'])
    if (p.scheme!='https' or p.netloc!='media.springernature.com' or p.query or p.fragment
            or not p.path.startswith('/full/springer-static/image/')):
        raise DownloadError('拒绝非官方或带认证参数的媒体地址。')
    remaining=deadline-time.monotonic()
    if remaining<=0:raise DownloadError('离线包准备超时。')
    # Only public publisher media. API credentials/cookies never enter this request.
    request=urllib.request.Request(job['url'],headers={'Accept':'image/png,image/jpeg,image/gif','User-Agent':'CampusResearchMCP/1.4.0'})
    try:
        with MEDIA.open(request,timeout=min(25,remaining)) as response:
            mime=response.headers.get_content_type()
            length=response.headers.get('Content-Length')
            if length and length.isdigit() and int(length)>MAX_IMAGE:raise DownloadError('单张图片超过8MiB。')
            blocks=[];size=0
            while True:
                if time.monotonic()>deadline:raise DownloadError('离线包图片下载超时。')
                block=response.read(min(1024*1024,MAX_IMAGE+1-size))
                if not block:break
                size+=len(block)
                if size>MAX_IMAGE:raise DownloadError('单张图片超过8MiB。')
                blocks.append(block)
            raw=b''.join(blocks)
            if length and length.isdigit() and len(raw)!=int(length):raise DownloadError('图片文件未完整返回。')
            if response.status!=200:raise DownloadError('图片请求未成功。')
        expected={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.gif':'image/gif'}
        ext='.'+job['filename'].rsplit('.',1)[-1].lower()
        if mime!=expected.get(ext):raise DownloadError('图片媒体类型不匹配，未把网页当作图片。')
        width,height=image_dimensions(raw,mime)
        if width<1 or height<1:raise DownloadError('图片像素尺寸无效。')
        return {**job,'body':raw,'mimetype':mime,'byte_size':len(raw),'width':width,'height':height,'sha256':hashlib.sha256(raw).hexdigest()}
    except urllib.error.HTTPError as exc:
        code=exc.code;exc.close()
        raise DownloadError('Springer出版社图片请求失败：HTTP '+str(code)) from None
    except OSError:raise DownloadError('Springer出版社图片连接失败。') from None

class JATSRenderer:
    anchor=Renderer.anchor
    math=Renderer.math
    def __init__(self,root,assets,by_figure,embedded):
        self.root=root;self.assets={a['xml_reference']:a for a in assets};self.by_figure=by_figure
        self.embedded=embedded;self.placed=set()
    def inner(self,node,depth=2):
        return html.escape(node.text or '')+''.join(self.render(c,depth)+html.escape(c.tail or '') for c in node)
    def figure(self,node,depth):
        self.placed.add(id(node));images=[]
        for ref in self.by_figure[id(node)]:
            a=self.assets[ref]
            src='data:'+a['mimetype']+';base64,'+base64.b64encode(a['body']).decode('ascii') if self.embedded else 'images/'+a['filename']
            images.append('<img src="'+src+'" alt="'+html.escape(node.get('id') or a['filename'],quote=True)+'" width="'+str(a['width'])+'" height="'+str(a['height'])+'">')
        label=next((text(e) for e in node if local(e)=='label'),'')
        caption=next((e for e in node if local(e)=='caption'),None)
        return '<figure'+self.anchor(node)+'>'+''.join(images)+'<figcaption><strong>'+html.escape(label)+'</strong> '+(self.inner(caption,depth) if caption is not None else '')+'</figcaption></figure>'
    def render(self,node,depth=2):
        tag=local(node)
        if node.tag.startswith('{http://www.w3.org/1998/Math/MathML}'):return self.math(node)
        if tag in ('script','style','object','iframe','graphic','inline-graphic','alt-text','long-desc','custom-meta-group'):return ''
        if tag=='alternatives':
            choice=next((e for e in node if local(e)=='math'),None)
            if choice is None:choice=next((e for e in node if local(e)=='tex-math'),None)
            return self.render(choice,depth) if choice is not None else self.inner(node,depth)
        if tag=='tex-math':return '<code>'+html.escape(text(node))+'</code>'
        if tag=='fig':return self.figure(node,depth)
        if tag in ('sec','ack','app','ref-list','abstract','notes','fn-group'):
            title=next((e for e in node if local(e)=='title'),None)
            label=next((text(e) for e in node if local(e)=='label'),'')
            heading=self.inner(title,depth) if title is not None else {'ref-list':'References','abstract':'Abstract','ack':'Acknowledgments'}.get(tag,'')
            level=str(min(depth,6))
            header='<h'+level+'>'+html.escape(label)+' '+heading+'</h'+level+'>' if heading else ''
            return '<section'+self.anchor(node)+'>'+header+''.join(self.render(e,depth+1) for e in node if local(e) not in ('title','label'))+'</section>'
        if tag=='xref':
            target=node.get('rid','').split()
            return '<a href="#'+html.escape(target[0],quote=True)+'">'+self.inner(node,depth)+'</a>' if target and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',target[0]) else self.inner(node,depth)
        if tag in ('ext-link','uri'):
            url=public_url(node.get(XLINK) or text(node))
            return '<a href="'+html.escape(url,quote=True)+'">'+self.inner(node,depth)+'</a>' if url else self.inner(node,depth)
        if tag in ('name','string-name'):return html.escape(' '.join(text(e) for e in node))
        if tag in ('mixed-citation','element-citation','person-group'):
            return html.escape(node.text or '')+' '.join(self.render(c,depth)+html.escape(c.tail or '') for c in node)
        if tag=='pub-id':return ' ['+html.escape(node.get('pub-id-type','ID'))+': '+html.escape(text(node))+'] '
        if tag in ('disp-formula','disp-formula-group'):return '<div class="equation"'+self.anchor(node)+'>'+self.inner(node,depth)+'</div>'
        if tag=='ref':return '<div class="reference"'+self.anchor(node)+'>'+self.inner(node,depth)+'</div>'
        if tag=='table-wrap':return '<div class="table-wrap"'+self.anchor(node)+'>'+self.inner(node,depth)+'</div>'
        mapped={'p':'p','title':'h4','caption':'div','label':'strong','italic':'i','bold':'b','sup':'sup','sub':'sub','underline':'u',
                'table':'table','thead':'thead','tbody':'tbody','tfoot':'tfoot','tr':'tr','th':'th','td':'td','list-item':'li','fn':'aside'}
        if tag=='list':mapped['list']='ol' if node.get('list-type') in ('order','ordered','alpha-lower','roman-lower') else 'ul'
        if tag in mapped:
            target=mapped[tag];attrs=self.anchor(node)
            if tag in ('th','td'):
                for key in ('colspan','rowspan'):
                    value=node.get(key,'')
                    if value.isdigit() and 1<=int(value)<=100:attrs+=' '+key+'="'+value+'"'
            return '<'+target+attrs+'>'+self.inner(node,depth)+'</'+target+'>'
        if tag=='break':return '<br>'
        return '<div'+self.anchor(node)+'>'+self.inner(node,depth)+'</div>' if node.get('id') else self.inner(node,depth)

def render_html(root,assets,by_figure,doi,embedded):
    front=next(e for e in root if local(e) in ('front','book-part-meta'))
    meta=next((e for e in front if local(e)=='article-meta'),front)
    title=next((e for e in meta.iter() if local(e) in ('article-title','chapter-title','title')),None)
    if title is None:raise DownloadError('JATS缺少文章或章节标题。')
    renderer=JATSRenderer(root,assets,by_figure,embedded)
    authors=[]
    for contributor in meta.iter():
        if local(contributor)!='contrib' or contributor.get('contrib-type') not in ('author',None):continue
        name=next((e for e in contributor if local(e) in ('name','collab')),None)
        if name is not None:authors.append(' '.join(text(e) for e in name) if local(name)=='name' else text(name))
    abstract=next((e for e in meta if local(e)=='abstract'),None)
    journal=next((text(e) for e in front.iter() if local(e)=='journal-title'),'Springer Nature')
    permissions=next((e for e in meta if local(e)=='permissions'),None)
    body=next(e for e in root if local(e)=='body')
    back=next((e for e in root if local(e)=='back'),None)
    out=['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
         '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data: \'self\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
         '<title>'+html.escape(text(title))+'</title><style>'+CSS+'.table-wrap{overflow-x:auto;margin:28px 0}table{min-width:450px}aside{font-size:.9em}</style></head><body><main>',
         '<header><div class="journal">'+html.escape(journal)+'</div><h1>'+renderer.inner(title)+'</h1><p class="authors">'+html.escape(' · '.join(authors))+'</p>',
         '<p class="meta">DOI: <a href="https://doi.org/'+urllib.parse.quote(doi,safe='/')+'">'+html.escape(doi)+'</a></p></header>']
    if abstract is not None:out.append('<div class="abstract">'+renderer.render(abstract)+'</div>')
    out.append(renderer.inner(body))
    if back is not None:out.append(renderer.inner(back))
    for figure in root.iter():
        if local(figure)=='fig' and id(figure) not in renderer.placed:out.append(renderer.figure(figure,2))
    out.append('<footer class="source"><p>Offline reading edition from publisher JATS XML. Figure files are the publisher’s full-size published images, retained byte for byte. Author-submitted TIFF/EPS source files are not verified. MathML is preserved; no external images or scripts are needed.</p>')
    if permissions is not None:out.append('<p>'+html.escape(' '.join(text(e) for e in permissions))+'</p>')
    out.append('</footer></main></body></html>')
    return ''.join(out).encode('utf-8')

def prepare_bundle(doi,key):
    if not SLOTS.acquire(blocking=False):raise DownloadError('已有两个Springer离线包正在准备，请稍后重试。')
    try:
        deadline=time.monotonic()+110
        xml_info,xml_token=prepare_springer_xml(doi,key)
        xml=article_bytes(xml_info,xml_token);root=ET.fromstring(xml)
        figures,jobs,by_figure=figure_jobs(root,doi)
        assets=[];size=0
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(fetch_image,job,deadline) for job in jobs]
            for future in concurrent.futures.as_completed(futures):
                asset=future.result();size+=asset['byte_size']
                if size>MAX_TOTAL:raise DownloadError('图片合计超过40MiB。')
                assets.append(asset)
        order={job['xml_reference']:i for i,job in enumerate(jobs)}
        assets.sort(key=lambda a:order[a['xml_reference']])
        manifest={'doi':doi,'figure_count':len(figures),'image_count':len(assets),'all_figures_downloaded':True,
                  'image_bytes_unchanged':True,'author_originals_verified':False,
                  'image_source':'Publisher full-size published media files referenced by OA JATS',
                  'xml':{k:xml_info[k] for k in ('file_name','byte_size','sha256')},
                  'images':[{k:v for k,v in a.items() if k!='body'} for a in assets]}
        standalone=render_html(root,assets,by_figure,doi,True)
        index=render_html(root,assets,by_figure,doi,False)
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            z.writestr('article.jats.xml',xml);z.writestr('index.html',index)
            z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
            z.writestr('README.txt','Open index.html for offline reading. images/ contains unchanged publisher full-size published figure files. These are not verified author-submitted TIFF/EPS sources. manifest.json records SHA-256, dimensions and source URLs.\n')
            for asset in assets:z.writestr('images/'+asset['filename'],asset['body'])
        archive=buffer.getvalue()
        if len(standalone)>MAX_FILE or len(archive)>MAX_FILE:raise DownloadError('生成附件超过20MiB，未交付完整离线包。')
        basename=xml_info['file_name'].removesuffix('.jats.xml')
        html_info,html_token=cache_generated_file(standalone,basename+'-offline.html','text/html','html',doi)
        zip_info,zip_token=cache_generated_file(archive,basename+'-images.zip','application/zip','zip',doi)
        return [(xml_info,xml_token),(html_info,html_token),(zip_info,zip_token)],manifest
    finally:SLOTS.release()
