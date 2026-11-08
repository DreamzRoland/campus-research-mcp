"""Original high-resolution figures and offline HTML over the existing private MCP."""
import base64
import concurrent.futures
import hashlib
import html
import io
import json
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from article_downloads import DIRECT, MAX_FILE, DownloadError, cache_generated_file, prepare_article
from native_file_output import article_bytes

MAX_IMAGE = 8 * 1024 * 1024
MAX_FIGURES = 30
MAX_IMAGES_TOTAL = 40 * 1024 * 1024
_slots = threading.BoundedSemaphore(2)
MATH = 'http://www.w3.org/1998/Math/MathML'

def local(node): return node.tag.rsplit('}', 1)[-1]
def text(node): return ''.join(node.itertext()).strip() if node is not None else ''
def children(node): return {local(c):text(c) for c in node}
def parse_xml(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise DownloadError('不支持含实体声明的 XML。')
    try: return ET.fromstring(raw)
    except ET.ParseError: raise DownloadError('上游 XML 格式无效。') from None

def get_api(url, key, institutional_token, accept, limit, deadline):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'api.elsevier.com'
            or not parsed.path.startswith('/content/object/') or parsed.fragment
            or any(k.lower() in ('apikey', 'insttoken', 'access_token')
                   for k, _ in urllib.parse.parse_qsl(parsed.query))):
        raise DownloadError('图片元数据包含非官方或不支持的下载地址。')
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise DownloadError('原图下载超过时限。')
    headers = {'X-ELS-APIKey':key, 'Accept':accept}
    if institutional_token: headers['X-ELS-Insttoken'] = institutional_token
    try:
        with DIRECT.open(urllib.request.Request(url, headers=headers), timeout=min(20, remaining)) as response:
            length = response.headers.get('Content-Length', '')
            if length.isdigit() and int(length) > limit: raise DownloadError('原图响应超过大小上限。')
            blocks=[]; size=0
            while True:
                if time.monotonic() > deadline: raise DownloadError('原图下载超过时限。')
                block=response.read(min(65536, limit+1-size))
                if not block: break
                size += len(block)
                if size > limit: raise DownloadError('原图响应超过大小上限。')
                blocks.append(block)
            if length.isdigit() and size != int(length): raise DownloadError('原图响应未完整下载。')
            return b''.join(blocks), response.headers.get('Content-Type','').split(';')[0].lower()
    except urllib.error.HTTPError as exc:
        raise DownloadError(f'原图 API 返回 HTTP {exc.code}，未用缩略图代替。') from None
    except (OSError, ValueError):
        raise DownloadError('校园主机连接原图 API 失败。') from None

def figure_refs(root):
    figures=[e for e in root.iter() if local(e)=='figure']
    refs=[]
    for figure in figures:
        links=[e.attrib.get('locator','') for e in figure.iter() if local(e)=='link']
        if not links: raise DownloadError('XML 图形缺少原图引用。')
        for ref in links:
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', ref):
                raise DownloadError('XML 图片引用格式无效。')
            if ref not in refs: refs.append(ref)
    if len(refs) > MAX_FIGURES: raise DownloadError('单篇图片数量超过30张上限。')
    return refs

def select_highres(metadata, refs, doi):
    found_doi=next((text(e) for e in metadata.iter() if local(e)=='doi'), None)
    if found_doi != doi: raise DownloadError('图片元数据与请求 DOI 不一致。')
    candidates={}
    for item in metadata:
        if local(item)!='attachment': continue
        fields=children(item)
        if fields.get('type')!='IMAGE-HIGH-RES' or fields.get('ref') not in refs: continue
        try:
            record={k:fields[k] for k in ['ref','filename','mimetype','url']}
            record.update({k:int(fields[k]) for k in ['size','width','height']})
        except (KeyError, ValueError): raise DownloadError('高清原图元数据不完整。') from None
        if (record['mimetype'] not in ('image/jpeg','image/png','image/gif')
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}', record['filename'])
                or not 0 < record['size'] <= MAX_IMAGE
                or not 0 < record['width'] <= 30000 or not 0 < record['height'] <= 30000):
            raise DownloadError('高清原图类型、尺寸或大小不受支持。')
        suffixes={'image/jpeg':('.jpg','.jpeg'),'image/png':('.png',),'image/gif':('.gif',)}
        if not record['filename'].lower().endswith(suffixes[record['mimetype']]):
            raise DownloadError('原图扩展名与媒体类型不一致。')
        ref=record['ref']; previous=candidates.get(ref)
        if previous is None or record['width']*record['height'] > previous['width']*previous['height']:
            candidates[ref]=record
    missing=[r for r in refs if r not in candidates]
    if missing: raise DownloadError('缺少高清原图：' + ', '.join(missing) + '；未生成不完整离线包。')
    selected=[candidates[r] for r in refs]
    if len({r['filename'] for r in selected})!=len(selected): raise DownloadError('原图文件名重复。')
    if sum(r['size'] for r in selected)>MAX_IMAGES_TOTAL: raise DownloadError('原图合计超过40 MiB上限。')
    return selected

