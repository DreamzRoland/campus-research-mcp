"""Auditable bounded federation, exact identities and source-aware ranking."""
import concurrent.futures
import copy
import json
import time
from . import models, providers, query as grammar, store
from .transport import Budget, Client, SourceError

PROFILES = {
    'general': ['scopus', 'openalex', 'crossref', 'wos'],
    'ai': ['arxiv', 'dblp', 'openreview', 'proceedings', 'openalex'],
    'theses': ['datacite', 'openaire', 'core', 'wanfang', 'thesesfr', 'repositories'],
    'biomedical': ['pubmed', 'europepmc', 'openalex', 'scopus'],
}


def plan(query, profile='general', sources='auto', filters=None, query_variants=None):
    if profile not in PROFILES:
        raise ValueError('unknown_search_profile')
    tree = grammar.parse(query)
    f = grammar.filters(filters)
    variants = query_variants or []
    if not isinstance(variants, list) or len(variants) > 4:
        raise ValueError('at_most_four_explicit_query_variants')
    for v in variants:
        grammar.parse(v)
    if not isinstance(sources, str):
        raise ValueError('sources_must_be_comma_separated')
    selected = PROFILES[profile][:] if sources == 'auto' else list(dict.fromkeys(s.strip() for s in sources.split(',') if s.strip()))
    if not selected or len(selected) > 12 or any(s not in providers.CATALOG for s in selected):
        raise ValueError('unknown_or_too_many_sources')
    if sources == 'auto' and f.get('publisher') and 'springer' in f['publisher'].lower():
        selected.append('springer')
    return {'schema_version': '2', 'query': query, 'query_variants': variants, 'profile': profile,
            'sources': selected, 'source_selection': 'automatic_profile' if sources == 'auto' else 'explicit',
            'filters': f, 'tree': tree, 'thesis_only': profile == 'theses',
            'query_expansion_is_explicit': True}


def safe_status(exc):
    if isinstance(exc, SourceError):
        return {'status': 'failed', 'error': exc.code, 'http_status': exc.status}
    if isinstance(exc, ValueError) and str(exc) in ('source_query_field_unsupported', 'source_boolean_operator_unsupported', 'source_rate_limit_deadline'):
        return {'status': 'unsupported' if 'unsupported' in str(exc) else 'failed', 'error': str(exc)}
    return {'status': 'failed', 'error': 'source_connection_or_schema_error'}


def call_source(source, p, cursor, count, client, secret, variant=0):
    tree = p['tree'] if variant == 0 else grammar.parse(p['query_variants'][variant - 1])
    before = client.counts.get(source, 0)
    compiled = None
    try:
        compiled = grammar.compile_query(tree, source)
        result = providers.search(source, tree, p['filters'], cursor, count, client, secret, p['thesis_only'])
        raw = result['records']
        result = {k: v for k, v in result.items() if k != 'records'}
        records = [r for r in raw if grammar.matches(r, p['filters'], p['thesis_only'])]
        return records, {'status': 'ok', **result, 'received': len(raw), 'matched': len(records),
                         'post_filter_applied': True, 'source_query': compiled, 'source_cursor_in': cursor,
                         'filters': p['filters'], 'requests_observed': client.counts.get(source, 0) - before}
    except Exception as exc:
        return [], {**safe_status(exc), 'source_query': compiled, 'source_cursor_in': cursor, 'filters': p['filters']}


def ranked(records, ranks, f):
    records = list(records.values())
    for r in records:
        entries = ranks.get(r['record_id'], {})
        r['ranking'] = {'method': 'equal_weight_rrf', 'constant': 60, 'source_ranks': entries,
                        'score': sum(1 / (60 + v) for v in entries.values())}
    sort = f.get('sort', 'relevance')
    if sort == 'year':
        records.sort(key=lambda r: (-(r.get('year') or 0), r['record_id']))
    elif sort == 'citations':
        src = f['citation_source']
        records.sort(key=lambda r: (-((r.get('citations', {}).get(src) or {}).get('count') or 0), r['record_id']))
    else:
        records.sort(key=lambda r: (-r['ranking']['score'], r['record_id']))
    return models.candidate_relations(records)


def ingest(records, records_by_id, ranks, source, rank_base=0):
    for n, r in enumerate(records, rank_base + 1):
        saved = store.save_record(r, models.identity(r))
        rid = saved['record_id']
        records_by_id[rid] = models.merge_record(records_by_id[rid], saved) if rid in records_by_id else saved
        ranks.setdefault(rid, {})[source] = min(n, ranks.get(rid, {}).get(source, n))


