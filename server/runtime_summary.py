"""Non-secret operational evidence from the live tunnel health endpoint."""
import json,subprocess,urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://127.0.0.1:8088/health?details=true',timeout=6) as r:
    health=json.load(r)
parts=health.get('components',{})
def component(name):
    c=parts.get(name,{})
    return {k:c[k] for k in ['status','state','reason_code','observed_at','details'] if k in c}
units={}
for name in ['campus-scopus-tunnel','campus-scopus-proxy']:
    lines=subprocess.check_output(['systemctl','show',name,'-p','ActiveState','-p','SubState','-p','UnitFileState'],text=True)
    units[name]=dict(line.split('=',1) for line in lines.splitlines() if '=' in line)
print(json.dumps({'snapshot_at':health.get('snapshot_at'),'live':health.get('live'),'ready':health.get('ready'),
                  'services':units,'control_plane':component('control-plane'),'mcp':component('mcp'),
                  'dispatcher':component('dispatcher'),'response_delivery':component('response-delivery')},indent=2))