def image_dimensions(raw, mime):
    if mime=='image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n') and len(raw)>=33 and raw[-8:]==b'IEND\xaeB`\x82':
        return struct.unpack('>II', raw[16:24])
    if mime=='image/gif' and raw[:6] in (b'GIF87a',b'GIF89a') and len(raw)>=14 and raw.endswith(b';'):
        return struct.unpack('<HH', raw[6:10])
    if mime=='image/jpeg' and raw.startswith(b'\xff\xd8\xff') and raw.endswith(b'\xff\xd9'):
        pos=2
        while pos+4<=len(raw):
            if raw[pos]!=255: break
            while pos<len(raw) and raw[pos]==255: pos+=1
            if pos>=len(raw): break
            marker=raw[pos]; pos+=1
            if marker in (0xd8,0xd9) or 0xd0<=marker<=0xd7: continue
            if pos+2>len(raw): break
            length=int.from_bytes(raw[pos:pos+2],'big')
            if length<2 or pos+length>len(raw): break
            if marker in (0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf) and length>=8:
                height,width=struct.unpack('>HH',raw[pos+3:pos+7]); return width,height
            if marker==0xda: break
            pos+=length
    raise DownloadError('原图文件签名或结构无效，未把错误页当作图片。')

def download_figure(record, key, institutional_token, deadline):
    raw,mime=get_api(record['url'],key,institutional_token,record['mimetype'],MAX_IMAGE,deadline)
    if mime!=record['mimetype'] or len(raw)!=record['size']:
        raise DownloadError('高清原图的格式或字节数与官方元数据不一致。')
    if image_dimensions(raw,mime)!=(record['width'],record['height']):
        raise DownloadError('高清原图像素尺寸与官方元数据不一致。')
    return {**record, 'sha256':hashlib.sha256(raw).hexdigest(), 'body':raw}

CSS='''
:root{color-scheme:light;--ink:#18202b;--muted:#536174;--line:#dce3eb;--accent:#255d79}
*{box-sizing:border-box}body{margin:0;background:#f3f5f7;color:var(--ink);font:18px/1.8 Georgia,"Times New Roman",serif}
main{max-width:1050px;margin:32px auto;background:white;padding:50px 64px;border:1px solid var(--line);border-radius:8px}
h1{font-size:2rem;line-height:1.35;margin:12px 0 20px}h2{font-size:1.5rem;line-height:1.45;margin:2.2em 0 .8em}h3,h4,h5,h6{line-height:1.5;margin:1.6em 0 .6em}
.journal,.authors,.affiliation,.meta,nav,.source{font:14px/1.65 system-ui,sans-serif;color:var(--muted)}.journal{color:var(--accent);font-weight:650;text-transform:uppercase;letter-spacing:.08em}
.authors{font-size:16px;color:var(--ink);margin:12px 0}.affiliation{margin-bottom:20px}.meta{border-top:1px solid var(--line);padding-top:14px}a{color:var(--accent);text-decoration-thickness:1px;text-underline-offset:3px}
nav{padding:18px 22px;background:#f5f8fa;border:1px solid var(--line);margin:24px 0}nav ol{margin:6px 0;padding-left:22px}.abstract{padding:22px 26px;background:#f7f8fa;border-left:3px solid #6b91a7;margin:28px 0}.abstract h2{margin:0 0 10px;font-size:1.2rem}
p{margin:1em 0}figure{margin:30px 0;padding:18px 20px;border:1px solid var(--line);border-radius:6px;break-inside:avoid}figure img{display:block;max-width:100%;height:auto;margin:auto}figcaption{font-size:15px;line-height:1.7;margin-top:16px}.figure-label{font-weight:bold}.equation{overflow-x:auto;display:flex;gap:20px;align-items:center;justify-content:center;margin:22px 0;padding:8px 0}.equation .eq-label{flex:none;color:var(--muted)}math{font-size:1.03em}math[display=block]{margin:.6em 0}table{width:100%;border-collapse:collapse;font-size:16px}th,td{border:1px solid var(--line);padding:8px;text-align:left}.reference{font-size:15px;line-height:1.65;margin:14px 0}.source{border-top:1px solid var(--line);margin-top:35px;padding-top:18px}li{margin:.4em 0}
@media(max-width:720px){body{font-size:16px}main{margin:0;padding:24px 20px;border:0;border-radius:0}h1{font-size:1.6rem}figure{padding:12px}nav{padding:12px}}
@media print{body{background:white;font-size:11pt}main{padding:0;margin:0;max-width:none;border:0}nav{display:none}h2,h3{break-after:avoid}a{color:inherit}figure img{max-height:680px;object-fit:contain}}
'''

