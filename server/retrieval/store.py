"""Private SQLite state shared by short MCP sessions and the independent worker."""
import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import time
import threading
from pathlib import Path

SCHEMA = 1
_init_lock = threading.RLock()
_initialized = set()


def root():
    p = Path(os.environ.get('CAMPUS_RESEARCH_DIR', '/var/lib/campus-scopus/research'))
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.chmod(0o700)
    return p


@contextlib.contextmanager
def connect():
    p = root() / 'research.sqlite3'
    db = sqlite3.connect(p, timeout=15, isolation_level=None)
    p.chmod(0o600)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    try:
        with _init_lock:
            key = (str(p), p.stat().st_ino)
            if key not in _initialized:
                for attempt in range(30):
                    try:
                        db.execute('PRAGMA journal_mode=WAL')
                        break
                    except sqlite3.OperationalError as exc:
                        if 'locked' not in str(exc) or attempt == 29:
                            raise
                        time.sleep(.1)
                initialize(db)
                _initialized.add(key)
        if db.execute('SELECT max(version) FROM schema_version').fetchone()[0] != SCHEMA:
            raise ValueError('unsupported_database_schema')
        yield db
    finally:
        db.close()


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO schema_version VALUES(1);
        CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
          payload TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS record_aliases(alias TEXT PRIMARY KEY,record_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,payload TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS cache(id TEXT PRIMARY KEY,payload TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS rate(provider TEXT PRIMARY KEY,next_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS capabilities(provider TEXT PRIMARY KEY,payload TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,state TEXT NOT NULL,
          payload TEXT NOT NULL,progress TEXT NOT NULL DEFAULT '{}',result TEXT,created REAL NOT NULL,
          updated REAL NOT NULL,lease REAL NOT NULL DEFAULT 0,cancel INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,state TEXT NOT NULL,size INTEGER NOT NULL,
          payload TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS harvest(provider TEXT PRIMARY KEY,payload TEXT NOT NULL,updated REAL NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS repository_fts USING fts5(record_id UNINDEXED,provider UNINDEXED,title,abstract);
    ''')


def encode(v):
    return json.dumps(v, ensure_ascii=False, separators=(',', ':'))


def identifier(prefix):
    return prefix + secrets.token_hex(24)


def save_record(record, identity):
    now = time.time()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT id,payload FROM records WHERE identity=?', (identity,)).fetchone()
        aliases = record_aliases(record)
        if not row:
            for alias in aliases:
                row = db.execute('SELECT r.id,r.payload FROM records r JOIN record_aliases a ON r.id=a.record_id WHERE a.alias=?', (alias,)).fetchone()
                if row:
                    break
        rid = row['id'] if row else identifier('r_')
        if row:
            from .models import merge_record
            old = json.loads(row['payload'])
            previous_doi, incoming_doi = old.get('identifiers', {}).get('doi'), record.get('identifiers', {}).get('doi')
            if previous_doi and incoming_doi and previous_doi != incoming_doi:
                db.rollback()
                raise ValueError('conflicting_verified_record_identifiers')
            record = merge_record(old, record)
        record['record_id'] = rid
        if row:
            db.execute('UPDATE records SET payload=?,updated=? WHERE id=?', (encode(record), now, rid))
        else:
            db.execute('INSERT INTO records VALUES(?,?,?,?)', (rid, identity, encode(record), now))
        for alias in record_aliases(record):
            db.execute('INSERT OR IGNORE INTO record_aliases VALUES(?,?)', (alias, rid))
        db.commit()
    return record


def record_aliases(record):
    prefix = 'thesis:' if record.get('type') == 'thesis' else 'work:'
    aliases = [prefix + source + ':' + str(meta['id']) for source, meta in record.get('source_records', {}).items()]
    for kind in ('doi', 'pmid', 'arxiv', 'handle'):
        value = record.get('identifiers', {}).get(kind)
        if value:
            if kind == 'arxiv':
                import re
                value = re.sub(r'v\d+$', '', value)
            aliases.append(prefix + kind + ':' + value)
    return aliases


def get_record(rid):
    with connect() as db:
        row = db.execute('SELECT payload FROM records WHERE id=?', (rid,)).fetchone()
    if not row:
        raise ValueError('record_not_found_or_expired')
    return json.loads(row[0])


def save_run(data, run_id=None):
    rid = run_id or identifier('s_')
    with connect() as db:
        db.execute('INSERT INTO runs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
                   (rid, encode(data), time.time()))
    return rid


def get_run(rid):
    with connect() as db:
        row = db.execute('SELECT payload FROM runs WHERE id=?', (rid,)).fetchone()
    if not row:
        raise ValueError('search_run_not_found_or_expired')
    return json.loads(row[0])


def cache_key(provider, request):
    return hashlib.sha256((provider + encode(request)).encode()).hexdigest()


def cached(key):
    with connect() as db:
        row = db.execute('SELECT payload FROM cache WHERE id=? AND expires>?', (key, time.time())).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(key, payload, ttl=86400):
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)', (key, encode(payload), time.time() + ttl))


def reserve_rate(provider, interval, deadline):
    """Reserve a slot transactionally; never hold a DB lock during network or sleep."""
    now = time.time()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT next_at FROM rate WHERE provider=?', (provider,)).fetchone()
        slot = max(now, row[0] if row else now)
        if slot - now > max(0, deadline - time.monotonic()):
            db.rollback()
            raise ValueError('source_rate_limit_deadline')
        db.execute('INSERT OR REPLACE INTO rate VALUES(?,?)', (provider, slot + interval))
        db.commit()
    delay = slot - time.time()
    if delay > 0:
        time.sleep(delay)


def backoff(provider, seconds):
    with connect() as db:
        db.execute('INSERT INTO rate VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET next_at=max(next_at,excluded.next_at)',
                   (provider, time.time() + min(300, max(1, seconds))))


def capability(provider, evidence=None):
    with connect() as db:
        if evidence is not None:
            db.execute('INSERT OR REPLACE INTO capabilities VALUES(?,?,?)', (provider, encode(evidence), time.time()))
            return evidence
        row = db.execute('SELECT payload,updated FROM capabilities WHERE provider=?', (provider,)).fetchone()
    return {**json.loads(row[0]), 'checked_at': row[1]} if row else None


def cleanup():
    now = time.time()
    with connect() as db:
        db.execute('DELETE FROM cache WHERE expires<?', (now,))
        db.execute('DELETE FROM runs WHERE updated<?', (now - 30 * 86400,))
        db.execute("DELETE FROM jobs WHERE updated<? AND state NOT IN ('queued','running')", (now - 30 * 86400,))
        # Stable records survive while referenced by runs; old unreferenced records expire.
        db.execute('DELETE FROM records WHERE updated<? AND id NOT IN (SELECT record_id FROM repository_fts)', (now - 30 * 86400,))
        db.execute('DELETE FROM record_aliases WHERE record_id NOT IN (SELECT id FROM records)')
