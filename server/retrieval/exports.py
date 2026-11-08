"""Reproducible runs and bounded reference imports; imported claims stay unverified."""
import csv
import io
import json
import re
from . import models as m, store
from .files import save_bytes
from .providers import abstract, thesis, year


def csv_cell(value):
    value = str(value if value is not None else '')
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def bib_escape(value):
    return str(value).replace('\\', '\\textbackslash{}').replace('{', '\\{').replace('}', '\\}').replace('\n', ' ')


def bib_record(r):
    kind = 'phdthesis' if r['type'] == 'thesis' and (r.get('thesis') or {}).get('level') == 'doctoral' else 'mastersthesis' if r['type'] == 'thesis' and (r.get('thesis') or {}).get('level') == 'masters' else 'misc' if r['type'] in ('thesis', 'preprint', 'unknown') else 'inproceedings' if r['type'] == 'conference' else 'article'
    fields = {'title': r['title'], 'author': ' and '.join(str(a) for a in r['authors']), 'year': r.get('year'),
              'doi': r['identifiers'].get('doi'), 'school': (r.get('thesis') or {}).get('institution'),
              'abstract': r['abstract']['text'], 'note': 'Sources: ' + ','.join(r['source_records'])}
    return '@' + kind + '{' + r['record_id'] + ',\n' + ',\n'.join('  ' + k + ' = {' + bib_escape(v) + '}' for k, v in fields.items() if v) + '\n}\n'


def ris_record(r):
    fields = [('TY', 'THES' if r['type'] == 'thesis' else 'CONF' if r['type'] == 'conference' else 'JOUR'), ('TI', r['title'])]
    fields += [('AU', str(a)) for a in r['authors']]
    fields += [('PY', r.get('year')), ('DO', r['identifiers'].get('doi')), ('AB', r['abstract']['text']),
               ('PB', (r.get('thesis') or {}).get('institution')), ('N1', 'Sources: ' + ','.join(r['source_records'])), ('ER', '')]
    return '\n'.join(k + '  - ' + str(v).replace('\n', ' ') for k, v in fields if v is not None) + '\n\n'


def export(run_id, format='json'):
    if format not in ('json', 'csv', 'ris', 'bibtex', 'report'):
        raise ValueError('unsupported_export_format')
    run = store.get_run(run_id)
    records = run['results']
    if format == 'json':
        body, ext, mime = json.dumps(run, ensure_ascii=False, indent=2), 'json', 'application/json'
    elif format == 'csv':
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        keys = ['record_id', 'title', 'authors', 'year', 'type', 'degree', 'institution', 'doi', 'abstract', 'sources']
        writer.writerow(keys)
        for r in records:
            vals = [r['record_id'], r['title'], '; '.join(str(a) for a in r['authors']), r.get('year'), r['type'],
                    (r.get('thesis') or {}).get('level'), (r.get('thesis') or {}).get('institution'),
                    r['identifiers'].get('doi'), r['abstract']['text'], ','.join(r['source_records'])]
            writer.writerow([csv_cell(v) for v in vals])
        body, ext, mime = output.getvalue(), 'csv', 'text/csv'
    elif format == 'bibtex':
        body, ext, mime = '\n'.join(bib_record(r) for r in records), 'bib', 'application/x-bibtex'
    elif format == 'ris':
        body, ext, mime = ''.join(ris_record(r) for r in records), 'ris', 'application/x-research-info-systems'
    else:
        p = run['plan']
        lines = ['Campus literature search report', 'Query: ' + p['query'], 'Variants: ' + json.dumps(p['query_variants'], ensure_ascii=False),
                 'Profile: ' + p['profile'], 'Filters: ' + json.dumps(p['filters'], ensure_ascii=False),
                 'Status: ' + run['status'], 'Stop reason: ' + str(run['stop_reason']), 'Record count: ' + str(len(records)),
                 'Requests: ' + str(run['requests_used']), 'Source status: ' + json.dumps(run['source_status'], ensure_ascii=False),
                 'This is an auditable bounded search, not proof of global exhaustive coverage.', '']
        lines.extend(f"{i}. {r['title']} | {r.get('year')} | {r['record_id']}" for i, r in enumerate(records, 1))
        body, ext, mime = '\n'.join(lines), 'txt', 'text/plain'
    return save_bytes(body.encode('utf-8'), run_id + '.' + ext, mime)


