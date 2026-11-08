import sys,json,hashlib,urllib.request,xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,'/opt/campus-scopus')
from server import secret
from article_downloads import DIRECT,prepare_article
from native_file_output import article_bytes
local=lambda e:e.tag.rsplit('}',1)[-1]
root=ET.parse('/var/lib/campus-scopus/object-meta-115355.xml').getroot()
attachment=next(e for e in root if local(e)=='attachment' and any(local(c)=='type' and c.text=='IMAGE-HIGH-RES' for c in e))
fields={local(c):c.text for c in attachment}
headers={'X-ELS-APIKey':secret('SCIENCEDIRECT_API_KEY'),'Accept':fields['mimetype']}
if secret('ELSEVIER_INST_TOKEN'):headers['X-ELS-Insttoken']=secret('ELSEVIER_INST_TOKEN')
try:
    with DIRECT.open(urllib.request.Request(fields['url'],headers=headers),timeout=25) as response:
        raw=response.read(8*1024*1024+1)
        print(json.dumps({'figure':fields['ref'],'http':response.status,'mime':response.headers.get('Content-Type'),'bytes':len(raw),'expected_bytes':int(fields['size']),'sha256':hashlib.sha256(raw).hexdigest(),'jpeg_magic':raw[:3]==b'\xff\xd8\xff','jpeg_end':raw[-2:]==b'\xff\xd9'}))
        Path('/var/lib/campus-scopus/probe-original-figure.jpg').write_bytes(raw)
except Exception as exc:print(json.dumps({'error':type(exc).__name__,'status':getattr(exc,'code',None)}))
info,token=prepare_article('10.1016/j.combustflame.2026.115355','xml',secret('SCIENCEDIRECT_API_KEY'),secret('ELSEVIER_INST_TOKEN'))
raw=article_bytes(info,token)
Path('/var/lib/campus-scopus/figure-source-115355.xml').write_bytes(raw)
xml=ET.fromstring(raw)
for name in ['originalText','doc','article','head','body','tail','abstract','section','bib-reference','author-group']:
    nodes=[e for e in xml.iter() if local(e)==name]
    print(json.dumps({'tag':name,'count':len(nodes),'children':[local(c) for c in nodes[0]][:25] if nodes else [],'sample':ET.tostring(nodes[0],encoding='unicode')[:1100] if nodes and name in ['head','bib-reference','author-group'] else None},ensure_ascii=False))
