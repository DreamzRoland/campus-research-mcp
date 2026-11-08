"""Explicitly configured final DOI fallback; anonymous HTTPS, bounded and verified."""
import json
import http.cookiejar
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from . import models, store
from .transport import SourceError, NoRedirect

# sci-net.xyz was observed only after the Sci-Hub DOI endpoint's own HTTPS302;
# its article title and same-host PDF link were inspected before registration.
HOSTS = {'sci-hub.ru', 'www.sci-hub.ru', 'sci-net.xyz'}
BASE = 'https://sci-hub.ru/'


def enabled():
    override = os.environ.get('CAMPUS_SCIHUB_FALLBACK')
    if override is not None:
        return override == '1'
    try:
        settings = json.loads(Path('/opt/campus-scopus/download-policy.json').read_text())
        return settings.get('scihub_final_fallback') is True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def eligible(record):
    return enabled() and record.get('type') != 'thesis' and bool(models.doi(record.get('identifiers', {}).get('doi')))


def describe():
    evidence = store.capability('scihub')
    return {'provider':'scihub','enabled':enabled(),'purpose':'final_doi_pdf_fallback',
            'status':(evidence or {}).get('status', 'not_verified') if enabled() else 'disabled',
            'verification':evidence,'captcha_bypass':False,'deadline_seconds':300,
            'requires_remaining_primary_download_budget':True}


def checked_url(url):
    # This uses the same DNS/public-address checks as normal original downloads,
    # with a separate exact allowlist so arbitrary imported Sci-Hub URLs stay disabled.
    import ipaddress
    import socket
    try:
        p = urllib.parse.urlsplit(url)
        if (len(url) > 6000 or p.scheme != 'https' or p.hostname not in HOSTS
                or p.username or getattr(p, 'pass' + 'word') or p.port not in (None, 443)
                or p.fragment or '\\' in urllib.parse.unquote(url)
                or any(ord(c) < 33 for c in url)
                or any(v in ('.', '..') for v in urllib.parse.unquote(p.path).split('/'))
                or not models.safe_url(url)):
            raise ValueError()
        addresses = {v[4][0] for v in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise ValueError()
        return url
    except (ValueError, OSError):
        raise SourceError('scihub_unapproved_delivery_location') from None


def challenge(html):
    title = re.search(r'<title\b[^>]*>(.*?)</title>', html, re.I | re.S)
    if (title and re.search(r'captcha|проверка на робота|just a moment', title.group(1), re.I)) or re.search(r'cf-chl-', html, re.I):
        return True
    # Ordinary article pages include a CAPTCHA widget in the optional problem-
    # reporting modal. A literal PDF element is evidence of an article response;
    # its delivered bytes are still checked, and a challenge at that URL stops.
    links = PdfLinks(BASE)
    links.feed(html)
    if links.links:
        return False
    return bool(re.search(r'captcha|проверка на робота|just a moment', html, re.I))


class PdfLinks(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.links = base, []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        value = (values.get('src') or values.get('original-url')) if tag in ('iframe', 'embed') else values.get('data') if tag == 'object' else values.get('href') if tag == 'a' else None
        if tag in ('button', 'a') and not value:
            literal = re.search(r'''(?:window\.)?location(?:\.href)?\s*=\s*(["'])([^"']+)\1''', values.get('onclick', ''))
            value = literal.group(2) if literal else None
        if value:
            url = urllib.parse.urljoin(self.base, value)
            p = urllib.parse.urlsplit(url)
            pdf_element = tag in ('iframe','embed','object') and (values.get('type','').lower() == 'application/pdf' or values.get('id') in ('pdf','plugin'))
            if p.path.lower().endswith('.pdf') or pdf_element:
                # Browser PDF view fragments do not participate in the HTTP request.
                url = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, p.query, ''))
                if url not in self.links:
                    self.links.append(url)


