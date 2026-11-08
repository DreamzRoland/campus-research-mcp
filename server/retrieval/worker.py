"""Independent systemd worker; holds an OS singleton lock and renews job leases."""
import concurrent.futures
import fcntl
import json
import os
import signal
import threading
import time
from pathlib import Path
from . import engine, jobs, store


def secret(name):
    d = os.environ.get('CREDENTIALS_DIRECTORY')
    if d:
        p = Path(d) / 'elsevier.json'
        if p.is_file():
            return json.loads(p.read_text()).get(name, '')
    return os.environ.get(name, '')


def execute(row):
    jobid = row['id']
    p = json.loads(row['payload'])
    try:
        if row['kind'] == 'search':
            result = engine.deep(p['plan'], secret, lambda: jobs.cancelled(jobid),
                                 lambda snap: jobs.checkpoint(jobid, snap), json.loads(row['result']) if row['result'] else None,
                                 p.get('citation_expansion', False))
        elif row['kind'] == 'download':
            from .files import download
            result = download(p['record_id'], lambda snap: jobs.checkpoint(jobid, snap), lambda: jobs.cancelled(jobid), secret)
        else:
            from .repositories import harvest
            result = harvest(p['repository'], lambda snap: jobs.checkpoint(jobid, snap), lambda: jobs.cancelled(jobid))
        jobs.finish(jobid, result)
    except Exception as exc:
        # Only fixed local error codes are exposed; never str(network exception) or traceback.
        from .transport import SourceError
        error = exc.code if isinstance(exc, SourceError) else 'task_failed_check_source_status'
        jobs.finish(jobid, {}, error)


def main(once=False):
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    lock = (store.root() / 'worker.lock').open('a')
    lock_path = store.root() / 'worker.lock'
    lock_path.chmod(0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    pools = {k: concurrent.futures.ThreadPoolExecutor(max_workers=n) for k, n in (('search', 2), ('download', 2), ('harvest', 1))}
    active = {k: {} for k in pools}
    cycles = 0
    try:
        while not stop.is_set():
            jobs.recover()
            for kind, pool in pools.items():
                for jid, fut in list(active[kind].items()):
                    if fut.done():
                        active[kind].pop(jid)
                maximum = 1 if kind == 'harvest' else 2
                while len(active[kind]) < maximum and not stop.is_set():
                    row = jobs.claim(kind)
                    if not row:
                        break
                    active[kind][row['id']] = pool.submit(execute, row)
            ids = [jid for a in active.values() for jid in a]
            if ids:
                with store.connect() as db:
                    db.executemany('UPDATE jobs SET lease=? WHERE id=? AND state=\'running\'', [(time.time() + 60, jid) for jid in ids])
            cycles += 1
            if cycles % 10 == 1:
                store.capability('_worker', {'status': 'running', 'heartbeat_at': time.time()})
            if cycles % 60 == 0:
                store.cleanup()
                from .files import cleanup
                cleanup()
            if once and not ids:
                break
            stop.wait(1)
    finally:
        # systemd eventually terminates in-flight work. Its persisted checkpoint is recoverable.
        for pool in pools.values():
            pool.shutdown(wait=True, cancel_futures=True)
        lock.close()


if __name__ == '__main__':
    import sys
    main('--once' in sys.argv)