def preview(record):
    r = copy.deepcopy(record)
    if len(r.get('abstract', {}).get('text', '')) > 1500:
        r['abstract']['text'] = r['abstract']['text'][:1500]
        r['abstract']['preview_truncated'] = True
    r.pop('references', None)
    r.pop('references_by_source', None)
    r.pop('abstract_variants', None)
    return r


def render(run, limit=20):
    records = run['results']
    known, unknown = [], []
    for r in records:
        if run['plan']['thesis_only'] and (r.get('thesis') or {}).get('level') == 'unknown':
            unknown.append(preview(r))
        else:
            known.append(preview(r))
    return {'schema_version': '2', 'ok': any(s.get('status') == 'ok' for s in run['source_status'].values()),
            'run_id': run['run_id'], 'status': run['status'], 'plan': {k: v for k, v in run['plan'].items() if k != 'tree'},
            'results': known[:limit], 'unknown_degree_results': unknown[:limit],
            'record_count': len(records), 'source_status': run['source_status'],
            'requests_used': run['requests_used'], 'stop_reason': run['stop_reason'],
            'has_more': run.get('has_more', False), 'continuation_id': run['run_id'] if run.get('has_more') else None,
            'all_results_available_via_export': True, 'claims_global_exhaustiveness': False}


def search(query='', profile='general', sources='auto', filters=None, query_variants=None,
           continuation_id='', count=10, secret=lambda _: ''):
    if type(count) is not int or not 1 <= count <= 10:
        raise ValueError('count_per_source_must_be_1_to_10')
    if continuation_id:
        previous = store.get_run(continuation_id)
        p = previous['plan']
        if query or query_variants or filters or sources != 'auto':
            raise ValueError('continuation_cannot_change_frozen_plan')
        if not previous.get('has_more'):
            return render(previous)
    else:
        previous = None
        p = plan(query, profile, sources, filters, query_variants)
    budget = Budget()
    client = Client(budget)
    availability = {s['id']: s for s in providers.catalogue(secret)}
    states = copy.deepcopy(previous['source_status']) if previous else {}
    records, ranks = {}, {}
    if previous:
        for r in previous['results']:
            records[r['record_id']] = r
            ranks[r['record_id']] = r.get('ranking', {}).get('source_ranks', {})
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    pending = {}
    for src in p['sources']:
        prev = states.get(src, {})
        if previous and prev.get('status') == 'ok' and not prev.get('has_more'):
            continue
        if not availability[src]['queryable']:
            states[src] = {'status': availability[src]['status'], 'error': 'source_not_enabled', 'official_url': availability[src]['url']}
            continue
        cursor = prev.get('next_cursor') if previous else None
        pending[pool.submit(call_source, src, p, cursor, count, client, secret)] = src
    done, unfinished = concurrent.futures.wait(pending, timeout=max(0, budget.deadline - time.monotonic())) if pending else (set(), set())
    for future in done:
        src = pending[future]
        xs, status = future.result()
        old = states.get(src, {})
        ingest(xs, records, ranks, src, old.get('rank_offset', 0))
        states[src] = {**status, 'rank_offset': old.get('rank_offset', 0) + status.get('received', 0)}
    for future in unfinished:
        future.cancel()
        states[pending[future]] = {'status': 'timeout', 'error': 'daily_time_budget_exhausted'}
    pool.shutdown(wait=False, cancel_futures=True)
    has_more = bool(p['query_variants']) or any(s.get('has_more') or s.get('status') in ('timeout', 'failed') for s in states.values())
    failures = any(s.get('status') != 'ok' for s in states.values())
    ordered = ranked(records, ranks, p['filters'])
    run = {'plan': p, 'results': ordered, 'source_status': states,
           'requests_used': budget.used + (previous.get('requests_used', 0) if previous else 0),
           'status': 'partial' if has_more or failures else 'completed',
           'stop_reason': 'daily_batch_complete' if not unfinished else 'time_budget_exhausted', 'has_more': has_more,
           'run_id': continuation_id or store.identifier('s_'), 'created_at': previous['created_at'] if previous else time.time()}
    # Variants are deliberately expanded only in deep mode; report this instead of silently claiming they ran.
    if p['query_variants']:
        run['stop_reason'] = 'variants_pending_use_deep_search'
    store.save_run(run, run['run_id'])
    return render(run)


