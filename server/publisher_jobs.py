"""Private background jobs for ASME/ACS/MDPI, independent of MCP request lifetime."""
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from article_downloads import DownloadError,LIFETIME
from publisher_downloads import DOWNLOAD_SECONDS,validate_request,prepare_publisher_pdf

def job_dir():
    p=Path(os.environ.get('CAMPUS_PUBLISHER_JOB_DIR','/var/lib/campus-scopus/publisher-jobs'))
    p.mkdir(mode=0o700,parents=True,exist_ok=True);p.chmod(0o700);return p

def job_path(job_id):
    if not isinstance(job_id,str) or not re.fullmatch('[a-f0-9]{48}',job_id):raise DownloadError('无效的出版社下载任务ID。')
    return job_dir()/(job_id+'.json')

def write_state(job_id,state):
    p=job_path(job_id);tmp=p.with_name(p.name+'.'+secrets.token_hex(6)+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as stream:json.dump(state,stream);stream.flush();os.fsync(stream.fileno())
        tmp.replace(p)
    finally:tmp.unlink(missing_ok=True)

def read_state(job_id):
    try:state=json.loads(job_path(job_id).read_text())
    except (OSError,ValueError):raise DownloadError('出版社下载任务不存在或已过期。') from None
    if state.get('job_id')!=job_id or time.time()>state.get('expires_at',0):raise DownloadError('出版社下载任务不存在或已过期。')
    return state

def alive(job_id):
    with open(job_dir()/(job_id+'.lock'),'a') as stream:
        os.fchmod(stream.fileno(),0o600)
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
        return False

def status(job_id,wait_seconds=0):
    if isinstance(wait_seconds,bool) or not isinstance(wait_seconds,int) or not 0<=wait_seconds<=20:raise DownloadError('wait_seconds须为0至20。')
    stop=time.monotonic()+wait_seconds
    while True:
        state=read_state(job_id)
        if state['status'] in ('queued','running') and time.time()-state['created_at']>20 and not alive(job_id):
            state=read_state(job_id)
            if state['status'] in ('queued','running'):
                state.update(status='failed',phase='failed',error='下载进程已结束，可能经历服务重启；请重新启动下载。',updated_at=time.time());write_state(job_id,state)
        if state['status'] in ('completed','failed') or time.monotonic()>=stop:break
        time.sleep(min(1,max(0,stop-time.monotonic())))
    public={k:v for k,v in state.items() if k!='cache_token'}
    end=state['updated_at'] if state['status'] in ('completed','failed') else time.time()
    public.update(ok=state['status']!='failed',elapsed_seconds=round(end-state['created_at']),space_saved=False)
    if state['status'] in ('queued','running'):public.update(next_tool='get_publisher_download',retry_after_seconds=20)
    return public,state.get('cache_token')

def start(provider,doi):
    validate_request(provider,doi)
    with open(job_dir()/'start.lock','a') as lock:
        os.fchmod(lock.fileno(),0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        now=time.time();active=[];terminal=[]
        for p in job_dir().glob('*.json'):
            if not re.fullmatch('[a-f0-9]{48}',p.stem):continue
            try:state=json.loads(p.read_text())
            except (OSError,ValueError):continue
            if state.get('expires_at',0)<now:
                if alive(p.stem):continue
                p.unlink(missing_ok=True);(job_dir()/(p.stem+'.lock')).unlink(missing_ok=True);continue
            if state.get('status') in ('queued','running'):
                result,_=status(p.stem)
                if result['status'] in ('queued','running'):active.append(result)
                else:terminal.append((state.get('created_at',0),p))
            else:terminal.append((state.get('created_at',0),p))
        for state in active:
            if state['provider']==provider and state['doi'].lower()==doi.lower():return state
        if len(active)>=2:raise DownloadError('ASME/ACS/MDPI已有两个下载任务，请等待其完成。')
        # Leave room for this new terminal record after it finishes.
        for _,p in sorted(terminal)[:-127]:
            p.unlink(missing_ok=True);(job_dir()/(p.stem+'.lock')).unlink(missing_ok=True)
        job_id=secrets.token_hex(24)
        state={'job_id':job_id,'provider':provider,'doi':doi,'status':'queued','phase':'queued','created_at':now,'updated_at':now,
            'expires_at':now+LIFETIME,'received_bytes':0,'total_bytes':None,'download_deadline_seconds':DOWNLOAD_SECONDS}
        write_state(job_id,state)
        env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','CAMPUS_PUBLISHER_JOB_DIR','CAMPUS_DOWNLOAD_DIR')}
        env['PYTHONDONTWRITEBYTECODE']='1'
        try:subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),'--worker',job_id],env=env,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)
        except OSError:
            state.update(status='failed',phase='failed',error='无法启动出版社下载进程。',updated_at=time.time());write_state(job_id,state)
            raise DownloadError('无法启动出版社下载进程。') from None
        return status(job_id)[0]

def worker(job_id):
    with open(job_dir()/(job_id+'.lock'),'a') as lock:
        os.fchmod(lock.fileno(),0o600)
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        state=read_state(job_id)
        def update(**fields):state.update(fields,updated_at=time.time());write_state(job_id,state)
        try:
            update(status='running');info,token=prepare_publisher_pdf(state['provider'],state['doi'],update)
            update(status='completed',phase='completed',file=info,cache_token=token,received_bytes=info['byte_size'])
        except DownloadError as exc:update(status='failed',phase='failed',error=str(exc))
        except Exception:update(status='failed',phase='failed',error='出版社下载或校验异常；未交付文件，详细信息未输出。')

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--worker':job_path(sys.argv[2]);worker(sys.argv[2])
    else:raise SystemExit('Run via campus MCP tools.')