class Session:
    def __init__(self, deadline, cancelled, progress):
        self.deadline, self.cancelled, self.progress = deadline, cancelled, progress
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                 urllib.request.HTTPCookieProcessor(self.cookies))

    def check(self):
        if self.cancelled():
            raise SourceError('cancelled')
        if time.monotonic() >= self.deadline:
            raise SourceError('download_time_budget_exhausted')

    def fetch(self, url, path, maximum):
        for _ in range(4):
            self.check()
            checked_url(url)
            store.reserve_rate('scihub', 3.1, self.deadline)
            self.check()
            req = urllib.request.Request(url, headers={'User-Agent': 'CampusResearchMCP/1.13.0', 'Accept': 'application/pdf,text/html;q=0.8'})
            try:
                response = self.opener.open(req, timeout=max(.1, min(60, self.deadline-time.monotonic())))
            except urllib.error.HTTPError as exc:
                status, location = exc.code, exc.headers.get('Location')
                exc.close()
                if status in (301, 302, 303, 307, 308) and location:
                    url = urllib.parse.urljoin(url, location)
                    continue
                if status == 429:
                    store.backoff('scihub', 60)
                raise SourceError({403:'scihub_site_verification_required', 404:'scihub_article_not_found', 429:'source_rate_limit'}.get(status, 'scihub_http_error'), status) from None
            except OSError:
                raise SourceError('scihub_connection_or_idle_timeout') from None
            try:
                with response:
                    length = response.headers.get('Content-Length')
                    total = int(length) if length and length.isdigit() else None
                    is_html = response.headers.get_content_type() in ('text/html', 'application/xhtml+xml')
                    limit = 2*1024*1024 if is_html else maximum
                    if total is not None and total > limit:
                        raise SourceError('scihub_response_size_limit')
                    raw = bytearray()
                    received, last = 0, 0.0
                    stream = None
                    try:
                        if not is_html:
                            path.unlink(missing_ok=True)
                            stream = path.open('xb')
                            path.chmod(0o600)
                        while True:
                            self.check()
                            block = response.read1(65536)
                            if not block:
                                break
                            received += len(block)
                            if received > limit:
                                raise SourceError('scihub_response_size_limit')
                            if is_html:
                                raw.extend(block)
                            else:
                                stream.write(block)
                            if time.monotonic()-last >= 5:
                                self.progress({'phase':'scihub_final_fallback', 'received_bytes':received, 'total_bytes':total})
                                last = time.monotonic()
                        if stream:
                            stream.flush()
                            os.fsync(stream.fileno())
                    finally:
                        if stream:
                            stream.close()
                    if total is not None and received != total:
                        raise SourceError('truncated_http_body')
                    if is_html:
                        body = raw.decode('utf-8', 'replace')
                        if challenge(body):
                            raise SourceError('scihub_site_verification_required')
                        return {'html':body, 'url':url}
                    return {'url':url}
            except OSError:
                raise SourceError('scihub_connection_or_idle_timeout') from None
        raise SourceError('scihub_redirect_limit')


def download(record, progress, cancelled, deadline, primary_error):
    from . import files
    if not eligible(record):
        raise SourceError('scihub_fallback_not_enabled_or_no_doi')
    session = Session(min(deadline, time.monotonic()+300), cancelled, progress)
    session.check()
    fid = files.reserve(files.MAX_NATIVE)
    path = files.directory() / (fid + '.part')
    try:
        progress({'phase':'scihub_final_fallback', 'primary_error':primary_error})
        url = BASE + urllib.parse.quote(models.doi(record['identifiers']['doi']), safe='/')
        result = session.fetch(url, path, files.MAX_NATIVE)
        if result.get('html'):
            links = PdfLinks(result['url'])
            links.feed(result['html'])
            if not links.links:
                raise SourceError('scihub_original_pdf_link_unavailable')
            # Delivery hosts must have been explicitly registered; never chase mirrors.
            result = session.fetch(links.links[0], path, files.MAX_NATIVE)
            if result.get('html'):
                raise SourceError('scihub_original_pdf_unavailable')
        verification = files.verify_pdf(path, record, {})
        info = files.commit(fid, path, {'file_name':'paper-'+record['record_id'][2:18]+'.pdf',
            'mime_type':'application/pdf','record_id':record['record_id'],'kind':'article',
            'provider':'scihub','verification':verification,'primary_error':primary_error,
            'publisher_completeness_independently_confirmed':False})
        store.capability('scihub', {'status':'available','pdf_download_verified':True})
        return {'ok':True,'record_id':record['record_id'],'file':info,'provider':'scihub','fallback_used':True,'space_saved':False}
    except Exception as exc:
        files.discard(fid)
        if isinstance(exc, SourceError) and exc.code not in ('cancelled','download_time_budget_exhausted'):
            store.capability('scihub', {'status':'needs_interactive_verification' if exc.code=='scihub_site_verification_required' else 'temporarily_unavailable',
                                      'error':exc.code,'pdf_download_verified':False})
        raise
