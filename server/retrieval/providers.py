"""Official-source adapters. Fulltext URL discovery never implies download rights."""
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlsplit
from . import models as m, store
from .query import compile_query
from .transport import SourceError

KEYS = {'scopus': 'SCOPUS_API_KEY', 'wos': 'WOS_API_KEY', 'springer': 'SPRINGER_META_API_KEY',
        'core': 'CORE_API_KEY', 'ieee': 'IEEE_API_KEY', 'semanticscholar': 'SEMANTIC_SCHOLAR_API_KEY',
        'wanfang': 'WFDATA_APP_KEY'}
CATALOG = {
    'scopus': ('Scopus', 'https://www.scopus.com', 'key', 'bibliography,abstract_if_authorized'),
    'wos': ('Web of Science Starter', 'https://www.webofscience.com', 'key', 'bibliography,no_abstract'),
    'crossref': ('Crossref', 'https://search.crossref.org', 'public', 'bibliography,available_abstract,references'),
    'openalex': ('OpenAlex', 'https://openalex.org', 'public', 'bibliography,available_abstract,citations,oa_locations'),
    'datacite': ('DataCite', 'https://commons.datacite.org', 'public', 'bibliography,theses,available_abstract'),
    'openaire': ('OpenAIRE', 'https://explore.openaire.eu', 'public', 'bibliography,theses,available_abstract,oa_locations'),
    'core': ('CORE', 'https://core.ac.uk', 'key', 'repository_metadata,theses_if_classified,oa_locations'),
    'thesesfr': ('theses.fr', 'https://theses.fr', 'public', 'doctoral_theses,public_metadata'),
    'wanfang': ('万方 OpenThesis', 'https://s.wanfangdata.com.cn/thesis', 'key', 'theses,available_abstract,no_verified_pdf_api'),
    'cnki': ('知网博硕士论文', 'https://kns.cnki.net/kns8?dbcode=CDMD', 'institutional', 'official_link,reference_import'),
    'cqvip': ('维普学位论文', 'https://www.cqvip.com/degree/search', 'institutional', 'official_link,reference_import'),
    'proquest': ('ProQuest Dissertations & Theses', 'https://www.proquest.com', 'institutional', 'official_link,reference_import'),
    'oatd': ('OATD', 'https://oatd.org', 'link', 'official_link'),
    'ndltd': ('NDLTD', 'https://ndltd.org', 'link', 'official_link'),
    'ethos': ('EThOS', 'https://www.bl.uk', 'link', 'official_link,availability_requires_verification'),
    'arxiv': ('arXiv', 'https://arxiv.org', 'public', 'preprints,abstract,versions,pdf'),
    'dblp': ('DBLP', 'https://dblp.org', 'public', 'conference_index,no_abstract'),
    'openreview': ('OpenReview', 'https://openreview.net', 'public', 'accepted_public_collections,abstract,pdf'),
    'proceedings': ('官方会议论文集', 'https://proceedings.mlr.press', 'public', 'publication_evidence,abstract,pdf'),
    'pubmed': ('PubMed', 'https://pubmed.ncbi.nlm.nih.gov', 'public', 'biomedical_metadata,available_abstract'),
    'europepmc': ('Europe PMC', 'https://europepmc.org', 'public', 'biomedical_metadata,available_abstract,oa_locations'),
    'ieee': ('IEEE Xplore', 'https://ieeexplore.ieee.org', 'key', 'metadata,available_abstract,separate_fulltext_rights'),
    'semanticscholar': ('Semantic Scholar', 'https://www.semanticscholar.org', 'key', 'metadata,citations,related_papers'),
    'repositories': ('机构仓储本地索引', '', 'index', 'harvested_theses,coverage_report'),
    'mit': ('MIT DSpace', 'https://dspace.mit.edu', 'harvest', 'oai_metadata,pdf_if_verified'),
    'caltech': ('CaltechTHESIS', 'https://thesis.caltech.edu', 'harvest', 'oai_metadata,pdf_if_verified'),
    'hkust': ('HKUST Repository', 'https://repository.hkust.edu.hk', 'harvest', 'oai_metadata,pdf_if_verified'),
    'springer': ('Springer Nature Meta', 'https://link.springer.com', 'key', 'metadata,available_abstract'),
}