def deep(p, secret, cancelled, checkpoint, resume=None, citation_expansion=False):
    budget = Budget(seconds=1200, requests=150, cancelled=cancelled)
    records, ranks = {}, {}
    if resume:
        for r in resume.get('results', []):
            records[r['record_id']] = r
            ranks[r['record_id']] = r.get('ranking', {}).get('source_ranks', {})
    states = copy.deepcopy((resume or {}).get('source_status', {}))
    cursors = copy.deepcopy((resume or {}).get('cursors', {}))
    seen = copy.deepcopy((resume or {}).get('seen_cursors', {}))
    finished = set((resume or {}).get('finished', []))
    availability = {s['id']: s for s in providers.catalogue(secret)}
    previous_used = (resume or {}).get('requests_used', 0)
    budget.limit = max(0, 150 - previous_used)
    created = (resume or {}).get('created_at', time.time())
    budget.deadline = time.monotonic() + max(0, 1200 - (time.time() - created))
    client = Client(budget)
    runid = (resume or {}).get('run_id', store.identifier('s_'))
    reason = 'source_pages_exhausted'
    while len(finished) < len(p['sources']) * (len(p['query_variants']) + 1):
        try:
            budget.check()
            if len(records) >= 500:
                reason = 'record_budget_exhausted'
                break
            if budget.used >= budget.limit:
                reason = 'request_budget_exhausted'
                break
            for variant in range(len(p['query_variants']) + 1):
                for src in p['sources']:
                    key = src + ':' + str(variant)
                    if key in finished:
                        continue
                    budget.check()
                    if not availability[src]['queryable']:
                        states[key] = {'status': availability[src]['status'], 'official_url': availability[src]['url']}
                        finished.add(key)
                        continue
                    cursor = cursors.get(key)
                    xs, status = call_source(src, p, cursor, 10, client, secret, variant)
                    ingest(xs[:max(0, 500 - len(records))], records, ranks, src, states.get(key, {}).get('rank_offset', 0))
                    status['rank_offset'] = states.get(key, {}).get('rank_offset', 0) + status.get('received', 0)
                    states[key] = status
                    nxt = status.get('next_cursor')
                    if status.get('status') != 'ok' or nxt is None:
                        finished.add(key)
                    elif str(nxt) in seen.get(key, []):
                        status.update(status='failed', error='repeating_source_cursor')
                        finished.add(key)
                    else:
                        seen.setdefault(key, []).append(str(nxt))
                        cursors[key] = nxt
                    snapshot = {'plan': p, 'run_id': runid, 'results': ranked(records, ranks, p['filters']),
                                'source_status': states, 'cursors': cursors, 'seen_cursors': seen, 'finished': list(finished),
                                'requests_used': previous_used + budget.used, 'created_at': created,
                                'status': 'running', 'stop_reason': None, 'has_more': True}
                    checkpoint(snapshot)
                    if len(records) >= 500 or budget.used >= budget.limit:
                        break
                if len(records) >= 500 or budget.used >= budget.limit:
                    break
        except SourceError as exc:
            reason = exc.code
            break
    if reason == 'source_pages_exhausted' and any(s.get('status') != 'ok' for s in states.values()):
        reason = 'source_processing_complete_with_gaps'
    out = {'plan': p, 'run_id': runid, 'results': ranked(records, ranks, p['filters'])[:500], 'source_status': states,
           'cursors': cursors, 'seen_cursors': seen, 'finished': list(finished),
           'requests_used': previous_used + budget.used, 'created_at': created,
           'status': 'completed' if reason == 'source_pages_exhausted' and all(s.get('status') == 'ok' for s in states.values()) else 'partial',
           'stop_reason': reason, 'has_more': reason not in ('source_pages_exhausted', 'source_processing_complete_with_gaps')}
    if citation_expansion and reason != 'cancelled':
        from .records import related
        edges = []
        for r in out['results'][:20]:
            try:
                edges.extend(related(r['record_id'], 'references', 'auto', min(20, 200 - len(edges)), secret, client)['relations'])
            except (ValueError, SourceError):
                break
            if len(edges) >= 200:
                break
        out['citation_relations'] = edges
        out['requests_used'] = previous_used + budget.used
    store.save_run(out, runid)
    return out
