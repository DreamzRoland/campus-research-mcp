"""Private, quota-reserved originals and native attachments; no public file server."""
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from pypdf import PdfReader
from . import models, store
from .transport import Budget, Client, SourceError, re_challenge

MAX_THESIS = 100 * 1024 * 1024
MAX_NATIVE = 20 * 1024 * 1024
QUOTA = 2 * 1024 * 1024 * 1024
LIFETIME = 86400
TERMINAL_DOWNLOAD_ERRORS = frozenset({
    'cancelled', 'download_time_budget_exhausted', 'private_file_cache_full',
    'file_size_limit', 'unregistered_or_unsafe_fulltext_location',
})
DOWNLOAD_HOSTS = {
    'arxiv.org', 'export.arxiv.org', 'openreview.net', 'api2.openreview.net',
    'proceedings.mlr.press', 'proceedings.neurips.cc', 'papers.nips.cc', 'openaccess.thecvf.com',
    'aclanthology.org', 'raw.githubusercontent.com', 'dspace.mit.edu', 'dsapi.lib.mit.edu',
    'thesis.caltech.edu', 'repository.hkust.edu.hk', 'lbezone.hkust.edu.hk',
    'theses.fr', 'www.theses.fr', 'api.theses.fr', 'theses.hal.science', 'tel.archives-ouvertes.fr',
    'zenodo.org', 'dash.harvard.edu', 'nrs.harvard.edu', 'repository.tudelft.nl',
    'etheses.whiterose.ac.uk', 'etheses.whiterose.ac.uk', 'www.repository.cam.ac.uk',
    'europepmc.org', 'www.ebi.ac.uk', 'pmc.ncbi.nlm.nih.gov',
}


def directory():
    p = store.root() / 'files'
    p.mkdir(exist_ok=True, mode=0o700)
    p.chmod(0o700)
    return p


