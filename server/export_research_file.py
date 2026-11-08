#!/usr/bin/env python3
"""Prepare one verified large file for existing admin SSH/SCP access; run with sudo."""
import hashlib
import json
import os
import pwd
import shutil
import sys
from pathlib import Path
from retrieval import files

def main():
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise SystemExit('Usage: sudo python3 export_research_file.py f_<id>')
    service = pwd.getpwnam('campus-scopus')
    os.setegid(service.pw_gid)
    os.seteuid(service.pw_uid)
    info = files.file_info(sys.argv[1])
    src = files.directory() / (info['file_id'] + '.bin')
    source = src.open('rb')
    digest = hashlib.sha256()
    for block in iter(lambda: source.read(65536), b''):
        digest.update(block)
    if src.stat().st_size != info['byte_size'] or digest.hexdigest() != info['sha256']:
        raise SystemExit('File integrity check failed.')
    admin = pwd.getpwnam('admin')
    os.seteuid(0)
    os.setegid(0)
    os.setgroups([])
    os.setgid(admin.pw_gid)
    os.setuid(admin.pw_uid)
    dest = Path(admin.pw_dir) / 'campus-research-exports'
    dest.mkdir(mode=0o700, exist_ok=True)
    if dest.is_symlink():
        raise SystemExit('Export directory must not be a symlink.')
    dest.chmod(0o700)
    for old in dest.glob('f_*.pdf'):
        if __import__('time').time() - old.stat().st_mtime > 86400:
            old.unlink()
    target = dest / (info['file_id'] + '.pdf')
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    source.seek(0)
    with os.fdopen(descriptor, 'wb') as out, source:
        shutil.copyfileobj(source, out)
    print(json.dumps({'byte_size': info['byte_size'], 'sha256': info['sha256'],
                      'download_command': 'scp research-host:' + str(target) + ' ./'+ info['file_name'],
                      'remove_after_download': 'ssh research-host "rm -- ' + str(target) + '"'}, ensure_ascii=False))

if __name__ == '__main__':
    main()
