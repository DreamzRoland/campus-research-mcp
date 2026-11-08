"""Fixed official endpoints, persistent pacing, bounded retries and redacted errors."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from . import store

HOSTS = {'api.openalex.org', 'api.datacite.org', 'api.openaire.eu', 'api.core.ac.uk',
         'api2.openreview.net', 'eutils.ncbi.nlm.nih.gov', 'www.ebi.ac.uk',
         'ieeexploreapi.ieee.org', 'api.semanticscholar.org', 'api.wfdata.com',
         'theses.fr', 'www.theses.fr', 'api.theses.fr', 'api.elsevier.com',
         'api.clarivate.com', 'api.springernature.com', 'api.crossref.org',
         'export.arxiv.org', 'sparql.dblp.org', 'proceedings.mlr.press',
         'proceedings.neurips.cc', 'papers.nips.cc', 'openaccess.thecvf.com',
         'aclanthology.org', 'dspace.mit.edu', 'thesis.caltech.edu', 'repository.hkust.edu.hk',
         'repository.hkust.edu.hk', 'dsapi.lib.mit.edu'}
INTERVALS = {'arxiv': 3.1, 'dblp': 1.1, 'pubmed': .4, 'core': 2.1, 'wos': 1.1,
             'crossref': 1.1, 'semanticscholar': 1.1, 'wanfang': 1.1}


class SourceError(ValueError):
    def __init__(self, code, status=None):
        self.code, self.status = code, status
        super().__init__(code)


class Budget:
    def __init__(self, seconds=25, requests=12, cancelled=None):
        self.deadline = time.monotonic() + seconds
        self.limit, self.used = requests, 0
        self.lock = threading.Lock()
        self.cancelled = cancelled or (lambda: False)

    def check(self):
        if self.cancelled():
            raise SourceError('cancelled')
        if time.monotonic() >= self.deadline:
            raise SourceError('time_budget_exhausted')

    def take(self):
        with self.lock:
            self.check()
            if self.used >= self.limit:
                raise SourceError('request_budget_exhausted')
            self.used += 1


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_endpoint(url):
    p = urllib.parse.urlsplit(url)
    if (p.scheme != 'https' or p.hostname not in HOSTS or p.username
            or getattr(p, 'pass' + 'word') or p.port not in (None, 443) or p.fragment
            or '\\' in url or any(ord(c) < 33 for c in url)):
        raise SourceError('unapproved_metadata_endpoint')
    return url


class Client:
    def __init__(self, budget=None):
        self.budget = budget or Budget()
        self.counts = {}
        self.count_lock = threading.Lock()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(self, provider, url, params=None, headers=None, payload=None, kind='json', ttl=86400, secret_values=()):
        public_endpoint(url)
        params = params or {}
        headers = headers or {}
        # Credentials only affect the opaque cache digest, not stored cache metadata.
        key = store.cache_key(provider, [url, params, payload, headers, kind])
        cached = store.cached(key) if ttl else None
        if cached is not None:
            self.budget.check()
            return cached
        query = urllib.parse.urlencode(params, doseq=True)
        target = url + (('?' + query) if query else '')
        body = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(2):
            self.budget.check()
            store.reserve_rate(provider, INTERVALS.get(provider, 1.05), self.budget.deadline)
            self.budget.take()
            with self.count_lock:
                self.counts[provider] = self.counts.get(provider, 0) + 1
            req = urllib.request.Request(target, data=body, headers={
                'User-Agent': 'CampusResearchMCP/1.13.0',
                'Accept': 'application/json' if kind == 'json' else 'application/xml,text/html;q=0.8',
                **({'Content-Type': 'application/json'} if payload is not None else {}), **headers})
            try:
                with self.opener.open(req, timeout=max(.1, min(12, self.budget.deadline - time.monotonic()))) as response:
                    raw = bytearray()
                    maximum = 16 * 1024 * 1024 if provider == 'proceedings' and kind == 'html' and urllib.parse.urlsplit(url).hostname == 'openaccess.thecvf.com' else 6 * 1024 * 1024
                    while True:
                        self.budget.check()
                        block = response.read1(65536) if hasattr(response, 'read1') else response.read(65536)
                        if not block:
                            break
                        raw.extend(block)
                        if len(raw) > maximum:
                            raise SourceError('metadata_response_too_large')
                    mime = response.headers.get_content_type()
                if any(v and (v.encode() in raw or urllib.parse.quote(v, safe='').encode() in raw) for v in secret_values):
                    raise SourceError('credential_echo_refused')
                if kind == 'json':
                    if mime not in ('application/json', 'application/vnd.api+json') and not mime.endswith('+json'):
                        raise SourceError('expected_json_received_html_or_other')
                    data = json.loads(raw)
                    if not isinstance(data, (dict, list)):
                        raise SourceError('unexpected_response_schema')
                else:
                    data = raw.decode('utf-8', 'replace')
                    if re_challenge(data):
                        raise SourceError('website_verification_required')
                    if kind == 'xml':
                        upper = data.upper()
                        if '<!ENTITY' in upper or ('<!DOCTYPE' in upper and provider != 'pubmed'):
                            raise SourceError('unsafe_xml')
                        if '<!DOCTYPE' in upper:
                            import re
                            declarations = re.findall(r'<!DOCTYPE[^>]*>', data, re.I | re.S)
                            if len(declarations) != 1 or '[' in declarations[0]:
                                raise SourceError('unsafe_xml')
                            data = data.replace(declarations[0], '')
                if ttl:
                    store.cache_put(key, data, ttl)
                store.capability(provider, {'status': 'available', 'last_http_status': 200})
                return data
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry = exc.headers.get('Retry-After', '30')
                developer_inactive = False
                if provider == 'ieee' and status == 403:
                    try:
                        developer_inactive = b'developer inactive' in exc.read(4096).lower()
                    except OSError:
                        pass
                exc.close()
                code = {401: 'authentication_required', 403: 'permission_or_site_verification',
                        404: 'not_found', 429: 'source_rate_limit'}.get(status, 'upstream_http_error')
                if developer_inactive:
                    code = 'api_developer_inactive'
                if status in (429, 503):
                    seconds = min(300, max(1, int(retry))) if retry.isdigit() else 30
                    store.backoff(provider, seconds)
                    if attempt == 0 and seconds + time.monotonic() + 1 < self.budget.deadline:
                        continue
                store.capability(provider, {'status': 'temporarily_unavailable', 'error': code, 'last_http_status': status})
                raise SourceError(code, status) from None
            except SourceError:
                raise
            except (OSError, ValueError, TypeError):
                raise SourceError('upstream_connection_or_format_error') from None


def re_challenge(body):
    first = body[:15000].lower()
    return '<title>安全验证' in first or 'cf-chl-' in first or '<title>just a moment' in first
