"""Persistent task API. Work is executed only by the separately supervised worker."""
import json
import time
from . import store


def enqueue(kind, payload):
    if kind not in ('search', 'download', 'harvest'):
        raise ValueError('invalid_job_kind')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute("SELECT id,payload FROM jobs WHERE kind=? AND state IN ('queued','running')", (kind,)).fetchall()
        for row in existing:
            if json.loads(row['payload']) == payload:
                db.rollback()
                return status(row['id'])
        if len(existing) >= 20:
            db.rollback()
            raise ValueError('job_queue_full')
        jobid = store.identifier('j_')
        now = time.time()
        db.execute('INSERT INTO jobs(id,kind,state,payload,created,updated) VALUES(?,?,?,?,?,?)',
                   (jobid, kind, 'queued', store.encode(payload), now, now))
        db.commit()
    return status(jobid)


def status(job_id, wait_seconds=0):
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 20:
        raise ValueError('wait_seconds_must_be_0_to_20')
    if not isinstance(job_id, str) or len(job_id) != 50 or not job_id.startswith('j_'):
        raise ValueError('invalid_job_id')
    end = time.monotonic() + wait_seconds
    while True:
        with store.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('job_not_found_or_expired')
        if row['state'] not in ('queued', 'running') or time.monotonic() >= end:
            break
        time.sleep(min(.25, max(0, end - time.monotonic())))
    out = {'ok': True, 'job_id': job_id, 'kind': row['kind'], 'state': row['state'],
           'progress': json.loads(row['progress']), 'created_at': row['created'], 'updated_at': row['updated'],
           'cancel_requested': bool(row['cancel']), 'worker_lease_active': row['lease'] > time.time(),
           'space_saved': False}
    heartbeat = store.capability('_worker') or {}
    out['worker_recently_seen'] = time.time() - heartbeat.get('heartbeat_at', 0) < 30
    if row['result']:
        result = json.loads(row['result'])
        if row['kind'] == 'search' and 'plan' in result:
            from .engine import render
            out['result'] = render(result)
        else:
            out['result'] = result
    if row['state'] in ('queued', 'running'):
        out['next_action'] = 'Poll get_research_job with the same job_id; queued/running is not a failure.'
        if row['state'] == 'queued' and not out['worker_recently_seen'] and time.time() - row['created'] > 30:
            out['warning'] = 'worker_not_recently_seen_check_campus_research_worker_service'
    return out


def cancel(job_id):
    status(job_id)
    with store.connect() as db:
        db.execute("UPDATE jobs SET cancel=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated=? WHERE id=? AND state IN ('queued','running')",
                   (time.time(), job_id))
    return status(job_id)


def cancelled(jobid):
    with store.connect() as db:
        row = db.execute('SELECT cancel FROM jobs WHERE id=?', (jobid,)).fetchone()
    return not row or bool(row[0])


def checkpoint(jobid, snapshot):
    if snapshot.get('plan') and snapshot.get('run_id'):
        store.save_run(snapshot, snapshot['run_id'])
    with store.connect() as db:
        if snapshot.get('plan'):
            progress = {'phase': 'searching', 'records': len(snapshot.get('results', [])), 'requests_used': snapshot.get('requests_used', 0), 'run_id': snapshot.get('run_id')}
            db.execute('UPDATE jobs SET progress=?,result=?,updated=?,lease=? WHERE id=?',
                       (store.encode(progress), store.encode(snapshot), time.time(), time.time() + 60, jobid))
        else:
            db.execute('UPDATE jobs SET progress=?,updated=?,lease=? WHERE id=?', (store.encode(snapshot), time.time(), time.time() + 60, jobid))


def claim(kind):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM jobs WHERE kind=? AND state='queued' AND cancel=0 ORDER BY created LIMIT 1", (kind,)).fetchone()
        if not row:
            db.rollback()
            return None
        db.execute("UPDATE jobs SET state='running',lease=?,updated=? WHERE id=?", (time.time() + 60, time.time(), row['id']))
        db.commit()
    return dict(row)


def finish(jobid, result, error=None):
    state = 'failed' if error else 'cancelled' if cancelled(jobid) else 'completed'
    if error:
        result = {'ok': False, 'error': error}
    with store.connect() as db:
        db.execute('UPDATE jobs SET state=?,result=?,updated=?,lease=0 WHERE id=?', (state, store.encode(result), time.time(), jobid))


def recover():
    with store.connect() as db:
        db.execute("UPDATE jobs SET state=CASE WHEN cancel=1 THEN 'cancelled' ELSE 'queued' END,lease=0 WHERE state='running' AND lease<?", (time.time(),))
