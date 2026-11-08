"""Exact record enrichment and explicit citation edges."""
import re
from urllib.parse import quote
from . import models as m, providers as p, store
from .transport import Budget, Client, SourceError


def save(r):
    return store.save_record(r, m.identity(r))


def get(record_id='', identifier='', source='auto', enrich=False, secret=lambda _: '', client=None):
    client = client or Client(Budget())
    if record_id:
        r = store.get_record(record_id)
        if not enrich:
            return {'ok': True, 'record': r, 'from_private_cache': True}
        identifier = r.get('identifiers', {}).get('doi') or r.get('identifiers', {}).get('arxiv') or ''
        if not identifier:
            return {'ok': True, 'record': r, 'enrichment': 'no_supported_exact_identifier'}
    d = m.doi(identifier)
    records, errors = [], {}
    if d:
        sources = ['crossref', 'datacite', 'openalex'] if source == 'auto' else [source]
        for src in sources:
            try:
                if src == 'crossref':
                    x = client.get(src, 'https://api.crossref.org/works/' + quote(d, safe=''))['message']
                    candidate = p.normalize_crossref(x)
                elif src == 'datacite':
                    x = client.get(src, 'https://api.datacite.org/dois/' + quote(d, safe=''))['data']
                    candidate = p.normalize_datacite(x)
                elif src == 'openalex':
                    params = {'filter': 'doi:https://doi.org/' + d, 'per-page': 1}
                    if secret('OPENALEX_API_KEY'):
                        params['api_key'] = secret('OPENALEX_API_KEY')
                    xs = client.get(src, 'https://api.openalex.org/works', params, secret_values=(secret('OPENALEX_API_KEY'),))['results']
                    if not xs:
                        raise SourceError('not_found')
                    candidate = p.normalize_openalex(xs[0])
                else:
                    raise SourceError('exact_lookup_source_unsupported')
                if candidate['identifiers'].get('doi') != d:
                    raise SourceError('record_identity_mismatch')
                records.append(save(candidate))
            except SourceError as exc:
                errors[src] = exc.code
    elif source == 'scopus' or re.fullmatch(r'2-s2\.0-\d{1,20}', identifier):
        if not re.fullmatch(r'2-s2\.0-\d{1,20}', identifier):
            raise ValueError('invalid_scopus_eid')
        if not secret('SCOPUS_API_KEY'):
            raise SourceError('needs_key')
        headers = {'X-ELS-APIKey': secret('SCOPUS_API_KEY')}
        if secret('ELSEVIER_INST_TOKEN'):
            headers['X-ELS-Insttoken'] = secret('ELSEVIER_INST_TOKEN')
        path = 'https://api.elsevier.com/content/abstract/eid/' + identifier
        try:
            x = client.get('scopus', path, {'view': 'FULL'}, headers, secret_values=(secret('SCOPUS_API_KEY'), secret('ELSEVIER_INST_TOKEN')))
            view = 'FULL'
        except SourceError as exc:
            if exc.status not in (401, 403):
                raise
            x = client.get('scopus', path, {'view': 'META'}, headers, secret_values=(secret('SCOPUS_API_KEY'), secret('ELSEVIER_INST_TOKEN')))
            view = 'META'
        root = x.get('abstracts-retrieval-response') or {}
        core = root.get('coredata') or {}
        if core.get('eid') != identifier:
            raise SourceError('record_identity_mismatch')
        import server as legacy
        r = p.normalize_existing('scopus', legacy.record(core))
        r['abstract'] = p.abstract(core.get('dc:description'), 'scopus')
        r['abstract_view_used'] = view
        r['field_sources']['abstract'] = ['scopus']
        records.append(save(r))
    elif source == 'arxiv' or re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?', identifier):
        import ai_search
        aid = ai_search.arxiv_id(identifier)
        raw = client.get('arxiv', 'https://export.arxiv.org/api/query', {'id_list': aid}, kind='xml')
        entries, _ = ai_search._atom(raw.encode(), 1)
        if not entries:
            raise SourceError('not_found')
        r = p.normalize_existing('arxiv', entries[0])
        actual = r['identifiers'].get('arxiv') or entries[0].get('arxiv_id') or ''
        if re.sub(r'v\d+$', '', actual) != re.sub(r'v\d+$', '', aid) or ('v' in aid.rsplit('/', 1)[-1] and actual != aid):
            raise SourceError('record_identity_mismatch')
        records.append(save(r))
    elif re.fullmatch(r'W\d{1,20}', identifier):
        x = client.get('openalex', 'https://api.openalex.org/works/' + identifier)
        if str(x.get('id', '')).rsplit('/', 1)[-1] != identifier:
            raise SourceError('record_identity_mismatch')
        records.append(save(p.normalize_openalex(x)))
    elif re.fullmatch(r'\d{4}[A-Za-z0-9]{6,12}', identifier) and source == 'thesesfr':
        data = client.get('thesesfr', 'https://theses.fr/api/v1/theses/recherche/', {'q': identifier, 'nombre': 5})
        matches = [x for x in data.get('theses', []) if (x.get('nnt') or x.get('id')) == identifier]
        if len(matches) != 1:
            raise SourceError('record_identity_mismatch')
        records.append(save(p.normalize_thesesfr(matches[0])))
    else:
        raise ValueError('provide_record_id_or_supported_exact_identifier')
    if not records:
        return {'ok': False, 'error': 'exact_record_unavailable', 'source_status': errors}
    return {'ok': True, 'record': store.get_record(records[0]['record_id']),
            'additional_record_ids': list(dict.fromkeys(r['record_id'] for r in records[1:] if r['record_id'] != records[0]['record_id'])),
            'source_status': errors}


