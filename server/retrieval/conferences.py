"""Curated year/track registry plus public accepted collections and official proceedings."""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from . import models as m
from .providers import abstract, loc, response
from .transport import SourceError, public_endpoint

# Explicit publication editions, not guessed endpoints for future conference years.
EDITIONS = [
    ('icml', 2024, 'main', 'pmlr', 'https://proceedings.mlr.press/v235/'),
    ('aistats', 2024, 'main', 'pmlr', 'https://proceedings.mlr.press/v238/'),
    ('colt', 2024, 'main', 'pmlr', 'https://proceedings.mlr.press/v247/'),
    ('icml', 2025, 'main', 'pmlr', 'https://proceedings.mlr.press/v267/'),
    ('neurips', 2024, 'main', 'neurips', 'https://proceedings.neurips.cc/paper_files/paper/2024'),
    ('neurips', 2025, 'main', 'neurips', 'https://proceedings.neurips.cc/paper_files/paper/2025'),
    ('cvpr', 2024, 'main', 'cvf', 'https://openaccess.thecvf.com/CVPR2024?day=all'),
    ('cvpr', 2025, 'main', 'cvf', 'https://openaccess.thecvf.com/CVPR2025?day=all'),
    ('iccv', 2023, 'main', 'cvf', 'https://openaccess.thecvf.com/ICCV2023?day=all'),
    ('acl', 2024, 'main', 'acl', 'https://aclanthology.org/events/acl-2024/'),
    ('acl', 2025, 'main', 'acl', 'https://aclanthology.org/events/acl-2025/'),
    ('emnlp', 2024, 'main', 'acl', 'https://aclanthology.org/events/emnlp-2024/'),
    ('naacl', 2024, 'main', 'acl', 'https://aclanthology.org/events/naacl-2024/'),
]
VENUES = [(c, y, 'main', v) for y in (2023, 2024, 2025, 2026) for c, v in (
    ('iclr', f'ICLR.cc/{y}/Conference'), ('icml', f'ICML.cc/{y}/Conference'),
    ('neurips', f'NeurIPS.cc/{y}/Conference'))]


