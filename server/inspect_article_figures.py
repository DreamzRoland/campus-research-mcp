import collections
import json
import sys
import xml.etree.ElementTree as ET
import urllib.parse
import urllib.request
sys.path.insert(0, '/opt/campus-scopus')
from server import secret
from article_downloads import prepare_article, DIRECT
from native_file_output import article_bytes

doi = '10.1016/j.combustflame.2026.115355'
info, token = prepare_article(doi, 'xml', secret('SCIENCEDIRECT_API_KEY'), secret('ELSEVIER_INST_TOKEN'))
root = ET.fromstring(article_bytes(info, token))
local = lambda e: e.tag.rsplit('}', 1)[-1]
print(json.dumps({'xml_bytes':info['byte_size'], 'counts':dict(collections.Counter(local(e) for e in root.iter()).most_common(35))}))
for name in ['pii', 'figure', 'link', 'object', 'attachment']:
    nodes = [e for e in root.iter() if local(e) == name]
    print(json.dumps({'tag':name, 'count':len(nodes), 'sample':[ET.tostring(e, encoding='unicode')[:6500] for e in nodes[:2]]}, ensure_ascii=False))
url = 'https://api.elsevier.com/content/object/doi/' + urllib.parse.quote(doi, safe='') + '?view=META'
headers = {'X-ELS-APIKey':secret('SCIENCEDIRECT_API_KEY'), 'Accept':'application/xml'}
if secret('ELSEVIER_INST_TOKEN'): headers['X-ELS-Insttoken'] = secret('ELSEVIER_INST_TOKEN')
try:
    with DIRECT.open(urllib.request.Request(url, headers=headers), timeout=25) as response:
        raw=response.read(2_000_001)
        meta=ET.fromstring(raw)
        from pathlib import Path
        Path('/var/lib/campus-scopus/object-meta-115355.xml').write_bytes(raw)
        print(json.dumps({'object_meta_http':response.status, 'bytes':len(raw), 'sample':ET.tostring(meta, encoding='unicode')[:13500]},ensure_ascii=False))
except Exception as exc:
    print(json.dumps({'object_meta_failed':type(exc).__name__, 'status':getattr(exc,'code',None)}))
