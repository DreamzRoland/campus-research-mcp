#!/usr/bin/env python3
"""Credential-aware API probe; outputs status and counts only."""
import json
import os
import sys
from pathlib import Path
from server import ping, search_scopus, get_scopus_record, check_sciencedirect_access

def main():
    p = ping()
    print(json.dumps({'checkpoint': 'credential_presence', **p}))
    if not p['scopus_key_configured']:
        print(json.dumps({'checkpoint': 'scopus_search', 'ok': False, 'error': 'credentials_not_entered'}))
        return 2
    result = search_scopus('TITLE-ABS-KEY(combustion)', count=1)
    print(json.dumps({'checkpoint': 'scopus_search', **{k:v for k,v in result.items() if k != 'records'},
                      'returned_records': len(result.get('records', []))}))
    record_ok = False
    if result.get('records') and result['records'][0].get('eid'):
        rec = get_scopus_record(result['records'][0]['eid'])
        record_ok = bool(rec.get('ok') and rec.get('record'))
        print(json.dumps({'checkpoint':'scopus_record', **{k:v for k,v in rec.items() if k != 'record'},
                          'record_present':bool(rec.get('record'))}))
    creds = Path(os.environ.get('CREDENTIALS_DIRECTORY', '/nonexistent')) / 'elsevier.json'
    config = json.loads(creds.read_text()) if creds.exists() else {}
    doi = config.get('PROBE_DOI')
    if not doi:
        selection = search_scopus('SRCTITLE("Combustion and Flame") AND DOCTYPE(ar)', count=1)
        doi = next((x.get('doi') for x in selection.get('records', []) if x.get('doi')), None)
    if doi:
        sd = check_sciencedirect_access(doi)
        print(json.dumps({'checkpoint':'sciencedirect_access', **sd}))
    else:
        print(json.dumps({'checkpoint':'sciencedirect_access', 'ok':False, 'error':'probe_doi_not_entered'}))
        sd = {'ok':False}
    passed = bool(result.get('ok') and record_ok and sd.get('ok'))
    print(json.dumps({'checkpoint':'api_verification_summary','ok':passed}))
    return 0 if passed else 1

if __name__ == '__main__':
    sys.exit(main())
