"""Bounded incremental OAI-PMH harvesting, never presented as live OAI keyword search."""
import json
import re
import time
import xml.etree.ElementTree as ET
from . import models as m, store
from .providers import abstract, loc, thesis, response, year
from .transport import Budget, Client, SourceError

REPOSITORIES = {
    'mit': {'name': 'Massachusetts Institute of Technology', 'endpoints': [
        'https://dspace.mit.edu/server/oai/request', 'https://dspace.mit.edu/oai/request', 'https://dsapi.lib.mit.edu/server/oai/request'], 'set': 'com_1721.1_7582'},
    'caltech': {'name': 'California Institute of Technology', 'endpoints': [
        'https://thesis.caltech.edu/oai2d', 'https://thesis.caltech.edu/cgi/oai2']},
    'hkust': {'name': 'Hong Kong University of Science and Technology', 'endpoints': [
        'https://repository.hkust.edu.hk/oai/request', 'https://repository.hkust.edu.hk/ir/oai/request']},
}
NS = {'o': 'http://www.openarchives.org/OAI/2.0/', 'dc': 'http://purl.org/dc/elements/1.1/'}


def verify_repository(code, client=None):
    if code not in REPOSITORIES:
        raise ValueError('unregistered_repository')
    client = client or Client(Budget(50, 6))
    errors = []
    for endpoint in REPOSITORIES[code]['endpoints']:
        try:
            raw = client.get(code, endpoint, {'verb': 'Identify'}, kind='xml', ttl=0)
            root = ET.fromstring(raw)
            if root.tag != '{http://www.openarchives.org/OAI/2.0/}OAI-PMH' or root.find('o:Identify', NS) is None:
                raise SourceError('not_an_oai_pmh_endpoint')
            evidence = {'status': 'available', 'endpoint': endpoint, 'protocol': 'OAI-PMH', 'metadata_only': True}
            store.capability(code, evidence)
            return evidence
        except (SourceError, ET.ParseError) as exc:
            errors.append(exc.code if isinstance(exc, SourceError) else 'invalid_oai_xml')
    result = {'status': 'temporarily_unavailable', 'errors': errors, 'metadata_only': True}
    store.capability(code, result)
    return result


def oai_record(element, code):
    header = element.find('o:header', NS)
    if header is None:
        return None, None, False
    ident = header.findtext('o:identifier', '', NS)
    if header.get('status') == 'deleted':
        return ident, None, True
    dc = element.find('o:metadata', NS)
    if dc is None:
        return ident, None, False
    fields = {}
    for e in dc.iter():
        if e.tag.startswith('{http://purl.org/dc/elements/1.1/}') and e.text:
            fields.setdefault(e.tag.rsplit('}', 1)[-1], []).append(e.text.strip())
    label = ' '.join(fields.get('type', []))
    degree_labels = [v for v in fields.get('description', []) if len(v) < 120 and m.degree(v) != 'unknown']
    if degree_labels:
        label += ' ' + degree_labels[-1]
    if code != 'caltech' and not re.search(r'thesis|dissertation|学位', label, re.I):
        return ident, None, False
    identifiers = fields.get('identifier', [])
    ids = {'repository': ident}
    locations = []
    for v in identifiers:
        if m.doi(v):
            ids['doi'] = m.doi(v)
        if '/handle/' in v:
            ids['handle'] = v.split('/handle/', 1)[1]
        if 'hdl.handle.net/' in v:
            ids['handle'] = v.split('hdl.handle.net/', 1)[1]
        locations += loc(v, code, 'pdf' if v.lower().endswith('.pdf') else 'landing', repository=code)
    if code == 'mit' and ids.get('handle'):
        locations += loc('https://dspace.mit.edu/handle/' + ids['handle'], code, repository=code)
    title = (fields.get('title') or [''])[0]
    notes = fields.get('description', [])
    award_year = next((year(v) for v in notes if v.lower().startswith('thesis ') and year(v)), None)
    raw_years = [int(v) for v in fields.get('date', []) if re.fullmatch(r'(?:18|19|20)\d{2}', v)]
    selected_year = award_year or (min(raw_years) if raw_years else None)
    r = m.make_record(code, ident, title, identifiers=ids, type='thesis',
        authors=fields.get('creator', []), year=selected_year,
        language=(fields.get('language') or [None])[0], abstract=abstract('', code),
        thesis=thesis(label, REPOSITORIES[code]['name'], institution_evidence='registered_repository_not_independent_award_verification', dates=fields.get('date', []),
                      award_year=award_year, year_basis='explicit_thesis_note' if award_year else 'untyped_repository_year'),
        access={'status': 'unknown', 'locations': locations, 'license': ' '.join(fields.get('rights', [])), 'embargo_end': None},
        evidence=[{'source': code, 'kind': 'oai_pmh_metadata', 'identifier': ident, 'datestamp': header.findtext('o:datestamp', '', NS)}])
    r['repository_descriptions'] = notes
    r['untyped_contributors'] = fields.get('contributor', [])
    return ident, r, False


