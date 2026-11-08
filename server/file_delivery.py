"""Optional expiring HTTPS file links; bind behind a TLS proxy, never expose cache paths.

Disabled unless explicitly configured with a public HTTPS base URL and a
private signing key file. No network listener starts when this is imported.
"""
import base64,hashlib,hmac,json,os,re,time,urllib.parse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

TTL=600
MAX_FILE=20*1024*1024

def signing_key():
    path=os.environ.get('CAMPUS_DELIVERY_KEY_FILE','')
    if not path:return None
    value=Path(path).read_bytes()
    if len(value)<32:raise ValueError('Delivery signing key must contain at least 32 bytes')
    return value

def create_link(download_id,metadata,now=None):
    base=os.environ.get('CAMPUS_DELIVERY_HTTPS_BASE','').rstrip('/')
    if not base:return None
    parsed=urllib.parse.urlsplit(base)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Delivery base must be a clean HTTPS origin or path')
    key=signing_key()
    if key is None:return None
    if not re.fullmatch(r'[0-9a-f]{48}',download_id):raise ValueError('Invalid download id')
    current=int(time.time() if now is None else now)
    expiry=min(current+TTL,int(metadata['expires_at']))
    payload={'id':download_id,'exp':expiry,'sha256':metadata['sha256']}
    raw=json.dumps(payload,separators=(',',':'),sort_keys=True).encode()
    encoded=base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    signature=hmac.new(key,encoded.encode(),hashlib.sha256).hexdigest()
    return {'url':base+'/download/'+encoded+'.'+signature,'expires_at':expiry}

def resolve_ticket(ticket,directory,key,now=None):
    if len(ticket)>512:raise ValueError('Invalid ticket')
    encoded,signature=ticket.split('.')
    expected=hmac.new(key,encoded.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,signature):raise ValueError('Invalid ticket')
    payload=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
    current=time.time() if now is None else now
    if not current<float(payload['exp'])<=current+TTL:raise ValueError('Expired ticket')
    token=payload['id']
    if not re.fullmatch(r'[0-9a-f]{48}',token):raise ValueError('Invalid ticket')
    manifest=directory/(token+'.json');binary=directory/(token+'.bin')
    if manifest.is_symlink() or binary.is_symlink():raise ValueError('Invalid file')
    metadata=json.loads(manifest.read_text())
    if metadata['expires_at']<=current or metadata['sha256']!=payload['sha256']:raise ValueError('Expired file')
    if metadata.get('format') not in ('pdf','xml'):raise ValueError('Invalid format')
    if binary.stat().st_size!=metadata['byte_size'] or not 0<metadata['byte_size']<=MAX_FILE:raise ValueError('Invalid size')
    body=binary.read_bytes()
    if hashlib.sha256(body).hexdigest()!=metadata['sha256']:raise ValueError('Invalid file digest')
    if metadata['format']=='pdf':
        from article_downloads import validate_pdf_content
        validate_pdf_content(binary)
    return metadata,body

class Handler(BaseHTTPRequestHandler):
    # Never log bearer links or request headers.
    def log_message(self,*args):pass
    def do_HEAD(self):self.serve(head=True)
    def do_GET(self):self.serve()
    def serve(self,head=False):
        path=urllib.parse.urlsplit(self.path).path
        marker='/download/'
        if not path.startswith(marker):self.send_error(404);return
        try:
            metadata,body=resolve_ticket(path[len(marker):],self.server.cache,self.server.signing_key)
        except Exception:
            self.send_error(404,'File unavailable or expired');return
        filename=re.sub(r'[^A-Za-z0-9._-]','_',metadata['file_name'])[:200]
        mime='application/pdf' if metadata['format']=='pdf' else 'application/xml'
        self.send_response(200)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Disposition','attachment; filename="'+filename+'"')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','private, no-store')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        if not head:self.wfile.write(body)

def main():
    key=signing_key()
    if key is None:raise SystemExit('Delivery is not configured')
    server=ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('CAMPUS_DELIVERY_PORT','8091'))),Handler)
    server.cache=Path(os.environ.get('CAMPUS_DOWNLOAD_DIR','/var/lib/campus-scopus/downloads'))
    server.signing_key=key
    server.serve_forever()

if __name__=='__main__':main()
