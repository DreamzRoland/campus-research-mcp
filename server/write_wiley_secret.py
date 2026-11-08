#!/usr/bin/env python3
"""Wiley token entry from the user's interactive SSH terminal; never echo it."""
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

def main():
    if os.geteuid()!=0:raise SystemExit('Run with sudo.')
    destination=Path('/etc/campus-scopus/secrets.json')
    data=json.loads(destination.read_text()) if destination.exists() else {}
    if '--status' in sys.argv:
        print(json.dumps({'wiley_tdm_token_configured':bool(data.get('WILEY_TDM_TOKEN'))}));return
    if not os.isatty(0):raise SystemExit('Use your interactive SSH terminal.')
    value=getpass.getpass('Wiley TDM Token (hidden; Enter keeps existing): ').strip()
    if value:
        if len(value)>1024 or any(ord(c)<33 or ord(c)>126 for c in value):raise SystemExit('Invalid token format; nothing saved.')
        data['WILEY_TDM_TOKEN']=value
    if not data.get('WILEY_TDM_TOKEN'):raise SystemExit('No token saved.')
    directory=destination.parent;directory.mkdir(mode=0o700,parents=True,exist_ok=True);directory.chmod(0o700)
    temporary=directory/'wiley-secrets.tmp'
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(data,stream);stream.flush();os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:temporary.unlink(missing_ok=True)
    print('Wiley token saved in root-only credentials. Existing credentials preserved.')
    subprocess.run(['systemctl','restart','campus-scopus-tunnel'],check=True)
    print('Existing Tunnel restarted to reload systemd credentials.')

if __name__=='__main__':main()