def harvest(code, progress=lambda _: None, cancelled=lambda: False, maximum=5000):
    if code not in REPOSITORIES or type(maximum) is not int or not 1 <= maximum <= 5000:
        raise ValueError('invalid_harvest_request')
    client = Client(Budget(1200, 150, cancelled))
    cap = store.capability(code) or verify_repository(code, client)
    if cap.get('status') != 'available' or not cap.get('endpoint'):
        raise SourceError('repository_endpoint_not_verified')
    with store.connect() as db:
        row = db.execute('SELECT payload FROM harvest WHERE provider=?', (code,)).fetchone()
    state = json.loads(row[0]) if row else {'processed': 0, 'indexed': 0, 'resumption_token': None, 'complete': False}
    token = state.get('resumption_token')
    params = {'verb': 'ListRecords', 'resumptionToken': token} if token else {'verb': 'ListRecords', 'metadataPrefix': 'oai_dc'}
    if not token and REPOSITORIES[code].get('set'):
        params['set'] = REPOSITORIES[code]['set']
    if not token and state.get('last_datestamp'):
        params['from'] = state['last_datestamp']
    pages = 0
    while pages < 150 and not cancelled():
        raw = client.get(code, cap['endpoint'], params, kind='xml', ttl=0)
        root = ET.fromstring(raw)
        err = root.find('o:error', NS)
        if err is not None:
            if err.get('code') == 'noRecordsMatch':
                state.update(complete=True, resumption_token=None)
                break
            if err.get('code') == 'badResumptionToken':
                state.update(resumption_token=None, complete=False, error='oai_cursor_expired_restart_from_last_committed_datestamp')
                with store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO harvest VALUES(?,?,?)', (code, store.encode(state), time.time()))
            raise SourceError('oai_' + str(err.get('code', 'error')))
        elements = root.findall('o:ListRecords/o:record', NS)
        for e in elements:
            identifier, record, deleted = oai_record(e, code)
            if deleted:
                with store.connect() as db:
                    rows = db.execute('SELECT id FROM records WHERE identity=?', ('thesis:' + code + ':' + identifier,)).fetchall()
                    for r in rows:
                        db.execute('DELETE FROM repository_fts WHERE record_id=?', (r[0],))
                        # Other-source evidence remains in the canonical record; mark this source withdrawn.
                        saved = store.get_record(r[0])
                        saved['source_records'].get(code, {})['deleted_at_source'] = True
                        db.execute('UPDATE records SET payload=? WHERE id=?', (store.encode(saved), r[0]))
            elif record:
                r = store.save_record(record, m.identity(record))
                with store.connect() as db:
                    db.execute('DELETE FROM repository_fts WHERE record_id=? AND provider=?', (r['record_id'], code))
                    db.execute('INSERT INTO repository_fts VALUES(?,?,?,?)', (r['record_id'], code, r['title'], r['abstract']['text']))
                state['indexed'] += 1
            state['processed'] += 1
            datestamp = e.findtext('o:header/o:datestamp', '', NS)
            if datestamp:
                state['last_datestamp'] = max(datestamp, state.get('last_datestamp', ''))
        node = root.find('o:ListRecords/o:resumptionToken', NS)
        next_token = (node.text or '').strip() if node is not None else ''
        if next_token and next_token == state.get('resumption_token'):
            raise SourceError('repeating_oai_cursor')
        state.update(resumption_token=next_token or None, complete=not bool(next_token), updated_at=time.time())
        with store.connect() as db:
            db.execute('INSERT OR REPLACE INTO harvest VALUES(?,?,?)', (code, store.encode(state), time.time()))
        progress({'phase': 'harvesting', 'repository': code, **state})
        pages += 1
        if not next_token or state['processed'] >= maximum:
            break
        params = {'verb': 'ListRecords', 'resumptionToken': next_token}
    return {'ok': True, 'repository': code, **state, 'scope': 'bounded_harvest_not_entire_repository', 'cancelled': cancelled()}


