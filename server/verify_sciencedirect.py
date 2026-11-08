#!/usr/bin/env python3
"""Choose one Elsevier journal DOI via Scopus; check SD status without body."""
import json
import sys
from server import search_scopus, check_sciencedirect_access

result = search_scopus('SRCTITLE("Combustion and Flame") AND DOCTYPE(ar)', count=1)
if not result.get('ok'):
    print(json.dumps({'checkpoint':'select_sciencedirect_doi', **result}))
    sys.exit(1)
doi = next((r.get('doi') for r in result.get('records', []) if r.get('doi')), None)
if not doi:
    print(json.dumps({'checkpoint':'select_sciencedirect_doi','ok':False,'error':'no_doi_in_result'}))
    sys.exit(1)
check = check_sciencedirect_access(doi)
print(json.dumps({'checkpoint':'sciencedirect_full_api','source':'Scopus: Combustion and Flame', **check}))
sys.exit(0 if check.get('ok') else 1)
