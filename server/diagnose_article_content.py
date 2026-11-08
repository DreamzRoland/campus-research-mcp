"""Inspect content structure and entitlements without printing credentials or bodies."""
import collections, hashlib, json, sys, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
sys.path.insert(0, '/opt/campus-scopus')
from server import secret
from article_downloads import DIRECT
import urllib.request

def inspect(doi, entitlement=False):
    headers={'X-ELS-APIKey':secret('SCIENCEDIRECT_API_KEY'),'Accept':'application/xml'}
    token=secret('ELSEVIER_INST_TOKEN')
    if token: headers['X-ELS-Insttoken']=token
    path='/content/article/'+('entitlement/doi/' if entitlement else 'doi/')
    url='https://api.elsevier.com'+path+urllib.parse.quote(doi,safe='')
    if not entitlement: url+='?view=FULL'
    out={'doi':doi,'kind':'entitlement' if entitlement else 'full_xml'}
    try:
        with DIRECT.open(urllib.request.Request(url,headers=headers),timeout=35) as r:
            raw=r.read(20*1024*1024+1)
            out.update(http_status=r.status,byte_size=len(raw),content_type=r.headers.get('Content-Type'))
        root=ET.fromstring(raw)
        counts=collections.Counter(e.tag.rsplit('}',1)[-1] for e in root.iter())
        out['structure']={k:counts[k] for k in ['body','sections','section','bib-reference','ref','para','originalText']}
        fields={'entitled','status','openaccess','openaccessFlag','pageRange','startingPage','endingPage','article-number','error-code'}
        out['access_metadata']=[{e.tag.rsplit('}',1)[-1]:(e.text or '').strip()[:160]} for e in root.iter() if e.tag.rsplit('}',1)[-1] in fields]
        out['original_text_chars']=sum(len(''.join(e.itertext())) for e in root.iter() if e.tag.rsplit('}',1)[-1]=='originalText')
    except urllib.error.HTTPError as e: out['http_status']=e.code
    except (OSError,ValueError,ET.ParseError):out['error']='connection_or_format_error'
    print(json.dumps(out))

for doi in ['10.1016/j.combustflame.2026.115285','10.1016/j.combustflame.2026.115373']:
    inspect(doi,True)
    inspect(doi)
