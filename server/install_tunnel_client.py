#!/usr/bin/env python3
"""Download latest official full tunnel-client; verify publisher SHA256 list."""
import hashlib
import io
import json
import os
import platform
import tempfile
import urllib.request
import zipfile
from pathlib import Path

def read(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'campus-scopus-installer'}), timeout=120) as r:
        return r.read()

def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo.')
    arch = {'x86_64':'amd64', 'aarch64':'arm64'}.get(platform.machine())
    if not arch:
        raise SystemExit('Unsupported architecture.')
    release = json.loads(read('https://api.github.com/repos/openai/tunnel-client/releases/latest'))
    name = 'tunnel-client-' + release['tag_name'] + '-linux-' + arch + '.zip'
    assets = {a['name']:a['browser_download_url'] for a in release['assets']}
    archive = read(assets[name])
    sums = read(assets['SHA256SUMS.txt']).decode()
    expected = next(line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip('*') == name)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise SystemExit('Checksum mismatch. Nothing installed.')
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        target = next(n for n in z.namelist() if Path(n).name == 'tunnel-client')
        binary = z.read(target)
    fd, tmp = tempfile.mkstemp(prefix='campus-tunnel-', dir='/usr/local/bin')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(binary)
        os.chmod(tmp, 0o755)
        os.replace(tmp, '/usr/local/bin/tunnel-client')
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print('Installed verified official tunnel-client', release['tag_name'])

if __name__ == '__main__':
    main()
