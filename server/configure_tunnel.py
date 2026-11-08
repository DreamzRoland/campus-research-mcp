#!/usr/bin/env python3
"""Root-only provisioning after local credential entry. No secrets on argv."""
import json
import os
import re
import subprocess
from pathlib import Path

def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo.')
    p = Path('/etc/campus-scopus/secrets.json')
    if not p.exists():
        raise SystemExit('Credentials have not been entered in the local terminal.')
    cfg = json.loads(p.read_text())
    tunnel = cfg.get('CONTROL_PLANE_TUNNEL_ID', '')
    if not cfg.get('CONTROL_PLANE_API_KEY') or not re.fullmatch(r'tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}', tunnel):
        raise SystemExit('Enter the OpenAI runtime key and valid Tunnel ID first.')
    cmd = ['runuser', '-u', 'campus-scopus', '--', 'env', 'HOME=/var/lib/campus-scopus',
           '/usr/local/bin/tunnel-client', 'init', '--sample', 'sample_mcp_stdio_local',
           '--profile', 'campus-scopus', '--force', '--tunnel-id', tunnel,
           '--mcp-command', '/var/lib/campus-scopus/.venv/bin/python /opt/campus-scopus/server.py',
           '--health-listen-addr', '127.0.0.1:8088']
    subprocess.run(cmd, check=True)
    Path('/etc/campus-scopus/ready').touch(mode=0o600)
    subprocess.run(['systemctl', 'enable', 'campus-scopus-tunnel.service'], check=True)
    subprocess.run(['systemctl', 'reset-failed', 'campus-scopus-tunnel.service'], check=True)
    subprocess.run(['systemctl', 'restart', 'campus-scopus-tunnel.service'], check=True)
    print('Profile and systemd startup configured. Verify doctor and readiness separately.')

if __name__ == '__main__':
    main()
