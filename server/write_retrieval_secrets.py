#!/usr/bin/env python3
"""Optional hidden API key entry; preserve all existing keys, never print values."""
import getpass
import json
import os
import tempfile
from pathlib import Path

def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo in your own interactive SSH terminal.')
    path = Path('/etc/campus-scopus/secrets.json')
    data = json.loads(path.read_text()) if path.exists() else {}
    for key, label in [('OPENALEX_API_KEY','OpenAlex optional free key'),('CORE_API_KEY','CORE free key'),
                       ('IEEE_API_KEY','IEEE metadata key'),('SEMANTIC_SCHOLAR_API_KEY','Semantic Scholar key'),
                       ('WFDATA_APP_KEY','Wanfang OpenThesis application key')]:
        value = getpass.getpass(label + ' (hidden; Enter preserves existing): ').strip()
        if value:
            data[key] = value
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.research-key-')
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump(data,out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    print('Optional keys updated; existing provider and Tunnel credentials preserved.')
    print('Restart campus-research-worker and campus-scopus-tunnel only after active tasks finish.')

if __name__=='__main__':main()