def catalogue(secret):
    result = []
    for code, (name, url, auth, caps) in CATALOG.items():
        key = KEYS.get(code)
        state = ('needs_key' if key and not secret(key) else 'needs_institutional_api' if auth == 'institutional'
                 else 'link_only' if auth == 'link' else 'needs_harvest_verification' if auth == 'harvest' else 'ready')
        evidence = store.capability(code)
        if state == 'ready' and evidence and evidence.get('status') == 'temporarily_unavailable':
            state = 'temporarily_unavailable'
        result.append({'id': code, 'name': name, 'url': url, 'status': state,
                       'queryable': auth not in ('institutional', 'link', 'harvest') and (not key or bool(secret(key))),
                       'capabilities': caps.split(','), 'verification': evidence,
                       'institutional_access_confirmed': bool((evidence or {}).get('institutional_access_confirmed', False))})
    return result


def year(value):
    found = re.search(r'(?:18|19|20)\d{2}', str(value or ''))
    return int(found[0]) if found else None


def abstract(value, source):
    raw = str(value or '')
    return {'text': m.text(raw), 'source': source, 'status': 'truncated' if len(raw) > 12000 else 'available' if raw else 'missing'}


def loc(url, provider, role='landing', **extra):
    url = m.safe_url(url)
    return [{'url': url, 'source': provider, 'role': role, **extra}] if url else []


def thesis(name='', institution='', advisors=None, status='unknown', **extra):
    return {'level': m.degree(name), 'degree_name': m.text(name, 300), 'institution': m.text(institution, 1000),
            'advisors': advisors or [], 'status': status, **extra}


def normalize_openalex(x):
    positions = {}
    for word, indexes in (x.get('abstract_inverted_index') or {}).items():
        for i in indexes[:2000]:
            if type(i) is int and 0 <= i < 30000:
                positions[i] = word
    a = ' '.join(positions[i] for i in sorted(positions))
    places = []
    for l in x.get('locations', [])[:30]:
        places += loc(l.get('landing_page_url'), 'openalex', version=l.get('version'))
        places += loc(l.get('pdf_url'), 'openalex', 'pdf', version=l.get('version'), open=l.get('is_oa', False))
    ids = {'doi': m.doi(x.get('doi')), 'openalex': str(x.get('id', '')).rsplit('/', 1)[-1]}
    for k, v in (x.get('ids') or {}).items():
        if k == 'pmid' and v:
            ids['pmid'] = str(v).rsplit('/', 1)[-1]
    typ = {'dissertation': 'thesis', 'preprint': 'preprint', 'conference-paper': 'conference', 'article': 'article'}.get(x.get('type'), x.get('type', 'unknown'))
    r = m.make_record('openalex', ids['openalex'], x.get('title'), identifiers=ids, type=typ,
                      authors=[a.get('author', {}).get('display_name', '') for a in x.get('authorships', [])[:100]],
                      year=x.get('publication_year'), language=x.get('language'), abstract=abstract(a, 'openalex'),
                      venue=((x.get('primary_location') or {}).get('source') or {}).get('display_name'),
                      citations={'openalex': {'count': x.get('cited_by_count'), 'retrieved_at': __import__('time').time()}},
                      access={'status': 'open' if (x.get('open_access') or {}).get('is_oa') else 'unknown', 'locations': places,
                              'license': (x.get('best_oa_location') or {}).get('license'), 'embargo_end': None})
    r['references'] = [{'provider': 'openalex', 'id': v.rsplit('/', 1)[-1]} for v in x.get('referenced_works', [])[:200]]
    r['references_by_source'] = {'openalex': r['references']}
    return r


