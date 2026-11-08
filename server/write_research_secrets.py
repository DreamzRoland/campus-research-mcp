#!/usr/bin/env python3
"""Extend existing credentials from an interactive user terminal only."""
import getpass
import json
import os
from pathlib import Path

def main():
    if os.geteuid()!=0 or not os.isatty(0):
        raise SystemExit('Run with sudo in your interactive SSH terminal.')
    directory=Path('/etc/campus-scopus')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(directory,0o700)
    destination=directory/'secrets.json'
    data=json.loads(destination.read_text()) if destination.exists() else {}
    fields=[('SPRINGER_META_API_KEY','Springer Nature Meta API key'),
            ('SPRINGER_OA_API_KEY','Springer Nature Open Access API key'),
            ('WOS_API_KEY','Web of Science Starter API key')]
    for field,label in fields:
        value=getpass.getpass(label+' (hidden; Enter keeps existing/skips): ').strip()
        if value:
            if len(value)>1024 or any(c.isspace() or ord(c)<32 for c in value):
                raise SystemExit('Invalid credential format; nothing saved.')
            data[field]=value
    temporary=directory/'research-secrets.tmp'
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(data,stream)
            stream.flush();os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print('Saved root-only credentials. Existing Elsevier and Tunnel values preserved.')
    for field,label in fields: print(label+': '+('configured' if data.get(field) else 'not configured'))

if __name__=='__main__': main()