def search_index(q, count, cursor):
    offset = int(cursor or 0)
    # Quote each token; never expose an arbitrary FTS program or SQL.
    terms = re.findall(r'"([^"]+)"|(\S+)', q)
    expr = ' AND '.join('"' + (a or b).replace('"', '""') + '"' for a, b in terms)
    with store.connect() as db:
        coverage = [{'repository': r[0], **json.loads(r[1])} for r in db.execute('SELECT provider,payload FROM harvest')]
        if not coverage:
            raise SourceError('no_verified_repository_harvest_available')
        rows = db.execute('SELECT record_id FROM repository_fts WHERE repository_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?', (expr, count + 1, offset)).fetchall()
    records = [store.get_record(r[0]) for r in rows[:count]]
    return response(records, cursor=str(offset + count) if len(rows) > count else None, coverage=coverage,
                    matching_scope='locally_harvested_metadata_only')


def repository_files(record):
    """METS gives an explicit item-to-original-file relation, including scanned theses."""
    meta = record.get('source_records', {}).get('mit')
    if not meta:
        return []
    ident = meta['id']
    client = Client(Budget(20, 3))
    xml = client.get('mit', REPOSITORIES['mit']['endpoints'][0],
                     {'verb': 'GetRecord', 'metadataPrefix': 'mets', 'identifier': ident}, kind='xml')
    root = ET.fromstring(xml)
    actual = root.findtext('o:GetRecord/o:record/o:header/o:identifier', '', NS)
    if actual != ident:
        raise SourceError('repository_file_record_identity_mismatch')
    found = []
    for group in root.iter('{http://www.loc.gov/METS/}fileGrp'):
        use = group.get('USE', '').upper()
        if use not in ('ORIGINAL', 'CONTENT'):
            continue
        for file in group.findall('{http://www.loc.gov/METS/}file'):
            if file.get('MIMETYPE', '').lower() != 'application/pdf':
                continue
            for element in file.findall('{http://www.loc.gov/METS/}FLocat'):
                url = element.get('{http://www.w3.org/1999/xlink}href')
                if not url or not url.startswith('https://dspace.mit.edu/'):
                    continue
                urls = [url]
                bitstream = re.fullmatch(r'https://dspace\.mit\.edu/bitstreams/([a-f0-9-]{36})/download', url)
                if bitstream:
                    urls.insert(0, 'https://dspace.mit.edu/server/api/core/bitstreams/' + bitstream[1] + '/content')
                for target in urls:
                    found += loc(target, 'mit', 'pdf', repository='mit', repository_file_relation_verified=True,
                             persistent_identifier_verified=True, source_record=ident, part_id=file.get('ID'),
                             expected_byte_size=int(file.get('SIZE')) if str(file.get('SIZE', '')).isdigit() else None,
                             expected_md5=file.get('CHECKSUM') if file.get('CHECKSUMTYPE') == 'MD5' else None)
    return found


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('repository', choices=REPOSITORIES)
    parser.add_argument('--harvest', action='store_true')
    args = parser.parse_args()
    print(json.dumps(harvest(args.repository) if args.harvest else verify_repository(args.repository), ensure_ascii=False))