def normalize_datacite(x):
    a = x.get('attributes', x)
    descriptions = [d.get('description', '') for d in a.get('descriptions', []) if d.get('descriptionType') == 'Abstract']
    typ = a.get('types') or {}
    is_thesis = typ.get('resourceTypeGeneral') == 'Dissertation'
    name = typ.get('resourceType') or ''
    titles = a.get('titles') or [{}]
    rights = a.get('rightsList') or []
    pub = a.get('publisher') or ''
    institution = pub.get('name', '') if isinstance(pub, dict) else pub
    r = m.make_record('datacite', a.get('doi') or x.get('id'), titles[0].get('title'),
         identifiers={'doi': a.get('doi') or x.get('id')}, other_titles=[d.get('title') for d in titles[1:]],
         type='thesis' if is_thesis else str(typ.get('resourceTypeGeneral', 'unknown')).lower(),
         authors=[v.get('name', '') for v in a.get('creators', [])[:100]], year=year(a.get('publicationYear')),
         language=a.get('language'), abstract=abstract('\n'.join(descriptions), 'datacite'),
         thesis=thesis(name, institution, dates=a.get('dates', []), institution_role='publisher_not_verified_awarding_body') if is_thesis else None,
         access={'status': 'unknown', 'locations': loc(a.get('url'), 'datacite'), 'license': rights[0].get('rightsUri') if rights else None, 'embargo_end': None})
    r['relations'] = [{'type': t.get('relationType'), 'identifier': t.get('relatedIdentifier'), 'identifier_type': t.get('relatedIdentifierType'), 'verified': True, 'source': 'datacite'} for t in a.get('relatedIdentifiers', [])[:100]]
    urls = a.get('contentUrl') or []
    if isinstance(urls, str):
        urls = [urls]
    for url in urls[:20]:
        r['access']['locations'] += loc(url, 'datacite', 'pdf' if urlsplit(url).path.lower().endswith('.pdf') else 'content', linked_from='https://doi.org/' + str(a.get('doi') or x.get('id')))
    # The deposit's publisher is not necessarily the degree-granting institution.
    if r.get('thesis'):
        r['thesis']['repository_publisher'] = r['thesis']['institution']
        r['thesis']['institution'] = ''
    return r


def normalize_openaire(x):
    instances = x.get('instances') or []
    names = [v.get('type', '') for v in instances]
    thesis_names = [n for n in names if 'thesis' in n.lower()]
    levels = {m.degree(n) for n in thesis_names} - {'unknown'}
    name = next((n for n in thesis_names if m.degree(n) != 'unknown'), 'Thesis')
    identifiers = {}
    for v in (x.get('pids') or []) + (x.get('originalIds') or []):
        if isinstance(v, dict):
            scheme = str(v.get('scheme') or v.get('type') or '').lower()
            value = v.get('value') or v.get('identifier')
            if scheme in ('doi', 'pmid', 'arxiv', 'handle') and value:
                identifiers[scheme] = value
    # V4 uses pid objects and may also expose id arrays depending on sparse field selection.
    for k in ('doi', 'pmid', 'arxiv', 'handle'):
        ids = x.get('ids') or {}
        if isinstance(ids, dict) and ids.get(k):
            identifiers[k] = ids[k][0] if isinstance(ids[k], list) else ids[k]
    locations = []
    for v in instances:
        for u in (v.get('urls') or []):
            locations += loc(u, 'openaire', 'pdf' if urlsplit(u).path.lower().endswith('.pdf') else 'landing',
                             repository=(v.get('hostedBy') or {}).get('value'))
    t = thesis(name, classification_evidence=names, classification_verified_at_origin=False) if thesis_names else None
    if t and len(levels) > 1:
        t['level'] = 'unknown'
        t['classification_conflict'] = True
    language = x.get('language') or {}
    access = x.get('bestAccessRight') or {}
    rights = access.get('label') if isinstance(access, dict) else str(access)
    return m.make_record('openaire', x['id'], x.get('mainTitle'), identifiers=identifiers,
        authors=[a.get('fullName', '') for a in (x.get('authors') or [])[:100]], year=year(x.get('publicationDate')),
        language=language.get('code') if isinstance(language, dict) else language,
        type='thesis' if t else x.get('type', 'unknown'), thesis=t,
        abstract=abstract('\n'.join(x.get('descriptions') or []), 'openaire'),
        access={'status': 'open' if rights == 'Open Access' else 'unknown', 'locations': locations, 'license': None, 'embargo_end': None})


