"""Pause Tunnel ingress without stopping its independent download descendants."""
import contextlib
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

UNIT = 'campus-scopus-tunnel.service'


def main_pid():
    pid = int(subprocess.check_output(['systemctl','show','--property=MainPID','--value',UNIT],text=True).strip())
    if pid <= 1:
        raise RuntimeError('Tunnel has no running main process; deployment deferred.')
    return pid


def descendants(pid):
    pending, seen = [pid], set()
    while pending:
        parent = pending.pop()
        try:
            threads = list((Path('/proc')/str(parent)/'task').iterdir())
        except FileNotFoundError:
            continue
        for thread in threads:
            try:
                children = (thread/'children').read_text().split()
            except FileNotFoundError:
                continue
            for value in children:
                child = int(value)
                if child not in seen:
                    seen.add(child);pending.append(child)
                    yield child


def is_mcp_server(pid):
    try:
        args = (Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
        return b'/opt/campus-scopus/server.py' in args
    except FileNotFoundError:
        return False


def pause(pid, handles):
    try:
        fd = os.pidfd_open(pid)
        handles.append(fd)
        signal.pidfd_send_signal(fd,signal.SIGSTOP)
        end = time.monotonic()+2
        while True:
            try:
                state=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[0]
            except FileNotFoundError:
                return False
            if state in ('T','t'):
                return True
            if time.monotonic() >= end:
                raise RuntimeError('Unable to pause Tunnel ingress; deployment deferred.')
            time.sleep(.01)
    except ProcessLookupError:
        return False


@contextlib.contextmanager
def paused_ingress():
    handles=[]
    try:
        pid=main_pid()
        if not pause(pid,handles):
            raise RuntimeError('Tunnel changed while pausing; deployment deferred.')
        for child in descendants(pid):
            if is_mcp_server(child):
                pause(child,handles)
        if main_pid()!=pid:
            raise RuntimeError('Tunnel restarted while pausing; deployment deferred.')
        yield
    finally:
        # pidfds retain process identity across service restarts/PID reuse. Never
        # resume a replacement process or signal the legacy download workers.
        for fd in reversed(handles):
            try:
                signal.pidfd_send_signal(fd,signal.SIGCONT)
            except ProcessLookupError:
                pass
            finally:
                os.close(fd)


def main(command):
    if not command:
        raise SystemExit('Pass a deployment command.')
    lock=os.open('/run/campus-scopus-deployment.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    child=None
    def interrupted(signum, frame):
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass
            child.wait()
        raise SystemExit(128+signum)
    previous={s:signal.signal(s,interrupted) for s in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP)}
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with paused_ingress():
            child=subprocess.Popen(command)
            return child.wait()
    finally:
        for s,handler in previous.items():signal.signal(s,handler)
        os.close(lock)


if __name__=='__main__':
    raise SystemExit(main(sys.argv[1:]))
