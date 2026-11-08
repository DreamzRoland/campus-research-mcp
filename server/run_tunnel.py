#!/usr/bin/env python3
"""Read systemd credentials, inject only OpenAI key into the tunnel process."""
import json
import os
import sys
from pathlib import Path

directory = Path(os.environ['CREDENTIALS_DIRECTORY'])
config = json.loads((directory / 'elsevier.json').read_text())
key = config.get('CONTROL_PLANE_API_KEY')
tunnel = config.get('CONTROL_PLANE_TUNNEL_ID')
if not key or not tunnel:
    raise SystemExit('Tunnel runtime key and tunnel ID must be entered in the local terminal.')
env = os.environ.copy()
env['CONTROL_PLANE_API_KEY'] = key
env['CONTROL_PLANE_TUNNEL_ID'] = tunnel
env['PYTHONUNBUFFERED'] = '1'
args = ['tunnel-client', 'doctor', '--profile', 'campus-scopus', '--explain',
        '--health.listen-addr', '127.0.0.1:0'] if '--doctor' in sys.argv else [
    'tunnel-client', 'run', '--profile', 'campus-scopus', '--health.listen-addr', '127.0.0.1:8088']
os.execve('/usr/local/bin/tunnel-client', args, env)