def normalize_crossref(x):
    dates = (x.get('issued') or {}).get('date-parts') or [[]]
    typ = 'thesis' if x.get('type') == 'dissertation' else 'conference' if x.get('type') == 'proceedings-article' else 'article'
    locations = loc(x.get('URL'), 'crossref')
    for l in x.get('link', []):
        locations += loc(l.get('URL'), 'crossref', 'pdf' if l.get('content-type') == 'application/pdf' else 'content', content_version=l.get('content-version'))
    r = m.make_record('crossref', x.get('DOI'), (x.get('title') or [''])[0], identifiers={'doi': x.get('DOI')},
        type=typ, year=dates[0][0] if dates[0] else None,
        authors=[' '.join(filter(None, (a.get('given'), a.get('family')))) or a.get('name', '') for a in x.get('author', [])[:100]],
        venue=(x.get('container-title') or [''])[0], publisher=x.get('publisher'), abstract=abstract(x.get('abstract'), 'crossref'),
        citations={'crossref': {'count': x.get('is-referenced-by-count')}},
        thesis=thesis(x.get('degree', ''), x.get('publisher', ''), institution_role='publisher_not_verified_awarding_body') if typ == 'thesis' else None,
        access={'status': 'unknown', 'locations': locations, 'license': None, 'embargo_end': None})
    r['references'] = [{'doi': m.doi(v.get('DOI')), 'unstructured': m.text(v.get('unstructured'), 500)} for v in x.get('reference', [])[:200]]
    r['references_by_source'] = {'crossref': r['references']}
    return r


def normalize_thesesfr(x):
    name = lambda a: ' '.join(filter(None, (a.get('prenom'), a.get('nom'))))
    date = x.get('dateSoutenance') or ''
    return m.make_record('thesesfr', x.get('nnt') or x.get('id'), x.get('titrePrincipal'),
        identifiers={'doi': x.get('doi'), 'nnt': x.get('nnt') or x.get('id')}, type='thesis',
        authors=[name(a) for a in x.get('auteurs', [])], year=year(date),
        other_titles=[x['titreEN']] if x.get('titreEN') else [],
        thesis=thesis('Doctorat', x.get('etabSoutenanceN', ''), [name(a) for a in x.get('directeurs', [])],
                      'awarded' if x.get('status') == 'soutenue' else 'in_progress', defense_date=date, discipline=x.get('discipline')),
        abstract=abstract(x.get('resume') or x.get('resumeFR') or x.get('resumeEN'), 'thesesfr'),
        access={'status': 'unknown', 'locations': loc('https://theses.fr/' + str(x.get('nnt') or x.get('id')), 'thesesfr'), 'license': None, 'embargo_end': None})


def normalize_core(x, entity='outputs'):
    types = x.get('documentType') or x.get('types') or []
    label = ' '.join(types) if isinstance(types, list) else str(types)
    is_thesis = bool(re.search(r'thesis|dissertation|学位', label, re.I))
    language = x.get('language')
    if isinstance(language, dict):
        language = language.get('code')
    source_id = ('work:' if entity == 'works' else '') + str(x.get('id'))
    identifiers = {'doi': x.get('doi'), 'core_work' if entity == 'works' else 'core_output': str(x.get('id'))}
    r = m.make_record('core', source_id, x.get('title'), identifiers=identifiers,
        type='thesis' if is_thesis else 'unknown',
        thesis=thesis(label) if is_thesis else None, year=year(x.get('yearPublished')), language=language,
        authors=[a.get('name', '') if isinstance(a, dict) else a for a in (x.get('authors') or [])[:100]],
        abstract=abstract(x.get('abstract'), 'core'),
        access={'status': 'unknown', 'locations': loc(x.get('downloadUrl'), 'core', 'pdf') + sum([loc(u, 'core') for u in (x.get('sourceFulltextUrls') or [])[:10]], []), 'license': None, 'embargo_end': None})
    r['source_records']['core']['entity'] = entity
    if is_thesis:
        r['thesis'].update(classification_evidence=[label], classification_verified_at_origin=False)
    return r


def normalize_epmc(x):
    locations = []
    for l in (x.get('fullTextUrlList') or {}).get('fullTextUrl', []):
        locations += loc(l.get('url'), 'europepmc', 'pdf' if l.get('documentStyle') == 'pdf' else 'landing', open=l.get('availabilityCode') == 'OA')
    return m.make_record('europepmc', x.get('id'), x.get('title'),
        identifiers={'doi': x.get('doi'), 'pmid': x.get('pmid'), 'pmcid': x.get('pmcid')}, type='article',
        authors=[a.get('fullName', '') for a in (x.get('authorList') or {}).get('author', [])[:100]],
        year=year(x.get('pubYear')), language=x.get('language'), abstract=abstract(x.get('abstractText'), 'europepmc'),
        citations={'europepmc': {'count': x.get('citedByCount')}},
        access={'status': 'open' if x.get('isOpenAccess') == 'Y' else 'unknown', 'locations': locations, 'license': x.get('license'), 'embargo_end': None})