def parse_ris(content):
    records, current, last = [], {}, None
    for line in content.splitlines():
        match = re.match(r'^([A-Z0-9]{2})\s{2}-\s?(.*)$', line)
        if match:
            tag, value = match.groups()
            if tag == 'TY':
                current = {'TY': [value]}
            elif tag == 'ER':
                if current:
                    records.append(current)
                current = {}
            else:
                current.setdefault(tag, []).append(value)
            last = tag
        elif current and last and line.strip():
            current[last][-1] += ' ' + line.strip()
    if current:
        raise ValueError('unterminated_ris_record')
    return records


def parse_bibtex(content):
    """Bounded brace-aware literal BibTeX. No macros, string evaluation or TeX execution."""
    records, pos = [], 0
    while True:
        match = re.search(r'@(\w+)\s*\{\s*([^,{}]+)\s*,', content[pos:])
        if not match:
            break
        kind = match[1].lower()
        if kind in ('string', 'preamble', 'comment'):
            raise ValueError('bibtex_macros_not_supported')
        pos += match.end()
        fields = {'_kind': kind, '_key': match[2]}
        while pos < len(content):
            while pos < len(content) and content[pos] in ' \r\n\t,':
                pos += 1
            if pos < len(content) and content[pos] == '}':
                pos += 1
                break
            key = re.match(r'(\w+)\s*=\s*', content[pos:])
            if not key:
                raise ValueError('unsupported_bibtex_syntax')
            pos += key.end()
            if pos >= len(content):
                raise ValueError('unterminated_bibtex_record')
            opener = content[pos]
            if opener in ('{', '"'):
                closer, depth, start = ('}' if opener == '{' else '"'), 1, pos + 1
                pos += 1
                while pos < len(content):
                    ch = content[pos]
                    if ch == '\\':
                        pos += 2
                        continue
                    if opener == '{' and ch == '{':
                        depth += 1
                    if ch == closer:
                        depth -= 1
                        if depth == 0:
                            break
                    pos += 1
                if pos >= len(content):
                    raise ValueError('unterminated_bibtex_value')
                value = content[start:pos]
                pos += 1
            else:
                value_match = re.match(r'\d+', content[pos:])
                if not value_match:
                    raise ValueError('bibtex_macros_not_supported')
                value = value_match[0]
                pos += len(value)
            fields[key[1].lower()] = value
        else:
            raise ValueError('unterminated_bibtex_record')
        records.append(fields)
    return records


def import_refs(content, format='ris'):
    if not isinstance(content, str) or len(content.encode()) > 1024 * 1024:
        raise ValueError('reference_import_limit_1MiB')
    if format not in ('ris', 'bibtex'):
        raise ValueError('reference_format_must_be_ris_or_bibtex')
    rows = parse_ris(content) if format == 'ris' else parse_bibtex(content)
    if not rows or len(rows) > 200:
        raise ValueError('reference_import_requires_1_to_200_records')
    out = []
    for i, row in enumerate(rows):
        get = (lambda k: (row.get(k) or [''])[0]) if format == 'ris' else (lambda k: row.get(k, ''))
        title = get('TI') or get('T1') if format == 'ris' else get('title')
        if not title:
            raise ValueError('reference_title_missing')
        kind = get('TY') if format == 'ris' else get('_kind')
        is_thesis = kind.upper() == 'THES' or kind in ('phdthesis', 'mastersthesis')
        name = 'doctoral' if kind == 'phdthesis' else 'masters' if kind == 'mastersthesis' else get('M3') if format == 'ris' else get('type')
        authors = row.get('AU', []) if format == 'ris' else [a.strip() for a in re.split(r'\s+and\s+', get('author')) if a.strip()]
        inst = get('PB') if format == 'ris' else get('school')
        content_hash = __import__('hashlib').sha256(store.encode(row).encode()).hexdigest()
        r = m.make_record('reference_import', content_hash, title, type='thesis' if is_thesis else 'unknown',
            authors=authors, identifiers={'doi': get('DO') if format == 'ris' else get('doi')},
            year=year(get('PY') if format == 'ris' else get('year')), abstract=abstract(get('AB') if format == 'ris' else get('abstract'), 'reference_import'),
            thesis=thesis(name, inst) if is_thesis else None, evidence=[{'kind': 'user_supplied_reference', 'verified': False}])
        out.append(store.save_record(r, m.identity(r)))
    return {'ok': True, 'imported': len(out), 'records': out, 'metadata_independently_verified': False, 'fulltext_download_performed': False}
