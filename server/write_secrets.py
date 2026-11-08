#!/usr/bin/env python3
"""Run only in the user's interactive SSH terminal; never print secret values."""
import getpass
import json
import os
import re
from pathlib import Path

BASE = Path('/etc/campus-scopus')
FIELDS = [
    ('SCOPUS_API_KEY', 'Scopus API key (hidden; Enter keeps existing): '),
    ('SCIENCEDIRECT_API_KEY', 'ScienceDirect key (hidden; Enter reuses Scopus): '),
    ('ELSEVIER_INST_TOKEN', 'Institutional token (optional, hidden): '),
    ('CONTROL_PLANE_API_KEY', 'OpenAI Tunnel runtime key (hidden; Enter skips): '),
]

def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo in your local interactive SSH terminal.')
    if not os.isatty(0):
        raise SystemExit('Interactive terminal required. No credentials accepted over pipes.')
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(BASE, 0o700)
    dest = BASE / 'secrets.json'
    old = json.loads(dest.read_text()) if dest.exists() else {}
    for key, prompt in FIELDS:
        value = getpass.getpass(prompt).strip()
        if value:
            if any(c in value for c in '\r\n\x00'):
                raise SystemExit('Invalid credential characters.')
            old[key] = value
        elif key == 'SCIENCEDIRECT_API_KEY' and not old.get(key):
            old[key] = old.get('SCOPUS_API_KEY', '')
    tunnel = input('Tunnel ID (non-secret; Enter keeps existing/skips): ').strip()
    if tunnel:
        if not re.fullmatch(r'tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}', tunnel):
            raise SystemExit('Invalid tunnel ID. Copy the complete ID from Platform.')
        old['CONTROL_PLANE_TUNNEL_ID'] = tunnel
    doi = input('Subscribed ScienceDirect DOI for access probe (optional): ').strip()
    if doi:
        if not doi.startswith('10.') or '/' not in doi:
            raise SystemExit('Invalid DOI.')
        old['PROBE_DOI'] = doi
    temporary = BASE / 'secrets.json.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(old, f)
    os.chmod(temporary, 0o600)
    temporary.replace(dest)
    print('Saved securely: root-only credentials. No secret values printed.')
    print('Configured fields:', ', '.join(k for k, v in old.items() if v))

if __name__ == '__main__':
    main()