def normalize_existing(provider, x):
    identifiers = {'doi': x.get('doi')}
    for k in ('eid', 'uid', 'arxiv_id'):
        if x.get(k):
            identifiers['arxiv' if k == 'arxiv_id' else k] = x[k]
    a = x.get('abstract') or ''
    if isinstance(a, dict):
        a = a.get('text', '')
    return m.make_record(provider, x.get('identifier') or x.get('id') or x.get('eid') or x.get('uid') or x.get('doi') or x.get('title'),
        x.get('title'), identifiers=identifiers, authors=x.get('authors') or ([x['first_author']] if x.get('first_author') else []),
        year=x.get('year') or year(x.get('date') or x.get('publicationDate') or x.get('published')), language=x.get('language'),
        type='preprint' if provider == 'arxiv' else 'conference' if provider == 'dblp' else 'article',
        abstract=abstract(a, provider), venue=x.get('venue') or x.get('source') or x.get('publicationName'),
        track='main' if provider == 'dblp' else None,
        access={'status': 'open' if provider == 'arxiv' else 'unknown',
                'locations': loc(x.get('record_url') or x.get('abs_url') or x.get('abstract_url'), provider) + loc(x.get('pdf_url'), provider, 'pdf'), 'license': None, 'embargo_end': None},
        evidence=[{'source': provider, 'kind': 'indexed_membership', 'conference_acceptance_verified': False}] if provider == 'dblp' else [])


def response(records, total=None, cursor=None, **extra):
    return {'records': records, 'total': total, 'next_cursor': cursor, 'has_more': cursor is not None, **extra}


