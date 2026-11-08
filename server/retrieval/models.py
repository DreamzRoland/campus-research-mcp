"""Conservative identity, source evidence and field provenance."""
import hashlib
import html
import re
import time
import unicodedata
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


def text(value, maximum=12000):
    if value is None:
        return ''
    if isinstance(value, list):
        value = ' '.join(text(v, maximum) for v in value)
    return html.unescape(re.sub('<[^>]*>', ' ', str(value))).strip()[:maximum]


def doi(value):
    v = str(value or '').strip()
    v = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', v, flags=re.I)
    return v.lower() if re.fullmatch(r'10\.\d{4,9}/[^\s\x00-\x1f]{1,250}', v) else ''


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2500 or '\\' in value or any(ord(c) < 33 for c in value):
        return ''
    try:
        p = urlsplit(value)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or getattr(p, 'pass' + 'word') or p.port not in (None, 80, 443):
            return ''
        sensitive = ('key', 'token', 'signature', 'authorization', 'credential', 'secret', 'password')
        if any(any(s in k.lower() for s in sensitive) for k, _ in parse_qsl(p.query)):
            return ''
        return urlunsplit((p.scheme, p.netloc, p.path, p.query, ''))
    except ValueError:
        return ''


def degree(value):
    v = text(value, 300).lower()
    if re.search(r'博士|doctoral|doctorat|ph\.?\s?d\.?|dphil', v):
        return 'doctoral'
    if re.search(r'硕士|碩士|master|msc|m\.sc|mphil', v):
        return 'masters'
    if re.search(r'bachelor|本科|学士|學士', v):
        return 'other'
    return 'unknown'


def make_record(provider, source_id, title, **fields):
    now = time.time()
    r = {'record_id': None, 'title': text(title, 2000), 'other_titles': [], 'authors': [],
         'identifiers': {}, 'type': 'unknown', 'year': None, 'language': None,
         'abstract': {'text': '', 'source': provider, 'status': 'missing'},
         'thesis': None, 'access': {'status': 'unknown', 'locations': [], 'license': None, 'embargo_end': None},
         'citations': {}, 'relations': [], 'evidence': [], 'field_sources': {},
         'source_records': {provider: {'id': str(source_id), 'retrieved_at': now}}}
    r.update(fields)
    r['title'] = text(r['title'], 2000)
    if r['identifiers'].get('doi'):
        r['identifiers']['doi'] = doi(r['identifiers']['doi'])
    r['identifiers'] = {k: str(v) for k, v in r['identifiers'].items() if v}
    r['access']['locations'] = [l for l in r['access'].get('locations', []) if safe_url(l.get('url'))]
    for field in ('title', 'authors', 'year', 'identifiers', 'abstract', 'thesis', 'access'):
        if r.get(field):
            r['field_sources'][field] = [provider]
    if r['type'] == 'thesis' and not r['thesis']:
        r['thesis'] = {'level': 'unknown', 'degree_name': '', 'institution': '', 'advisors': [], 'status': 'unknown'}
    return r


def identity(r):
    ids = r.get('identifiers', {})
    # Theses must not collapse into their derivative articles even if a source misassigns a DOI.
    prefix = 'thesis:' if r.get('type') == 'thesis' else 'work:'
    for kind in ('doi', 'pmid', 'arxiv', 'handle'):
        if ids.get(kind):
            value = doi(ids[kind]) if kind == 'doi' else ids[kind]
            if kind == 'arxiv':
                value = re.sub(r'v\d+$', '', value)
            if value:
                return prefix + kind + ':' + value
    source, meta = next(iter(r['source_records'].items()))
    return prefix + source + ':' + meta['id']


def merge_record(a, b):
    # Caller must have established exact identity. Preserve conflicts instead of inventing consensus.
    out = {**b, **a}
    for field in ('title', 'authors', 'year', 'language', 'venue', 'thesis'):
        owns_field = (set(a.get('field_sources', {}).get(field, []))
                      and set(a.get('field_sources', {}).get(field, [])).issubset(b.get('source_records', {})))
        if b.get(field) and (not out.get(field) or owns_field):
            out[field] = b[field]
    out['identifiers'] = {**b.get('identifiers', {}), **a.get('identifiers', {})}
    out['source_records'] = {**a.get('source_records', {}), **b.get('source_records', {})}
    out['field_sources'] = {k: sorted(set(a.get('field_sources', {}).get(k, []) + b.get('field_sources', {}).get(k, [])))
                           for k in set(a.get('field_sources', {})) | set(b.get('field_sources', {}))}
    out['abstract_variants'] = list(a.get('abstract_variants', []))
    if b.get('abstract', {}).get('text'):
        if not a.get('abstract', {}).get('text'):
            out['abstract'] = b['abstract']
        elif b['abstract'] != a['abstract'] and b['abstract'] not in out['abstract_variants']:
            out['abstract_variants'].append(b['abstract'])
    out['citations'] = {**a.get('citations', {}), **b.get('citations', {})}
    out['references_by_source'] = {**a.get('references_by_source', {}), **b.get('references_by_source', {})}
    out['access'] = dict(a.get('access', {}))
    locations = a.get('access', {}).get('locations', []) + b.get('access', {}).get('locations', [])
    out['access']['locations'] = list({(x['url'], x.get('role')): x for x in locations}.values())
    if b.get('access', {}).get('status') == 'open':
        out['access']['status'] = 'open'
    for field in ('relations', 'evidence', 'other_titles'):
        out[field] = list(a.get(field, []))
        for v in b.get(field, []):
            if v not in out[field]:
                out[field].append(v)
    manifestations = a.get('manifestations', []) + b.get('manifestations', [])
    for record in (a, b):
        for source, evidence in record.get('source_records', {}).items():
            item = {'source': source, 'source_id': evidence['id'], 'type': record.get('type'), 'version': record.get('version') or record.get('identifiers', {}).get('arxiv')}
            if item not in manifestations:
                manifestations.append(item)
    out['manifestations'] = manifestations
    if a.get('type') == 'preprint' and b.get('type') in ('article', 'conference'):
        out['type'] = b['type']
    return out


def normalized_title(title):
    return re.sub(r'[^\w]+', '', unicodedata.normalize('NFKC', title).casefold())


def candidate_relations(records):
    groups = {}
    for r in records:
        groups.setdefault(normalized_title(r['title']), []).append(r)
    for same in groups.values():
        if len(same) < 2:
            continue
        for a in same:
            for b in same:
                if a['record_id'] != b['record_id']:
                    relation = {'type': 'possible_same_title', 'record_id': b['record_id'], 'verified': False}
                    if relation not in a['relations']:
                        a['relations'].append(relation)
    return records