class Renderer:
    def __init__(self, root, assets, embedded):
        self.root=root; self.assets={a['ref']:a for a in assets}; self.embedded=embedded
        self.figures={e.attrib.get('id',''):e for e in root.iter() if local(e)=='figure'}
        self.placed=set()
    def anchor(self,node):
        identity=node.attrib.get('id','')
        return ' id="'+html.escape(identity,quote=True)+'"' if re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',identity) else ''
    def inner(self,node,depth=2,place=False):
        return html.escape(node.text or '')+''.join(self.render(c,depth,place)+html.escape(c.tail or '') for c in node)
    def math(self,node):
        tags={'math','mrow','mi','mn','mo','mtext','mspace','msub','msup','msubsup','mfrac','msqrt','mroot','mover','munder','munderover','mfenced','mtable','mtr','mtd','mlabeledtr','mstyle','mpadded','mphantom','menclose','semantics','annotation','none','mprescripts','mmultiscripts'}
        tag=local(node)
        if tag not in tags: return html.escape(text(node))
        attrs=' xmlns="'+MATH+'"' if tag=='math' else ''
        for key in ['display','mathvariant','stretchy','fence','separator','separators','open','close','columnalign','rowalign','width','height','depth','accent','accentunder','linethickness','displaystyle','encoding','notation']:
            if key in node.attrib: attrs+=' '+key+'="'+html.escape(node.attrib[key],quote=True)+'"'
        return '<'+tag+attrs+'>'+html.escape(node.text or '')+''.join(self.math(c)+html.escape(c.tail or '') for c in node)+'</'+tag+'>'
    def figure(self,node):
        self.placed.add(node.attrib.get('id',''))
        images=[]
        for link in node.iter():
            if local(link)!='link': continue
            asset=self.assets[link.attrib['locator']]
            src=('data:'+asset['mimetype']+';base64,'+base64.b64encode(asset['body']).decode('ascii')) if self.embedded else 'images/'+asset['filename']
            images.append('<img src="'+src+'" alt="'+html.escape(asset['ref'])+'" width="'+str(asset['width'])+'" height="'+str(asset['height'])+'">')
        label=next((text(e) for e in node if local(e)=='label'),'')
        caption=next((e for e in node if local(e)=='caption'),None)
        return '<figure'+self.anchor(node)+'>'+''.join(images)+'<figcaption><span class="figure-label">'+html.escape(label)+'</span> '+(self.inner(caption) if caption is not None else '')+'</figcaption></figure>'
    def render(self,node,depth=2,place=False):
        tag=local(node)
        if node.tag.startswith('{'+MATH+'}'): return self.math(node)
        if tag in ('script','style','object','attachment','alt-text','contributor-role','source-text','link'): return ''
        if tag=='figure': return self.figure(node)
        if tag in ('section','abstract-sec'):
            label=next((text(c) for c in node if local(c)=='label'),'')
            title=next((c for c in node if local(c)=='section-title'),None)
            heading=''
            if title is not None: heading='<h'+str(min(depth,6))+'>'+html.escape(label)+' '+self.inner(title)+'</h'+str(min(depth,6))+'>'
            rest=''.join(self.render(c,depth+1,place) for c in node if local(c) not in ('label','section-title'))
            return '<section'+self.anchor(node)+'>'+heading+rest+'</section>'
        if tag in ('para','simple-para'):
            result='<p'+self.anchor(node)+'>'+self.inner(node,depth,False)+'</p>'
            if place:
                for refnode in node.iter():
                    if local(refnode) in ('cross-ref','cross-refs'):
                        for identity in refnode.attrib.get('refid','').split():
                            if identity in self.figures and identity not in self.placed: result+=self.figure(self.figures[identity])
            return result
        if tag in ('cross-ref','cross-refs'):
            ref=node.attrib.get('refid','').split()
            return ('<a href="#'+html.escape(ref[0],quote=True)+'">'+self.inner(node,depth)+'</a>') if ref and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',ref[0]) else self.inner(node,depth)
        if tag in ('formula','display'):
            return '<div class="equation"'+self.anchor(node)+'>'+self.inner(node,depth)+'</div>'
        if tag=='label': return '<span class="eq-label">'+self.inner(node,depth)+'</span>'
        if tag=='bib-reference': return '<div class="reference"'+self.anchor(node)+'>'+self.inner(node,depth)+'</div>'
        if tag in ('bibliography','conflict-of-interest','acknowledgment','appendices','appendix'):
            title=next((e for e in node if local(e) in ('section-title','title')) ,None)
            default={'bibliography':'References','conflict-of-interest':'Declaration of competing interest','acknowledgment':'Acknowledgments','appendices':'Appendices','appendix':'Appendix'}[tag]
            return '<section'+self.anchor(node)+'><h2>'+(self.inner(title) if title is not None else default)+'</h2>'+''.join(self.render(c,depth,place) for c in node if c is not title)+'</section>'
        mapped={'italic':'i','bold':'b','sup':'sup','inf':'sub','sub':'sub','list':'ul','list-item':'li','table':'table','row':'tr','entry':'td','thead':'thead','tbody':'tbody','table-footnote':'p','footnote':'aside','hsp':'span'}
        if tag in mapped:
            target=mapped[tag]; return '<'+target+self.anchor(node)+'>'+self.inner(node,depth,place)+'</'+target+'>'
        if tag in ('author','authors'): return '<span>'+self.inner(node,depth)+'</span> '
        if tag in ('sections','abstract-sec','bibliography-sec','table-wrap','table-group'):
            return '<div'+self.anchor(node)+'>'+self.inner(node,depth,place)+'</div>'
        return '<span'+self.anchor(node)+'>'+self.inner(node,depth,place)+'</span>' if node.attrib.get('id') else self.inner(node,depth,place)