def search(provider, tree, f, cursor, count, client, secret, thesis_only=False):
    q = compile_query(tree, provider)
    cursor = cursor or ''
    if provider in KEYS and not secret(KEYS[provider]):
        raise SourceError('needs_key')
    if provider in ('openreview', 'proceedings'):
        from .conferences import search as conference_search
        return conference_search(provider, q, f, cursor, count, client)
    if provider == 'repositories':
        from .repositories import search_index
        return search_index(q, count, cursor)
    if f.get('conferences') and provider not in ('dblp', 'openreview', 'proceedings'):
        raise SourceError('source_conference_filter_unsupported')
    offset = int(cursor or 0) if provider not in ('openalex', 'openaire', 'europepmc') else 0
    if provider == 'openalex':
        if cursor and cursor != '*' and not secret('OPENALEX_API_KEY'):
            raise SourceError('pagination_needs_openalex_key')
        filters = []
        if thesis_only:
            filters.append('type:dissertation')
        if f.get('from_year'):
            filters.append('from_publication_date:' + str(f['from_year']) + '-01-01')
        if f.get('to_year'):
            filters.append('to_publication_date:' + str(f['to_year']) + '-12-31')
        params = {'search': q, 'per-page': count, 'cursor': cursor or '*'}
        if filters:
            params['filter'] = ','.join(filters)
        if secret('OPENALEX_API_KEY'):
            params['api_key'] = secret('OPENALEX_API_KEY')
        x = client.get(provider, 'https://api.openalex.org/works', params, secret_values=(secret('OPENALEX_API_KEY'),))
        return response([normalize_openalex(a) for a in x['results']], x['meta'].get('count'), x.get('meta', {}).get('next_cursor') if x['results'] else None)
    if provider == 'datacite':
        if thesis_only:
            q = '(' + q + ') AND types.resourceTypeGeneral:Dissertation'
        if f.get('from_year') or f.get('to_year'):
            q += ' AND publicationYear:[' + str(f.get('from_year', 1000)) + ' TO ' + str(f.get('to_year', 2100)) + ']'
        x = client.get(provider, 'https://api.datacite.org/dois', {'query': q, 'page[size]': count, 'page[number]': offset // count + 1})
        total = x.get('meta', {}).get('total')
        return response([normalize_datacite(a) for a in x['data']], total, str(offset + count) if x.get('links', {}).get('next') and x['data'] else None)
    if provider == 'openaire':
        fil = ['type:publication']
        if thesis_only:
            levels = {'masters': 'Master thesis', 'doctoral': 'Doctoral thesis', 'other': 'Bachelor thesis', 'unknown': 'Thesis'}
            fil.append('instance_type:' + levels.get(f.get('degree'), 'Doctoral thesis|Master thesis|Thesis'))
        for k in ('from_year', 'to_year'):
            if f.get(k):
                fil.append(k.replace('_year', '_publication_year') + ':' + str(f[k]))
        x = client.get(provider, 'https://api.openaire.eu/graph/v4/research-products', {'search': q, 'filter': ','.join(fil), 'page_size': count, 'cursor': cursor or '*'})
        return response([normalize_openaire(a) for a in x['results']], x['header'].get('numFound'), x['header'].get('nextCursor') if x['results'] else None)
    if provider == 'crossref':
        p = {'query.bibliographic': q, 'rows': count, 'offset': offset}
        fs = []
        if f.get('from_year'):
            fs.append('from-pub-date:' + str(f['from_year']))
        if f.get('to_year'):
            fs.append('until-pub-date:' + str(f['to_year']))
        if thesis_only:
            fs.append('type:dissertation')
        if fs:
            p['filter'] = ','.join(fs)
        x = client.get(provider, 'https://api.crossref.org/works', p)['message']
        return response([normalize_crossref(a) for a in x['items']], x.get('total-results'), str(offset + count) if offset + count < x.get('total-results', 0) and x['items'] else None)
    if provider == 'thesesfr':
        x = client.get(provider, 'https://theses.fr/api/v1/theses/recherche/', {'q': q, 'nombre': count, 'debut': offset})
        xs = x.get('theses', [])
        return response([normalize_thesesfr(a) for a in xs], x.get('totalHits'), str(offset + count) if offset + count < x.get('totalHits', 0) and xs else None)
    if provider == 'core':
        # outputs.documentType is a collection; its scalar query translation raises
        # Azure filter errors. works.documentType is scalar and verified filterable.
        entity = 'works' if thesis_only else 'outputs'
        if thesis_only:
            q = '(' + q + ') AND (documentType:thesis OR documentType:dissertation)'
        x = client.get(provider, 'https://api.core.ac.uk/v3/search/' + entity + '/', {'q': q, 'limit': count, 'offset': offset}, {'Authorization': 'Bearer ' + secret('CORE_API_KEY')}, secret_values=(secret('CORE_API_KEY'),))
        xs = x.get('results', [])
        return response([normalize_core(a, entity) for a in xs], x.get('totalHits'), str(offset + count) if xs and offset + count < x.get('totalHits', 0) else None,
                        upstream_entity=entity, type_filter_mode='server_side' if thesis_only else 'not_requested')
    if provider == 'wanfang':
        x = client.get(provider, 'https://api.wfdata.com/openwanfang/getQuery', headers={'X-App-Key': secret('WFDATA_APP_KEY')},
            payload={'collections': ['OpenThesis'], 'query': q, 'start': offset, 'rows': count}, secret_values=(secret('WFDATA_APP_KEY'),))
        xs = []
        for item in x.get('documents', []):
            a = item.get('fields') or item
            scalar = lambda k: m.text(a.get(k), 2000)
            identifier = scalar('Id')
            xs.append(m.make_record(provider, identifier, scalar('Title'), type='thesis', year=year(scalar('PublishYear')),
                identifiers={'wanfang': identifier, 'doi': scalar('DOI')}, authors=a.get('Creator') if isinstance(a.get('Creator'), list) else [scalar('Creator')],
                abstract=abstract(a.get('Abstract'), provider), thesis=thesis(scalar('Degree'), scalar('OriginalOrganization'), a.get('Tutor') or []),
                access={'status': 'unknown', 'locations': loc('https://d.wanfangdata.com.cn/thesis/' + quote(identifier, safe=''), provider), 'license': None, 'embargo_end': None}))
        return response(xs, x.get('num_found'), str(offset + count) if xs and offset + count < x.get('num_found', 0) else None)
    if provider == 'europepmc':
        if f.get('from_year') or f.get('to_year'):
            q += ' AND FIRST_PDATE:[' + str(f.get('from_year', 1000)) + '-01-01 TO ' + str(f.get('to_year', 2100)) + '-12-31]'
        x = client.get(provider, 'https://www.ebi.ac.uk/europepmc/webservices/rest/search', {'query': q, 'format': 'json', 'resultType': 'core', 'pageSize': count, 'cursorMark': cursor or '*'})
        return response([normalize_epmc(a) for a in x.get('resultList', {}).get('result', [])], x.get('hitCount'), x.get('nextCursorMark') if x.get('nextCursorMark') != (cursor or '*') else None)
    if provider == 'pubmed':
        if f.get('from_year') or f.get('to_year'):
            q += ' AND ' + str(f.get('from_year', 1000)) + ':' + str(f.get('to_year', 2100)) + '[dp]'
        x = client.get(provider, 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi', {'db': 'pubmed', 'retmode': 'json', 'term': q, 'retmax': count, 'retstart': offset})['esearchresult']
        ids = x.get('idlist', [])
        records = []
        if ids:
            xml = client.get(provider, 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi', {'db': 'pubmed', 'retmode': 'xml', 'id': ','.join(ids)}, kind='xml')
            # NCBI's external public DTD is inert with ElementTree; transport rejects any declaration.
            root = ET.fromstring(xml)
            for a in root.findall('.//PubmedArticle'):
                pmid = a.findtext('.//PMID')
                record = m.make_record(provider, pmid, ''.join(a.find('.//ArticleTitle').itertext()) if a.find('.//ArticleTitle') is not None else '',
                    identifiers={'pmid': pmid, 'doi': next((e.text for e in a.findall('.//ArticleId') if e.get('IdType') == 'doi'), '')},
                    authors=[' '.join(filter(None, (e.findtext('ForeName'), e.findtext('LastName')))) for e in a.findall('.//Author')],
                    type='article', year=year(a.findtext('.//PubDate/Year') or a.findtext('.//PubDate/MedlineDate')), language=a.findtext('.//Language'),
                    abstract=abstract(' '.join(''.join(e.itertext()) for e in a.findall('.//AbstractText')), provider),
                    access={'status': 'unknown', 'locations': loc('https://pubmed.ncbi.nlm.nih.gov/' + str(pmid) + '/', provider), 'license': None, 'embargo_end': None})
                records.append(record)
        return response(records, int(x.get('count', 0)), str(offset + count) if offset + count < int(x.get('count', 0)) else None)
    if provider in ('arxiv', 'dblp'):
        import ai_search
        if provider == 'arxiv':
            # Use the shared client for network/rate/budget; reuse the existing safe Atom parser.
            x = client.get(provider, 'https://export.arxiv.org/api/query', {'search_query': q, 'start': offset, 'max_results': count}, kind='xml')
            root = ET.fromstring(x)
            ns = {'a': 'http://www.w3.org/2005/Atom', 'o': 'http://a9.com/-/spec/opensearch/1.1/'}
            records = []
            for a in root.findall('a:entry', ns):
                aid = a.findtext('a:id', '', ns).split('/abs/')[-1]
                if aid == 'http://arxiv.org/api/errors':
                    raise SourceError('arxiv_query_error')
                r = m.make_record(provider, aid, a.findtext('a:title', '', ns), type='preprint', identifiers={'arxiv': aid, 'doi': a.findtext('{http://arxiv.org/schemas/atom}doi')},
                    authors=[e.findtext('a:name', '', ns) for e in a.findall('a:author', ns)], year=year(a.findtext('a:published', '', ns)),
                    abstract=abstract(a.findtext('a:summary', '', ns), provider),
                    access={'status': 'open', 'locations': loc('https://arxiv.org/abs/' + aid, provider) + loc('https://arxiv.org/pdf/' + aid, provider, 'pdf'), 'license': None, 'embargo_end': None})
                r['version'] = aid
                records.append(r)
            total = int(root.findtext('o:totalResults', '0', ns))
            return response(records, total, str(offset + count) if offset + count < total else None)
        # Isolate this invocation's transport; never monkey-patch the shared module.
        import types
        namespace = dict(ai_search.__dict__)
        namespace['_request'] = lambda source, params: __import__('json').dumps(client.get(source, 'https://sparql.dblp.org/sparql', params)).encode()
        call = types.FunctionType(ai_search.search_ai_conferences.__code__, namespace, argdefs=ai_search.search_ai_conferences.__defaults__)
        x = call(q, f.get('conferences', 'neurips,icml,iclr'), count, offset, f.get('from_year', 0), f.get('to_year', 0))
        return response([normalize_existing(provider, a) for a in x.get('records', [])], x.get('total_results'), str(offset + count) if x.get('has_more') else None)
    if provider == 'scopus':
        if f.get('from_year'):
            q += ' AND PUBYEAR > ' + str(f['from_year'] - 1)
        if f.get('to_year'):
            q += ' AND PUBYEAR < ' + str(f['to_year'] + 1)
        headers = {'X-ELS-APIKey': secret('SCOPUS_API_KEY')}
        if secret('ELSEVIER_INST_TOKEN'):
            headers['X-ELS-Insttoken'] = secret('ELSEVIER_INST_TOKEN')
        x = client.get(provider, 'https://api.elsevier.com/content/search/scopus', {'query': q, 'count': count, 'start': offset, 'view': 'STANDARD'}, headers,
            secret_values=(secret('SCOPUS_API_KEY'), secret('ELSEVIER_INST_TOKEN')))['search-results']
        import server as legacy
        xs = [normalize_existing(provider, legacy.record(a)) for a in x.get('entry', []) if 'error' not in a]
        total = int(x.get('opensearch:totalResults', 0))
        return response(xs, total, str(offset + count) if xs and offset + count < total else None)
    if provider == 'wos':
        from research_sources import wos_record
        x = client.get(provider, 'https://api.clarivate.com/apis/wos-starter/v1/documents', {'q': q, 'db': 'WOS', 'limit': count, 'page': offset // count + 1},
            {'X-ApiKey': secret('WOS_API_KEY')}, secret_values=(secret('WOS_API_KEY'),))
        total = (x.get('metadata') or {}).get('total', 0)
        xs = [normalize_existing(provider, wos_record(a)) for a in x.get('hits', [])]
        return response(xs, total, str(offset + count) if xs and offset + count < total else None)
    if provider == 'springer':
        from research_sources import springer_record
        x = client.get(provider, 'https://api.springernature.com/meta/v2/json', {'q': q, 'p': count, 's': offset + 1, 'api_key': secret('SPRINGER_META_API_KEY')},
            secret_values=(secret('SPRINGER_META_API_KEY'),))
        total = int((x.get('result') or [{}])[0].get('total', 0))
        xs = [normalize_existing(provider, springer_record(a, 'meta')) for a in x.get('records', [])]
        return response(xs, total, str(offset + count) if xs and offset + count < total else None)
    if provider == 'ieee':
        x = client.get(provider, 'https://ieeexploreapi.ieee.org/api/v1/search/articles', {'querytext': q, 'max_records': count, 'start_record': offset + 1, 'format': 'json', 'apikey': secret('IEEE_API_KEY')}, secret_values=(secret('IEEE_API_KEY'),))
        xs = [m.make_record(provider, a.get('article_number'), a.get('title'), identifiers={'doi': a.get('doi'), 'ieee': a.get('article_number')},
            authors=[v.get('full_name', '') for v in a.get('authors', {}).get('authors', [])], type='conference' if a.get('content_type') == 'Conferences' else 'article',
            year=year(a.get('publication_year')), abstract=abstract(a.get('abstract'), provider),
            access={'status': 'open' if a.get('accessType') == 'Open Access' else 'unknown', 'locations': loc(a.get('abstract_url'), provider) + loc(a.get('pdf_url'), provider, 'pdf'), 'license': None, 'embargo_end': None}) for a in x.get('articles', [])]
        return response(xs, x.get('total_records'), str(offset + count) if xs and offset + count < int(x.get('total_records', 0)) else None)
    if provider == 'semanticscholar':
        x = client.get(provider, 'https://api.semanticscholar.org/graph/v1/paper/search', {'query': q, 'offset': offset, 'limit': count, 'fields': 'title,authors,year,abstract,externalIds,citationCount,openAccessPdf,url'},
            {'x-api-key': secret('SEMANTIC_SCHOLAR_API_KEY')}, secret_values=(secret('SEMANTIC_SCHOLAR_API_KEY'),))
        xs = []
        for a in x.get('data', []):
            ids = a.get('externalIds') or {}
            xs.append(m.make_record(provider, a['paperId'], a['title'], identifiers={'doi': ids.get('DOI'), 'arxiv': ids.get('ArXiv'), 'pmid': ids.get('PubMed')},
                authors=[v.get('name', '') for v in a.get('authors', [])], year=a.get('year'), abstract=abstract(a.get('abstract'), provider),
                citations={provider: {'count': a.get('citationCount')}},
                access={'status': 'open' if a.get('openAccessPdf') else 'unknown', 'locations': loc(a.get('url'), provider) + loc((a.get('openAccessPdf') or {}).get('url'), provider, 'pdf'), 'license': None, 'embargo_end': None}))
        return response(xs, x.get('total'), str(x['next']) if 'next' in x else None)
    raise SourceError('source_not_queryable')
