"""Bounded read-only comparison of official Elsevier PDF API variants.

Uses existing service credentials; emits no credential values, URLs with
credentials, response bodies, or redirect query strings.
"""
import io,json,sys,uuid,urllib.error,urllib.parse,urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime,timezone
from pypdf import PdfReader
sys.path.insert(0,'/opt/campus-scopus')
from server import secret
from article_downloads import DIRECT

KEY=secret('SCIENCEDIRECT_API_KEY')
TOKEN=secret('ELSEVIER_INST_TOKEN')
LIMIT=20*1024*1024
rows=[]

def fetch(label,path,params=None,accept='application/pdf',key=None,token=True,version=False):
    headers={'X-ELS-APIKey':key or KEY,'X-ELS-ReqId':str(uuid.uuid4())}
    if accept:headers['Accept']=accept
    if token and TOKEN:headers['X-ELS-Insttoken']=TOKEN
    if version:headers['X-ELS-ResourceVersion']='new'
    url='https://api.elsevier.com'+path
    if params:url+='?'+urllib.parse.urlencode(params)
    out={'test':label,'request_id':headers['X-ELS-ReqId']}
    raw=b''
    try:
        with DIRECT.open(urllib.request.Request(url,headers=headers),timeout=20) as response:
            raw=response.read(LIMIT+1)
            out.update(status=response.status,mime=response.headers.get('Content-Type','').split(';')[0],
                       bytes=len(raw),declared_length=response.headers.get('Content-Length'),
                       content_range=response.headers.get('Content-Range'))
        if len(raw)>LIMIT:out['error']='size_limit';raw=b''
        elif raw.startswith(b'%PDF-'):
            reader=PdfReader(io.BytesIO(raw),strict=True)
            out.update(pdf_pages=len(reader.pages),pdf_parse_valid=True,has_eof=raw.rstrip().endswith(b'%%EOF'))
        elif 'xml' in out['mime'] or 'json' in out['mime']:
            if 'xml' in out['mime']:
                root=ET.fromstring(raw)
                tags=lambda n:[e for e in root.iter() if e.tag.rsplit('}',1)[-1]==n]
                out['xml_sections']=len(tags('section'))
                out['xml_references']=len(tags('bib-reference'))
                out['openaccess']=[(e.text or '').strip() for e in tags('openaccess')][:1]
                out['entitled']=[(e.text or '').strip() for e in tags('entitled')][:2]
    except urllib.error.HTTPError as exc:
        out['status']=exc.code
        loc=urllib.parse.urlsplit(exc.headers.get('Location',''))
        if loc.hostname:out.update(redirect_scheme=loc.scheme,redirect_host=loc.hostname)
    except (OSError,ValueError,ET.ParseError):out['error']='connection_or_format_error'
    rows.append(out);print(json.dumps(out),flush=True)
    return raw

primary='10.1016/j.combustflame.2026.115285'
base='/content/article/doi/'+urllib.parse.quote(primary,safe='')
raw=fetch('primary_full_xml',base,{'view':'FULL'},'application/xml')
root=ET.fromstring(raw)
pii=next((e.text for e in root.iter() if e.tag.rsplit('}',1)[-1]=='pii'),None)
assert pii
pii=''.join(c for c in pii if c.isalnum())
fetch('current_doi_full_header_and_query',base,{'view':'FULL','httpAccept':'application/pdf'})
fetch('doi_header_only_no_view',base)
fetch('doi_header_only_full',base,{'view':'FULL'})
fetch('doi_query_only_full',base,{'view':'FULL','httpAccept':'application/pdf'},accept=None)
fetch('pii_header_only_full','/content/article/pii/'+pii,{'view':'FULL'})
fetch('doi_unescaped_slash_full','/content/article/doi/'+primary,{'view':'FULL'})
fetch('doi_entitled_view',base,{'view':'ENTITLED'})
fetch('doi_latest_version',base,{'view':'FULL'},version=True)
fetch('doi_author_manuscript_redirect',base,{'view':'FULL','amsRedirect':'true'})
if TOKEN:fetch('doi_without_insttoken',base,{'view':'FULL'},token=False)
other_key=secret('SCOPUS_API_KEY')
if other_key and other_key!=KEY:fetch('doi_alternate_configured_key',base,{'view':'FULL'},key=other_key)

for name,doi in [('other_subscription','10.1016/j.combustflame.2026.115373'),
                 ('oa_control_1','10.1016/j.combustflame.2026.115355'),
                 ('oa_control_2','10.1016/j.combustflame.2026.115386')]:
    path='/content/article/doi/'+urllib.parse.quote(doi,safe='')
    fetch(name+'_pdf',path,{'view':'FULL'})
    if name.startswith('oa_'):fetch(name+'_xml',path,{'view':'FULL'},'application/xml')

report={'checked_at_utc':datetime.now(timezone.utc).isoformat(),'primary_doi':primary,'primary_pii':pii,
        'route':'direct; environment proxies disabled','insttoken_present':bool(TOKEN),
        'configured_keys_differ':bool(other_key and other_key!=KEY),'results':rows}
target=Path('/var/lib/campus-scopus/pdf-api-investigation.json')
target.write_text(json.dumps(report,indent=2));target.chmod(0o600)
print(json.dumps({'report_saved':True,'test_count':len(rows)}),flush=True)