class Page(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.meta, self.links, self.parts = base, {}, [], []
        self.anchor, self.title_context, self.capture_title, self.capture_abstract = None, '', False, False
        self.title_parts, self.abstract_parts = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta':
            key = a.get('name') or a.get('property')
            if key and a.get('content'):
                self.meta.setdefault(key.lower(), []).append(a['content'])
        if tag == 'a' and a.get('href'):
            self.anchor = [urljoin(self.base, a['href']), [], self.title_context]
        if tag == 'p' and 'title' in a.get('class', '').split():
            self.capture_title = True
            self.title_parts = []
        if tag in ('div', 'blockquote') and (a.get('id') == 'abstract' or 'abstract' in a.get('class', '').split()):
            self.capture_abstract = True

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor[1].append(data)
        if self.capture_title:
            self.title_parts.append(data)
        if self.capture_abstract:
            self.abstract_parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            u, parts, context = self.anchor
            self.links.append((u, ' '.join(' '.join(parts).split()), context))
            self.anchor = None
        if tag == 'p' and self.capture_title:
            self.title_context = ' '.join(' '.join(self.title_parts).split())
            self.capture_title = False
        if tag in ('div', 'blockquote') and self.capture_abstract:
            self.capture_abstract = False


def paper_path(kind, url, year, conference):
    p = urlsplit(url)
    if kind == 'pmlr':
        return p.hostname == 'proceedings.mlr.press' and bool(re.fullmatch(r'/v\d+/[A-Za-z0-9_-]+\.html', p.path))
    if kind == 'neurips':
        return p.hostname in ('proceedings.neurips.cc', 'papers.nips.cc') and f'/paper/{year}/' in p.path and p.path.endswith('-Abstract-Conference.html')
    if kind == 'cvf':
        return p.hostname == 'openaccess.thecvf.com' and f'/content/{conference.upper()}{year}/html/' in p.path
    if kind == 'acl':
        return p.hostname == 'aclanthology.org' and bool(re.fullmatch('/' + str(year) + r'\.' + re.escape(conference) + r'-(?:long|short)\.\d+/', p.path))
    return False


def indexed_candidates(query, conference, year, offset, count, client):
    """Small indexed discovery, followed by mandatory official-page confirmation.

    Large CVF/ACL/NeurIPS annual pages can exceed a daily call's budget. This
    route does not claim the DBLP index covers the entire official proceedings.
    """
    import ai_search
    import types
    namespace = dict(ai_search.__dict__)
    namespace['_request'] = lambda source, params: json.dumps(client.get(source, 'https://sparql.dblp.org/sparql', params)).encode()
    call = types.FunctionType(ai_search.search_ai_conferences.__code__, namespace, argdefs=ai_search.search_ai_conferences.__defaults__)
    return call(query, conference, count, offset, year, year)


def metadata(url, client, edition=None):
    public_endpoint(url)
    html = client.get('proceedings', url, kind='html', ttl=7 * 86400)
    page = Page(url)
    page.feed(html)
    get = lambda k: (page.meta.get(k) or [''])[0]
    title = get('citation_title') or get('dc.title') or get('og:title')
    if not title:
        raise SourceError('official_paper_metadata_missing')
    pdf = get('citation_pdf_url')
    if not pdf:
        pdf = next((u for u, label, _ in page.links if label.lower() in ('pdf', 'download pdf') and ('.pdf' in u or '/pdf?' in u)), '')
    c, y, track, kind, index = edition if edition else ('', None, 'main', '', url)
    r = m.make_record('proceedings', url, title, type='conference',
        authors=page.meta.get('citation_author', []), year=y or __import__('retrieval.providers', fromlist=['year']).year(get('citation_publication_date') or get('citation_date')),
        identifiers={'doi': get('citation_doi'), 'official_url': url},
        abstract=abstract(get('description') or get('dc.description') or ' '.join(page.abstract_parts), 'proceedings'),
        venue=c, track=track, access={'status': 'open', 'locations': loc(url, 'proceedings') + loc(pdf, 'proceedings', 'pdf', linked_from=url), 'license': None, 'embargo_end': None},
        evidence=[{'source': 'proceedings', 'kind': 'official_publication', 'url': url, 'edition_url': index,
                   'conference': c, 'year': y, 'track': track, 'conference_acceptance_verified': True}])
    return r


def selected(f, registry):
    codes = f.get('conferences', 'neurips,icml,iclr').lower().split(',')
    if codes == ['all']:
        import ai_search
        codes = list(ai_search.CONFERENCES)
    import ai_search
    if any(c not in ai_search.CONFERENCES for c in codes):
        raise SourceError('unknown_conference')
    chosen = [v for v in registry if v[0] in codes and f.get('from_year', 1000) <= v[1] <= f.get('to_year', 2100)
              and v[2] == f.get('track', 'main')]
    # Latest editions first. A missing year is a coverage gap, not an empty successful search.
    return sorted(chosen, key=lambda e: (-e[1], codes.index(e[0])))


def search(provider, query, f, cursor, count, client):
    registry = selected(f, VENUES if provider == 'openreview' else EDITIONS)
    if not registry:
        raise SourceError('conference_year_track_not_registered')
    pos = json.loads(cursor) if cursor else {'edition': 0, 'offset': 0}
    idx, offset = pos['edition'], pos['offset']
    if type(idx) is not int or type(offset) is not int or idx < 0 or offset < 0:
        raise SourceError('invalid_conference_cursor')
    if idx >= len(registry):
        return response([], coverage={'editions': [list(v[:3]) for v in registry]})
    edition = registry[idx]
    out = []
    terms = [t.strip('"').casefold() for t in re.findall(r'"[^"]+"|\S+', query) if t.upper() != 'AND']
    if provider == 'openreview':
        c, y, track, venue = edition
        # Server-side accepted venue filter + local evidence check. All submissions is never used.
        batch = max(50, count)
        x = client.get(provider, 'https://api2.openreview.net/notes/search',
                       {'term': query, 'group': venue, 'source': 'forum', 'limit': batch, 'offset': offset}, ttl=86400)
        notes = x.get('notes', [])
        consumed = 0
        for a in notes:
            consumed += 1
            content = {k: v.get('value') if isinstance(v, dict) else v for k, v in a.get('content', {}).items()}
            if content.get('venueid') != venue:
                continue
            title = content.get('title') or ''
            if not all(t in (title + ' ' + str(content.get('abstract') or '')).casefold() for t in terms):
                continue
            identifier = a['id']
            pdf = content.get('pdf')
            pdf = 'https://openreview.net/pdf?id=' + identifier if pdf else ''
            out.append(m.make_record(provider, identifier, title, type='conference',
                authors=content.get('authors') or [], year=y, venue=c, track=track,
                identifiers={'openreview': identifier, 'doi': content.get('doi')}, abstract=abstract(content.get('abstract'), provider),
                access={'status': 'open', 'locations': loc('https://openreview.net/forum?id=' + identifier, provider) + loc(pdf, provider, 'pdf'), 'license': content.get('license'), 'embargo_end': None},
                evidence=[{'kind': 'official_accepted_collection', 'source': provider, 'venueid': venue, 'conference_acceptance_verified': True}]))
            if len(out) >= count:
                break
        more = consumed < len(notes) or len(notes) == batch
        nextpos = {'edition': idx, 'offset': offset + consumed} if more else {'edition': idx + 1, 'offset': 0}
    elif edition[3] in ('neurips', 'acl'):
        c, y, track, kind, url = edition
        found = indexed_candidates(query, c, y, offset, count, client)
        for candidate in found.get('records', []):
            links = [u.replace('http://', 'https://', 1) for u in candidate.get('official_links', [])]
            d = (candidate.get('doi') or '').lower()
            if kind == 'acl' and d.startswith('10.18653/v1/'):
                links.append('https://aclanthology.org/' + d.split('/v1/', 1)[1] + '/')
            official = next((u for u in links if paper_path(kind, u, y, c)), None)
            if not official:
                continue
            r = metadata(official, client, edition)
            if m.normalized_title(r['title']) != m.normalized_title(candidate['title']):
                raise SourceError('official_page_title_identity_mismatch')
            out.append(r)
        nextpos = {'edition': idx, 'offset': offset + count} if found.get('has_more') else {'edition': idx + 1, 'offset': 0}
    else:
        c, y, track, kind, url = edition
        html = client.get(provider, url, kind='html', ttl=7 * 86400)
        page = Page(url)
        page.feed(html)
        entries = {}
        for link, label, context in page.links:
            if not paper_path(kind, link, y, c):
                continue
            title = context if kind == 'pmlr' and label.lower() == 'abs' else label
            if title and all(t in title.casefold() for t in terms):
                entries[link] = title
        matches = list(entries.items())
        for link, _ in matches[offset:offset + count]:
            out.append(metadata(link, client, edition))
        more = offset + count < len(matches)
        nextpos = {'edition': idx, 'offset': offset + count} if more else {'edition': idx + 1, 'offset': 0}
    token = json.dumps(nextpos) if nextpos['edition'] < len(registry) else None
    return response(out, cursor=token, coverage={'editions': [list(v[:3]) for v in registry],
                    'searched_edition': list(edition[:3]), 'matching_scope': 'title' if provider == 'proceedings' else 'public_title_and_abstract',
                    'registry_is_exhaustive': False,
                    'discovery_source': 'dblp_then_official_confirmation' if provider == 'proceedings' and edition[3] in ('neurips','acl') else 'official_collection'})