def render_html(root, assets, doi, embedded):
    article=next((e for e in root.iter() if local(e)=='article'), None)
    if article is None: raise DownloadError('XML 中没有可重排的正文 article。')
    head=next((e for e in article if local(e)=='head'),None)
    body=next((e for e in article if local(e)=='body'),None)
    tail=next((e for e in article if local(e)=='tail'),None)
    if head is None or body is None: raise DownloadError('XML 文章结构缺少 head 或 body。')
    renderer=Renderer(root,assets,embedded)
    title=next((e for e in head if local(e)=='title'),None)
    if title is None: raise DownloadError('XML 缺少文章标题。')
    group=next((e for e in head if local(e)=='author-group'),None)
    authors=[]; affiliations=[]
    if group is not None:
        for node in group:
            if local(node)=='author': authors.append(' '.join(text(c) for c in node if local(c) in ('given-name','surname')))
            if local(node)=='affiliation':
                # Elsevier provides equivalent plain and structured representations.
                preferred=next((text(c) for kind in ('textfn','source-text','affiliation') for c in node if local(c)==kind and text(c)),text(node))
                affiliations.append(preferred)
    abstract=next((e for e in head if local(e)=='abstract'),None)
    publication=next((text(e) for e in root.iter() if local(e)=='publicationName'),'ScienceDirect')
    sections=[e for e in body.iter() if local(e)=='section']
    navigation=[]
    for section in sections:
        stitle=next((e for e in section if local(e)=='section-title'),None)
        label=next((text(e) for e in section if local(e)=='label'),'')
        if stitle is not None and section.attrib.get('id'):
            navigation.append('<li><a href="#'+html.escape(section.attrib['id'],quote=True)+'">'+html.escape(label+' '+text(stitle))+'</a></li>')
    output=['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data: \'self\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
            '<title>'+html.escape(text(title))+'</title><style>'+CSS+'</style></head><body><main>',
            '<header><div class="journal">'+html.escape(publication)+'</div><h1>'+renderer.inner(title)+'</h1>',
            '<p class="authors">'+html.escape(' · '.join(authors))+'</p><p class="affiliation">'+html.escape('; '.join(affiliations))+'</p>',
            '<p class="meta">DOI: <a href="https://doi.org/'+urllib.parse.quote(doi,safe='/')+'">'+html.escape(doi)+'</a></p></header>']
    if abstract is not None: output.append('<div class="abstract"><h2>Abstract</h2>'+renderer.inner(abstract)+'</div>')
    output.append('<nav aria-label="Contents"><strong>Contents</strong><ol>'+''.join(navigation)+'</ol></nav>')
    output.append(renderer.inner(body,2,True))
    for identity,figure in renderer.figures.items():
        if identity not in renderer.placed: output.append(renderer.figure(figure))
    if tail is not None: output.append(renderer.inner(tail))
    output.append('<footer class="source">Offline reading edition from the original article XML. Figures use the publisher’s original high-resolution image files. Mathematical expressions retain the XML MathML. Original XML and image checksums are included in the accompanying ZIP.</footer></main></body></html>')
    return ''.join(output).encode('utf-8')

