"""Small deterministic boolean grammar. No paid model or implicit translation."""
import re

FIELDS = {'title', 'abstract', 'author', 'all'}
TOKEN = re.compile(r'\s*((?:[A-Za-z]+:)?"(?:[^"\\]|\\.)*"|\(|\)|[^\s()]+)')


def parse(query):
    if not isinstance(query, str) or not query.strip() or len(query) > 1500 or any(ord(c) < 32 for c in query):
        raise ValueError('query_must_be_1_to_1500_printable_characters')
    tokens = TOKEN.findall(query)
    if len(tokens) > 100:
        raise ValueError('query_too_complex')
    position = 0

    def atom():
        nonlocal position
        if position >= len(tokens):
            raise ValueError('incomplete_boolean_query')
        t = tokens[position]
        position += 1
        if t == '(':
            v = expression()
            if position >= len(tokens) or tokens[position] != ')':
                raise ValueError('unbalanced_query_parentheses')
            position += 1
            return v
        if t.upper() == 'NOT':
            return ('NOT', atom())
        if t in (')', 'AND', 'OR'):
            raise ValueError('invalid_boolean_operator')
        field = 'all'
        if ':' in t and not t.startswith('"'):
            field, t = t.split(':', 1)
            if field not in FIELDS:
                raise ValueError('unsupported_query_field')
            if not t:
                if position >= len(tokens):
                    raise ValueError('missing_field_value')
                t = tokens[position]
                position += 1
        if t.count('"') not in (0, 2) or (t.startswith('"') != t.endswith('"')):
            raise ValueError('unbalanced_query_quotes')
        return ('TERM', field, t.strip('"'))

    def conjunction():
        nonlocal position
        v = atom()
        while position < len(tokens) and tokens[position].upper() != 'OR' and tokens[position] != ')':
            if tokens[position].upper() == 'AND':
                position += 1
            v = ('AND', v, atom())
        return v

    def expression():
        nonlocal position
        v = conjunction()
        while position < len(tokens) and tokens[position].upper() == 'OR':
            position += 1
            v = ('OR', v, conjunction())
        return v

    tree = expression()
    if position != len(tokens):
        raise ValueError('unbalanced_query_parentheses')
    return tree


def compile_query(tree, provider):
    """Source grammars differ. Fail closed where operators cannot be represented."""
    fieldmap = {
        'arxiv': {'all': 'all', 'title': 'ti', 'abstract': 'abs', 'author': 'au'},
        'scopus': {'all': 'TITLE-ABS-KEY', 'title': 'TITLE', 'abstract': 'ABS', 'author': 'AUTH'},
        'wos': {'all': 'TS', 'title': 'TI', 'abstract': 'AB', 'author': 'AU'},
        'datacite': {'all': '', 'title': 'titles.title:', 'abstract': 'descriptions.description:', 'author': 'creators.name:'},
        'wanfang': {'all': '', 'title': 'Title:', 'abstract': 'Abstract:', 'author': 'Creator:'},
        'pubmed': {'all': '', 'title': '[Title]', 'abstract': '[Title/Abstract]', 'author': '[Author]'},
        'europepmc': {'all': '', 'title': 'TITLE:', 'abstract': 'ABSTRACT:', 'author': 'AUTH_LAST:'},
    }
    simple = provider in ('crossref', 'openalex', 'dblp', 'core', 'openreview', 'proceedings', 'repositories', 'ieee', 'semanticscholar')
    if tree[0] == 'TERM':
        _, field, value = tree
        if simple:
            if field != 'all':
                raise ValueError('source_query_field_unsupported')
            return '"' + value.replace('"', '') + '"' if ' ' in value else value
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        part = '"' + escaped + '"'
        fieldcode = fieldmap.get(provider, {}).get(field)
        if fieldcode is None:
            if provider in ('openaire', 'springer', 'thesesfr') and field == 'all':
                return part
            raise ValueError('source_query_field_unsupported')
        if provider == 'scopus':
            return fieldcode + '(' + part + ')'
        if provider == 'wos':
            return fieldcode + '=' + part
        if provider == 'pubmed':
            return part + fieldcode
        if provider == 'arxiv':
            return fieldcode + ':' + part
        return fieldcode + part
    if simple:
        # Plain AND is represented by the endpoint's documented keyword semantics;
        # do not silently drop OR/NOT from bibliographic endpoints.
        if tree[0] != 'AND':
            raise ValueError('source_boolean_operator_unsupported')
        return compile_query(tree[1], provider) + ' ' + compile_query(tree[2], provider)
    if tree[0] == 'NOT':
        return 'NOT (' + compile_query(tree[1], provider) + ')'
    return '(' + compile_query(tree[1], provider) + ' ' + tree[0] + ' ' + compile_query(tree[2], provider) + ')'


def filters(value=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError('filters_must_be_an_object')
    allowed = {'from_year', 'to_year', 'language', 'degree', 'institution', 'conferences', 'track', 'publisher', 'type', 'include_unknown', 'include_in_progress', 'sort', 'citation_source'}
    if set(value) - allowed:
        raise ValueError('unknown_filter')
    for key in ('from_year', 'to_year'):
        if key in value and (type(value[key]) is not int or not 1000 <= value[key] <= 2100):
            raise ValueError('invalid_year_filter')
    if value.get('from_year', 1000) > value.get('to_year', 2100):
        raise ValueError('invalid_year_range')
    if value.get('degree') not in (None, 'masters', 'doctoral', 'other', 'unknown'):
        raise ValueError('invalid_degree_filter')
    for key in ('include_unknown', 'include_in_progress'):
        if key in value and type(value[key]) is not bool:
            raise ValueError('invalid_boolean_filter')
    for key in ('language', 'institution', 'publisher', 'type', 'track', 'conferences', 'sort', 'citation_source'):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > 300):
            raise ValueError('invalid_text_filter')
    if value.get('sort', 'relevance') not in ('relevance', 'year', 'citations'):
        raise ValueError('invalid_sort')
    if value.get('sort') == 'citations' and not value.get('citation_source'):
        raise ValueError('citation_sort_requires_source')
    return dict(value)


def matches(record, f, thesis_only=False):
    if thesis_only:
        if record.get('type') != 'thesis':
            return False
        t = record.get('thesis') or {}
        if t.get('status') == 'in_progress' and not f.get('include_in_progress'):
            return False
        if t.get('level') == 'other' and f.get('degree') != 'other':
            return False
        if t.get('level') == 'unknown' and not f.get('include_unknown', True):
            return False
    if f.get('degree') and (record.get('thesis') or {}).get('level') != f['degree']:
        return False
    if ('from_year' in f or 'to_year' in f) and not record.get('year'):
        return False
    if record.get('year') and not f.get('from_year', 1000) <= record['year'] <= f.get('to_year', 2100):
        return False
    if f.get('language') and str(record.get('language') or '').lower() not in (f['language'].lower(), {'en': 'eng', 'zh': 'zho', 'fr': 'fra'}.get(f['language'])):
        return False
    if f.get('institution') and f['institution'].casefold() not in str((record.get('thesis') or {}).get('institution', '')).casefold():
        return False
    if f.get('type') and record.get('type') != f['type']:
        return False
    if f.get('publisher') and f['publisher'].casefold() not in str(record.get('publisher', '')).casefold():
        return False
    if f.get('track') and record.get('track') != f['track']:
        return False
    return True