def related(record_id, direction='references', source='auto', count=20, secret=lambda _: '', client=None):
    if direction not in ('references', 'citations', 'similar') or type(count) is not int or not 1 <= count <= 50:
        raise ValueError('invalid_relation_arguments')
    if source not in ('auto', 'openalex', 'crossref', 'semanticscholar'):
        raise ValueError('unsupported_relation_source')
    client = client or Client(Budget())
    record = store.get_record(record_id)
    ids = record['identifiers']
    chosen = 'openalex' if source == 'auto' and direction != 'references' else 'crossref' if source == 'auto' and record.get('references_by_source', {}).get('crossref') else 'openalex' if source == 'auto' else source
    edges = []
    if chosen == 'crossref':
        if direction != 'references':
            raise SourceError('crossref_does_not_provide_this_relation')
        refs = record.get('references_by_source', {}).get('crossref')
        if refs is None and ids.get('doi'):
            r = get(identifier=ids['doi'], source='crossref', secret=secret, client=client)
            refs = (r.get('record') or {}).get('references_by_source', {}).get('crossref', [])
        for ref in (refs or [])[:count]:
            edges.append({'from_record': record_id, 'type': 'references', 'source': 'crossref', **ref})
    elif chosen == 'openalex':
        aid = ids.get('openalex')
        if not aid and ids.get('doi'):
            r = get(identifier=ids['doi'], source='openalex', secret=secret, client=client)
            aid = (r.get('record') or {}).get('identifiers', {}).get('openalex')
        if not aid:
            raise SourceError('openalex_exact_identity_unavailable')
        if direction == 'citations':
            params = {'filter': 'cites:' + aid, 'per-page': count}
            x = client.get('openalex', 'https://api.openalex.org/works', params)
            for a in x['results']:
                r = save(p.normalize_openalex(a))
                edges.append({'from_record': r['record_id'], 'to_record': record_id, 'type': 'references', 'source': 'openalex'})
        else:
            x = client.get('openalex', 'https://api.openalex.org/works/' + aid)
            key = 'referenced_works' if direction == 'references' else 'related_works'
            for value in x.get(key, [])[:count]:
                edges.append({'from_record': record_id, 'to_identifier': value.rsplit('/', 1)[-1], 'type': direction, 'source': 'openalex', 'target_metadata_resolved': False})
    else:
        key = secret('SEMANTIC_SCHOLAR_API_KEY')
        if not key:
            raise SourceError('needs_key')
        if not ids.get('doi'):
            raise SourceError('exact_doi_required')
        if direction == 'similar':
            x = client.get(chosen, 'https://api.semanticscholar.org/recommendations/v1/papers/forpaper/' + quote('DOI:' + ids['doi'], safe=''),
                           {'fields': 'title,externalIds', 'limit': count}, {'x-api-key': key}, secret_values=(key,))
            targets = x.get('recommendedPapers', [])
        else:
            x = client.get(chosen, 'https://api.semanticscholar.org/graph/v1/paper/' + quote('DOI:' + ids['doi'], safe='') + '/' + direction,
                           {'fields': 'title,externalIds', 'limit': count}, {'x-api-key': key}, secret_values=(key,))
            targets = [(v.get('citedPaper' if direction == 'references' else 'citingPaper') or {}) for v in x.get('data', [])]
        for a in targets[:count]:
            edges.append({'from_record': record_id, 'type': direction, 'source': chosen, 'target_title': a.get('title'), 'target_identifiers': a.get('externalIds'),
                          'is_citation_edge': direction != 'similar'})
    return {'ok': True, 'source': chosen, 'direction': direction, 'relations': edges, 'limited_to': count, 'has_more': len(edges) == count}