def prepare_bundle(doi,key,institutional_token=''):
    if not _slots.acquire(blocking=False): raise DownloadError('已有两个离线文献包正在准备，请稍后再试。')
    try:
        deadline=time.monotonic()+110
        xml_info,xml_token=prepare_article(doi,'xml',key,institutional_token)
        xml=article_bytes(xml_info,xml_token); root=parse_xml(xml); refs=figure_refs(root)
        url='https://api.elsevier.com/content/object/doi/'+urllib.parse.quote(doi,safe='')+'?view=META'
        meta,mime=get_api(url,key,institutional_token,'application/xml',2*1024*1024,deadline)
        if mime not in ('application/xml','text/xml'): raise DownloadError('图片元数据未返回 XML。')
        selected=select_highres(parse_xml(meta),refs,doi)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(download_figure,record,key,institutional_token,deadline) for record in selected]
            assets=[future.result() for future in futures]
        manifest={'doi':doi,'xml':{k:xml_info[k] for k in ('file_name','byte_size','sha256')},
                  'figure_count':len(refs),'all_highres_downloaded':True,'image_bytes_unchanged':True,
                  'figures':[{k:v for k,v in a.items() if k!='body'} for a in assets]}
        standalone=render_html(root,assets,doi,True)
        index=render_html(root,assets,doi,False)
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            archive.writestr('article.xml',xml); archive.writestr('index.html',index)
            archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
            archive.writestr('README.txt','Open index.html for offline reading. Images in images/ are the original HIGHRES downloads, without pixel edits or recompression. SHA-256 values are recorded in manifest.json.\n')
            for a in assets: archive.writestr('images/'+a['filename'],a['body'])
        bundle=buffer.getvalue()
        if len(standalone)>MAX_FILE or len(bundle)>MAX_FILE: raise DownloadError('离线附件超过20 MiB上限。')
        basename=xml_info['file_name'].removesuffix('.xml')
        html_info,html_token=cache_generated_file(standalone,basename+'-offline.html','text/html','html',doi)
        zip_info,zip_token=cache_generated_file(bundle,basename+'-originals.zip','application/zip','zip',doi)
        return [(xml_info,xml_token),(html_info,html_token),(zip_info,zip_token)],manifest
    finally: _slots.release()