def checked_url(url, resolve_dns=True, signed_delivery=False):
    try:
        p = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(p.path)
        delivery = (signed_delivery and p.hostname == 'cf003.cdn.4science.cloud'
                    and p.path.startswith('/4s-assetstore-prod--mit--dspace/'))
        if (len(url) > 6000 or p.scheme != 'https' or (p.hostname not in DOWNLOAD_HOSTS and not delivery) or p.username
                or getattr(p, 'pass' + 'word') or p.port not in (None, 443) or p.fragment
                or '\\' in url or '\\' in path or any(ord(c) < 33 for c in url)
                or any(v in ('.', '..') for v in path.split('/'))):
            raise ValueError()
        if p.hostname == 'raw.githubusercontent.com' and not re.fullmatch(r'/mlresearch/v\d+/(?:main|gh-pages)/assets/[^?#]+\.pdf', path):
            raise ValueError()
        if delivery:
            allowed = {'response-content-type', 'response-content-disposition', 'Expires', 'Signature', 'Key-Pair-Id'}
            if set(k for k, _ in urllib.parse.parse_qsl(p.query)) - allowed:
                raise ValueError()
        elif not models.safe_url(url):
            raise ValueError()
        if resolve_dns:
            addresses = {v[4][0] for v in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                raise ValueError()
        return url
    except (ValueError, OSError):
        raise SourceError('unregistered_or_unsafe_fulltext_location') from None


class Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = urllib.parse.urlsplit(req.full_url)
        official_delivery = ((source.hostname == 'dspace.mit.edu' and source.path.startswith('/server/api/core/bitstreams/') and source.path.endswith('/content'))
                             or (source.hostname == 'cf003.cdn.4science.cloud' and source.path.startswith('/4s-assetstore-prod--mit--dspace/')))
        checked_url(newurl, signed_delivery=official_delivery)
        # Never forward provider API credentials or browser cookies.
        return urllib.request.Request(newurl, headers={'User-Agent': 'CampusResearchMCP/1.13.0', 'Accept': 'application/pdf,text/html;q=0.5'})


def cleanup():
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute('SELECT id FROM files WHERE expires<?', (time.time(),)).fetchall()
        for row in rows:
            for suffix in ('.bin', '.part'):
                (directory() / (row['id'] + suffix)).unlink(missing_ok=True)
        db.execute('DELETE FROM files WHERE expires<?', (time.time(),))
        db.commit()


def reserve(size=MAX_THESIS):
    cleanup()
    fid = store.identifier('f_')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        used = db.execute('SELECT coalesce(sum(size),0) FROM files').fetchone()[0]
        if used + size > QUOTA:
            db.rollback()
            raise SourceError('private_file_cache_full')
        db.execute('INSERT INTO files VALUES(?,?,?,?,?)', (fid, 'reserved', size, '{}', time.time() + 2100))
        db.commit()
    return fid


def discard(fid):
    for suffix in ('.bin', '.part'):
        (directory() / (fid + suffix)).unlink(missing_ok=True)
    with store.connect() as db:
        db.execute('DELETE FROM files WHERE id=?', (fid,))


def commit(fid, path, info):
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            digest.update(block)
    info = {**info, 'file_id': fid, 'byte_size': size, 'sha256': digest.hexdigest(),
            'expires_at': time.time() + LIFETIME, 'native_attachment_available': size <= MAX_NATIVE,
            'space_saved': False}
    target = directory() / (fid + '.bin')
    os.replace(path, target)
    target.chmod(0o600)
    with store.connect() as db:
        db.execute('UPDATE files SET state=?,size=?,payload=?,expires=? WHERE id=?',
                   ('completed', size, store.encode(info), info['expires_at'], fid))
    if size > MAX_NATIVE:
        info['retrieval'] = {'method': 'existing_ssh', 'prepare_command': 'ssh -t research-host "sudo /var/lib/campus-scopus/.venv/bin/python -B /opt/campus-scopus/export_research_file.py ' + fid + '"',
                             'reason': 'exceeds_current_20MiB_native_implementation_threshold',
                             'instructions': 'Run prepare_command locally; it prints a private SCP command. Original bytes are preserved.'}
    return info


def save_bytes(data, filename, mime):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_NATIVE:
        raise ValueError('export_exceeds_native_file_limit')
    fid = reserve(len(data))
    path = directory() / (fid + '.part')
    try:
        with path.open('xb') as f:
            path.chmod(0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return commit(fid, path, {'file_name': filename, 'mime_type': mime, 'kind': 'export'})
    except Exception:
        discard(fid)
        raise


def file_info(fid):
    if not re.fullmatch(r'f_[a-f0-9]{48}', fid):
        raise ValueError('invalid_file_id')
    with store.connect() as db:
        row = db.execute("SELECT payload FROM files WHERE id=? AND state='completed' AND expires>?", (fid, time.time())).fetchone()
    if not row:
        raise ValueError('file_not_found_or_expired')
    return json.loads(row[0])


def read_native(fid, filename):
    info = file_info(fid)
    if info['file_name'] != filename or info['byte_size'] > MAX_NATIVE:
        raise ValueError('native_file_name_or_size_mismatch')
    data = (directory() / (fid + '.bin')).read_bytes()
    if len(data) != info['byte_size'] or hashlib.sha256(data).hexdigest() != info['sha256']:
        raise ValueError('file_integrity_check_failed')
    return data


def link(info):
    if not info.get('native_attachment_available'):
        return None
    from mcp.types import ResourceLink
    return ResourceLink(type='resource_link', uri='file:///campus-research/' + info['file_id'] + '/' + urllib.parse.quote(info['file_name'], safe=''),
                        name=info['file_name'], mime_type=info['mime_type'], size=info['byte_size'],
                        description='Original verified bytes; SHA-256 ' + info['sha256'] + '. Host materialization is required; Space persistence is not asserted.')


def resolve(record_id, secret=lambda _: ''):
    record = store.get_record(record_id)
    accepted, unavailable = [], []
    if 'mit' in record.get('source_records', {}):
        try:
            from .repositories import repository_files
            accepted += repository_files(record)
        except SourceError as exc:
            unavailable.append({'reason': exc.code, 'source': 'mit_mets'})
    for location in record.get('access', {}).get('locations', []):
        try:
            checked_url(location['url'], resolve_dns=False)
            accepted.append(location)
        except SourceError:
            unavailable.append({'url': location['url'], 'reason': 'source_download_adapter_not_registered'})
    def priority(l):
        h = urllib.parse.urlsplit(l['url']).hostname
        repository = h in ('dspace.mit.edu', 'dsapi.lib.mit.edu', 'thesis.caltech.edu', 'repository.hkust.edu.hk', 'dash.harvard.edu')
        return (0 if l.get('repository_file_relation_verified') else 1 if repository else 2, 0 if l.get('role') == 'pdf' else 1)
    accepted.sort(key=priority)
    d = record.get('identifiers', {}).get('doi', '')
    routes = []
    for prefix, name in [('10.1016/', 'sciencedirect'), ('10.1007/', 'springer'), ('10.1038/', 'springer'),
                         ('10.1017/', 'cambridge'), ('10.1002/', 'wiley'), ('10.1111/', 'wiley'),
                         ('10.1063/', 'aip'), ('10.1115/', 'asme'), ('10.1021/', 'acs'), ('10.3390/', 'mdpi')]:
        if d.startswith(prefix):
            routes.append(name)
    from . import scihub
    fallback = ['scihub'] if scihub.eligible(record) else []
    return {'ok': True, 'record_id': record_id, 'access_status': record['access']['status'],
            'locations': accepted, 'publisher_routes': routes, 'unavailable_locations': unavailable,
            'fallback_routes': fallback, 'fallback_policy': 'only_after_primary_routes_fail',
            'downloadable_route_found': bool(accepted or routes or fallback), 'download_rights_verified': False,
            'reason': None if accepted or routes or fallback else 'no_registered_authorized_download_route', 'space_saved': False}


def verify_pdf(path, record, origin):
    if origin.get('expected_byte_size') and path.stat().st_size != origin['expected_byte_size']:
        raise SourceError('repository_declared_size_mismatch')
    if origin.get('expected_md5'):
        checksum = hashlib.md5()
        with path.open('rb') as f:
            for block in iter(lambda: f.read(65536), b''):
                checksum.update(block)
        if checksum.hexdigest() != origin['expected_md5'].lower():
            raise SourceError('repository_declared_checksum_mismatch')
    with path.open('rb') as f:
        first = f.read(1024)
        f.seek(max(0, path.stat().st_size - 4096))
        last = f.read()
    if not first.lstrip().startswith(b'%PDF-') or b'%%EOF' not in last:
        raise SourceError('not_a_complete_pdf')
    try:
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise SourceError('encrypted_pdf_not_verifiable')
        pages = len(reader.pages)
        if pages < 2:
            raise SourceError('single_page_or_preview_refused')
        indexes = sorted(set(list(range(min(8, pages))) + list(range(max(0, pages - 2), pages))))
        extracted = ' '.join((reader.pages[i].extract_text() or '')[:40000] for i in indexes)
    except SourceError:
        raise
    except Exception:
        raise SourceError('invalid_pdf_structure') from None
    norm = models.normalized_title(extracted)
    identifiers = record.get('identifiers', {})
    d = identifiers.get('doi')
    title = models.normalized_title(record['title'])
    authors = [models.normalized_title(a if isinstance(a, str) else a.get('name', '')) for a in record.get('authors', [])]
    author_ok = any(a and (a in norm or a.split(',')[0] in norm) for a in authors)
    inst = models.normalized_title((record.get('thesis') or {}).get('institution', ''))
    if d and d.casefold() in extracted.casefold():
        method = 'exact_doi_in_pdf'
    elif title and title in norm and author_ok:
        if record.get('type') == 'thesis' and not (inst and inst in norm):
            # Exact repository file relation is stronger than an aggregator-suggested URL.
            if not origin.get('repository_file_relation_verified'):
                raise SourceError('thesis_awarding_institution_or_file_relation_unverified')
        method = 'title_author_and_repository_relation' if origin.get('repository_file_relation_verified') else 'title_author_in_pdf'
    elif not norm and origin.get('repository_file_relation_verified') and origin.get('persistent_identifier_verified'):
        method = 'scanned_pdf_exact_repository_identifier_file_relation'
    elif origin.get('repository_file_relation_verified') and origin.get('persistent_identifier_verified'):
        method = 'exact_repository_identifier_original_file_relation'
    else:
        raise SourceError('pdf_identity_unverified')
    return {'pdf_page_count': pages, 'identity_method': method, 'structure_verified': True,
            'repository_declared_size_verified': bool(origin.get('expected_byte_size')),
            'repository_declared_checksum_verified': bool(origin.get('expected_md5')),
            'original_bytes_preserved': True, 'publisher_completeness_independently_confirmed': False}


def stream(url, path, deadline, progress, cancelled, maximum):
    checked_url(url)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirects())
    try:
        with op.open(urllib.request.Request(url, headers={'User-Agent': 'CampusResearchMCP/1.13.0', 'Accept': 'application/pdf,text/html;q=0.5'}), timeout=60) as r:
            mime = r.headers.get_content_type()
            length = r.headers.get('Content-Length')
            total = int(length) if length and length.isdigit() else None
            if total is not None and total > maximum:
                raise SourceError('file_size_limit')
            if mime in ('text/html', 'application/xhtml+xml'):
                raw = r.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise SourceError('landing_page_too_large')
                html = raw.decode('utf-8', 'replace')
                if re_challenge(html):
                    raise SourceError('website_verification_required')
                return {'html': html, 'url': r.url}
            received, last = 0, 0.0
            with path.open('xb') as f:
                path.chmod(0o600)
                while True:
                    if cancelled():
                        raise SourceError('cancelled')
                    if time.monotonic() > deadline:
                        raise SourceError('download_time_budget_exhausted')
                    block = r.read1(65536)
                    if not block:
                        break
                    received += len(block)
                    if received > maximum:
                        raise SourceError('file_size_limit')
                    f.write(block)
                    if time.monotonic() - last >= 5:
                        progress({'phase': 'downloading', 'received_bytes': received, 'total_bytes': total})
                        last = time.monotonic()
                f.flush()
                os.fsync(f.fileno())
            if total is not None and total != received:
                raise SourceError('truncated_http_body')
            return {'url': r.url, 'mime_type': mime}
    except SourceError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        retry = exc.headers.get('Retry-After', '60')
        exc.close()
        error = SourceError({401: 'authentication_required', 403: 'institutional_access_or_site_verification_required', 404: 'file_not_found', 429: 'source_rate_limit'}.get(status, 'download_http_error'), status)
        if status == 429:
            if retry.isdigit():
                error.retry_after = int(retry)
            else:
                try:
                    from email.utils import parsedate_to_datetime
                    error.retry_after = max(1, parsedate_to_datetime(retry).timestamp() - time.time())
                except (ValueError, TypeError):
                    error.retry_after = 60
        raise error from None
    except OSError:
        raise SourceError('download_connection_or_idle_timeout') from None


def stream_with_retry(url, path, deadline, progress, cancelled, maximum):
    for attempt in range(3):
        path.unlink(missing_ok=True)
        try:
            return stream(url, path, deadline, progress, cancelled, maximum)
        except SourceError as exc:
            if exc.code not in ('download_connection_or_idle_timeout', 'source_rate_limit') or attempt == 2:
                raise
            seconds = getattr(exc, 'retry_after', 2 * (attempt + 1))
            if time.monotonic() + seconds >= deadline:
                raise SourceError('download_time_budget_exhausted') from None
            progress({'phase': 'waiting_for_source', 'retry_after_seconds': seconds, 'attempt': attempt + 2})
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if cancelled():
                    raise SourceError('cancelled')
                time.sleep(min(1, max(0, end - time.monotonic())))


def publisher_file(route, d, secret, progress, cancelled):
    # Existing tested downloaders retain their own credential, redirect and PDF checks.
    if cancelled():
        raise SourceError('cancelled')
    progress({'phase': 'publisher_download', 'provider': route})
    if route == 'cambridge':
        from crossref_cambridge import prepare_cambridge_pdf
        return prepare_cambridge_pdf(d)
    if route == 'springer':
        from springer_pdf import prepare_springer_pdf
        return prepare_springer_pdf(d)
    if route == 'wiley':
        from wiley_downloads import prepare_wiley_pdf
        return prepare_wiley_pdf(d, secret('WILEY_TDM_TOKEN'))
    if route == 'sciencedirect':
        from article_downloads import prepare_article
        return prepare_article(d, 'pdf', secret('SCIENCEDIRECT_API_KEY'), secret('ELSEVIER_INST_TOKEN'))
    if route == 'aip':
        from aip_downloads import prepare_aip_pdf
        return prepare_aip_pdf(d, lambda **fields: progress(fields))
    from publisher_downloads import prepare_publisher_pdf
    return prepare_publisher_pdf(route, d, lambda **fields: progress(fields))


def terminal_download_error(code):
    if code in TERMINAL_DOWNLOAD_ERRORS:
        return True
    # Also preserve terminal causes supplied by an older/composite primary route.
    prefix = 'original_pdf_unavailable:'
    return code.startswith(prefix) and bool(TERMINAL_DOWNLOAD_ERRORS.intersection(code[len(prefix):].split(',')))


def download_one(record_id, progress=lambda _: None, cancelled=lambda: False, secret=lambda _: '', resolved_override=None, suffix='', deadline_override=None):
    record = store.get_record(record_id)
    resolved = resolved_override or resolve(record_id, secret)
    if resolved['publisher_routes']:
        try:
            info, token = publisher_file(resolved['publisher_routes'][0], record['identifiers']['doi'], secret, progress, cancelled)
            return {'ok': True, 'record_id': record_id, 'legacy_file': {'metadata': info, 'token': token}, 'space_saved': False}
        except Exception as exc:
            if isinstance(exc, SourceError) and terminal_download_error(exc.code):
                raise
            from article_downloads import DownloadError
            if isinstance(exc, DownloadError) and exc.code and terminal_download_error(exc.code):
                # Legacy publisher tools keep their human-readable messages; the
                # unified flow uses explicit codes without exposing raw responses.
                raise SourceError(exc.code) from None
            if not resolved['locations']:
                raise SourceError('publisher_download_failed_no_other_authorized_route') from None
    if not resolved['locations']:
        raise SourceError('no_registered_authorized_download_route')
    fid = reserve()
    path = directory() / (fid + '.part')
    deadline = deadline_override or time.monotonic() + 1800
    errors = []
    try:
        for location in resolved['locations'][:6]:
            try:
                path.unlink(missing_ok=True)
                result = stream_with_retry(location['url'], path, deadline, progress, cancelled, MAX_THESIS if record['type'] == 'thesis' else MAX_NATIVE)
                if result.get('html'):
                    from .conferences import Page
                    page = Page(result['url'])
                    page.feed(result['html'])
                    title = (page.meta.get('citation_title') or page.meta.get('dc.title') or [''])[0]
                    if not title or models.normalized_title(title) != models.normalized_title(record['title']):
                        raise SourceError('landing_page_identity_unverified')
                    pdfs = page.meta.get('citation_pdf_url') or []
                    if not pdfs:
                        raise SourceError('original_pdf_link_unavailable')
                    result = stream_with_retry(pdfs[0], path, deadline, progress, cancelled, MAX_THESIS if record['type'] == 'thesis' else MAX_NATIVE)
                    if result.get('html'):
                        raise SourceError('original_pdf_requires_interactive_access')
                verification = verify_pdf(path, record, location)
                name = 'thesis-' if record['type'] == 'thesis' else 'paper-'
                name += record_id[2:18] + suffix + '.pdf'
                info = commit(fid, path, {'file_name': name, 'mime_type': 'application/pdf', 'record_id': record_id, 'kind': 'thesis' if record['type'] == 'thesis' else 'article', 'verification': verification})
                return {'ok': True, 'record_id': record_id, 'file': info, 'space_saved': False}
            except SourceError as exc:
                if terminal_download_error(exc.code):
                    raise
                errors.append(exc.code)
        raise SourceError('original_pdf_unavailable:' + ','.join(sorted(set(errors))))
    except Exception:
        discard(fid)
        raise


def download_primary(record_id, progress=lambda _: None, cancelled=lambda: False, secret=lambda _: ''):
    started = time.monotonic()
    resolved = resolve(record_id, secret)
    parts = {}
    for location in resolved['locations']:
        if location.get('repository_file_relation_verified') and location.get('part_id'):
            parts.setdefault(location['part_id'], []).append(location)
    if len(parts) <= 1:
        return download_one(record_id, progress, cancelled, secret, resolved, '', started + 1800)
    if len(parts) > 8:
        raise SourceError('original_volume_count_exceeds_eight')
    outputs = []
    try:
        for i, (partid, locations) in enumerate(parts.items(), 1):
            if time.monotonic() - started > 1800 or cancelled():
                raise SourceError('cancelled' if cancelled() else 'download_time_budget_exhausted')
            progress({'phase': 'original_volume', 'part': i, 'parts': len(parts)})
            one = download_one(record_id, progress, cancelled, secret,
                               {'locations': locations, 'publisher_routes': []}, '-part' + str(i), started + 1800)
            outputs.append({**one['file'], 'original_part_id': partid})
        manifest = save_bytes(json.dumps({'record_id': record_id, 'original_volumes': outputs, 'complete_volume_set': True}, ensure_ascii=False, indent=2).encode(),
                              'thesis-' + record_id[2:18] + '-volumes.json', 'application/json')
        return {'ok': True, 'record_id': record_id, 'files': outputs, 'manifest': manifest, 'complete_volume_set': True, 'space_saved': False}
    except Exception:
        for info in outputs:
            discard(info['file_id'])
        raise


def download(record_id, progress=lambda _: None, cancelled=lambda: False, secret=lambda _: ''):
    deadline = time.monotonic() + 1800
    try:
        return download_primary(record_id, progress, cancelled, secret)
    except SourceError as exc:
        # Never turn cancellation, exhausted budget, quota or safety rejection into
        # another network request. DOI articles only; no replacement of thesis volumes.
        if terminal_download_error(exc.code):
            raise
        from . import scihub
        record = store.get_record(record_id)
        if not scihub.eligible(record):
            raise
        if cancelled():
            raise SourceError('cancelled') from None
        if time.monotonic() >= deadline:
            raise SourceError('download_time_budget_exhausted') from None
        return scihub.download(record, progress, cancelled, deadline, exc.code)
