"""Disposable, revision-incremental SQLite BM25 storage (stdlib only).

No authorization or lifecycle decisions live here. Callers supply the verified
readable set on *every* request. SQL restricts both postings and corpus statistics
before vendored Okapi scoring. A separate process-lifetime lock makes automatic
corruption replacement safe even on Windows (no open database is renamed).
"""
from collections import Counter
from contextlib import contextmanager
import hashlib
import errno
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import tempfile

import operation_guard

WINDOW = 1800
STRIDE = 1560
SCHEMA = 'personal-kb.search.sqlite.v1'
MAX_QUERY_POSTINGS = 1_000_000
VENDOR = Path(__file__).resolve().parent.parent / 'vendor/claude-obsidian/scripts/bm25-index.py'


def vendor():
    spec = importlib.util.spec_from_file_location('_pkb_persistent_bm25', VENDOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def identity(entry):
    return digest([entry.get(k) for k in ('document_id', 'revision_id', 'extraction_id',
                                         'extracted_text_sha256', 'sha256')])


def engine_id():
    return digest([SCHEMA, WINDOW, STRIDE, 'han-whitespace-v1',
                   hashlib.sha256(VENDOR.read_bytes()).hexdigest()])


def _safe(path, kind=None):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise OSError('搜索缓存路径不能是符号链接或重解析点')
    if (kind == 'file' and not stat.S_ISREG(info.st_mode)) or (kind == 'directory' and not stat.S_ISDIR(info.st_mode)):
        raise OSError('搜索缓存路径类型不正确，请检查文件权限与位置')


@contextmanager
def locked(root, timeout=120):
    root = Path(root)
    # Do not create a cache outside the selected store via an existing alias.
    _safe(root, 'directory')
    root = root.resolve(strict=True)
    directory = root / 'derived-search'
    _safe(directory, 'directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    lock_path = directory / 'index.lock'
    _safe(lock_path, 'file')
    with operation_guard._open(lock_path, True) as handle:
        # msvcrt locks one byte; create it before requesting the range lock.
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0'); handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                operation_guard._kernel_lock(handle)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise OSError('无法取得搜索索引锁，请检查文件系统支持与权限') from exc
                if time.monotonic() >= deadline:
                    raise OSError('搜索索引正由另一进程维护；请稍后重试') from exc
                time.sleep(0.05)
        try:
            for name in ('index.sqlite3', 'index.sqlite3-journal', 'index.sqlite3-wal', 'index.sqlite3-shm', 'index.seal.json'):
                _safe(directory / name, 'file')
            yield directory / 'index.sqlite3'
        finally:
            operation_guard._kernel_lock(handle, release=True)


def _connect(path):
    db = sqlite3.connect(str(path), timeout=120)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        # A bounded SQLite page cache, not a deserialized whole-corpus Python dict.
        db.execute('PRAGMA cache_size=-8192')
        db.execute('PRAGMA temp_store=FILE')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            db.executescript('''
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE documents (key TEXT PRIMARY KEY, text_hash TEXT NOT NULL,
                    characters INTEGER NOT NULL, chunks INTEGER NOT NULL);
                CREATE TABLE chunks (id INTEGER PRIMARY KEY, document TEXT NOT NULL
                    REFERENCES documents(key) ON DELETE CASCADE, start INTEGER NOT NULL,
                    dl INTEGER NOT NULL, UNIQUE(document,start));
                CREATE TABLE postings (term TEXT NOT NULL, chunk INTEGER NOT NULL
                    REFERENCES chunks(id) ON DELETE CASCADE, count INTEGER NOT NULL,
                    PRIMARY KEY(term,chunk)) WITHOUT ROWID;
                CREATE INDEX postings_chunk ON postings(chunk);
            ''')
            db.executemany('INSERT INTO meta VALUES (?,?)', [('engine', engine_id()), ('builds', '0')])
            db.commit()
        if db.execute("SELECT value FROM meta WHERE key='engine'").fetchone() != (engine_id(),):
            raise sqlite3.DatabaseError('search engine changed')
        builds = db.execute("SELECT value FROM meta WHERE key='builds'").fetchone()
        if builds is None or not builds[0].isdigit():
            raise sqlite3.DatabaseError('invalid search build counter')
        # Validate table/column availability even for empty queries.
        db.execute('SELECT d.text_hash,d.characters,d.chunks,c.start,c.dl,p.count FROM documents d '
                   'LEFT JOIN chunks c ON c.document=d.key LEFT JOIN postings p ON p.chunk=c.id LIMIT 0')
        if db.execute('SELECT d.key FROM documents d LEFT JOIN chunks c ON c.document=d.key '
                      'GROUP BY d.key HAVING d.chunks != count(c.id) LIMIT 1').fetchone():
            raise sqlite3.DatabaseError('incomplete cached document')
        return db
    except BaseException:
        db.close()
        raise


def file_digest(path):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def sealed_digest(path):
    seal = path.parent / 'index.seal.json'
    try:
        if seal.stat().st_size > 4096:
            return None
        value = json.loads(seal.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('engine') != engine_id():
            return None
        actual = file_digest(path)
        return actual if actual == value.get('sha256') else None
    except (FileNotFoundError, ValueError, UnicodeError):
        return None


def publish_seal(path, previous):
    actual = file_digest(path)
    if actual == previous:
        return
    # The SQLite handle is closed. Under index.lock a reader sees either the
    # completed DB + matching seal or automatically discards the incomplete pair.
    # Atomic replace touches only a closed, tiny JSON file on Windows.
    fd, name = tempfile.mkstemp(prefix='.seal-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump({'engine': engine_id(), 'sha256': actual}, handle)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path.parent / 'index.seal.json')
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def database(root):
    with locked(root) as path:
        recovered = False
        previous = None
        try:
            if path.exists():
                previous = sealed_digest(path)
                if previous is None:
                    raise sqlite3.DatabaseError('search seal missing or mismatched')
            db = _connect(path)
        except sqlite3.DatabaseError:
            # All clients hold index.lock; _connect closed its handle first.
            # Only disposable files are removed. A crash at any step leaves
            # either a verified pair or an automatically rebuildable cache miss.
            for suffix in ('', '-journal', '-wal', '-shm'):
                Path(str(path) + suffix).unlink(missing_ok=True)
            (path.parent / 'index.seal.json').unlink(missing_ok=True)
            db = _connect(path)
            previous = None
            recovered = True
        success = False
        try:
            yield db, recovered
            success = True
        finally:
            db.close()
            if success:
                publish_seal(path, previous)


def ensure(db, entries, get_text, module, *, prune=False):
    wanted = {identity(entry): entry for entry in entries}
    stored = dict(db.execute('SELECT key,text_hash FROM documents'))
    existing = set(stored)
    invalid = {key for key, entry in wanted.items() if key in stored and
               entry.get('extracted_text_sha256') and stored[key] != entry['extracted_text_sha256']}
    missing = (wanted.keys() - existing) | invalid
    removed = existing - wanted.keys() if prune else set()
    built_chunks = 0
    # Both document-complete markers and every posting publish in one transaction.
    # Interrupt/kill before commit rolls back, without a persistent stale lock.
    with db:
        db.executemany('DELETE FROM documents WHERE key=?', ((key,) for key in invalid))
        for key in sorted(missing):
            text = get_text(wanted[key])
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            starts = range(0, len(text), STRIDE)
            db.execute('INSERT INTO documents VALUES (?,?,?,?)', (key, text_hash, len(text), len(starts)))
            for start in starts:
                raw = text[start:start + WINDOW]
                ranking = re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', raw)
                tokens = module.tokenize(ranking)  # Never skip or truncate failed chunks.
                cursor = db.execute('INSERT INTO chunks(document,start,dl) VALUES (?,?,?)',
                                    (key, start, len(tokens)))
                db.executemany('INSERT INTO postings VALUES (?,?,?)',
                               ((term, cursor.lastrowid, count) for term, count in Counter(tokens).items()))
                built_chunks += 1
        db.executemany('DELETE FROM documents WHERE key=?', ((key,) for key in removed))
        if missing:
            db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='builds'")
    return {'status': 'ready', 'storage': SCHEMA, 'built_documents': len(missing),
            'built_chunks': built_chunks, 'removed_documents': len(removed),
            'build_count': int(db.execute("SELECT value FROM meta WHERE key='builds'").fetchone()[0])}


def rank(root, entries, get_text, query=None, *, prune=False):
    """Query None maintains the supplied set. get_text verifies release provenance."""
    module = vendor()
    # Check query budget before mutating an optional cache.
    qterms = module.tokenize(query) if query is not None else []
    with database(root) as (db, recovered):
        metrics = ensure(db, entries, get_text, module, prune=prune)
        metrics['recovered_cache'] = recovered
        if query is None:
            return metrics
        db.execute('CREATE TEMP TABLE selected (key TEXT PRIMARY KEY, position INTEGER NOT NULL)')
        db.executemany('INSERT INTO selected VALUES (?,?)', ((identity(e), i) for i, e in enumerate(entries)))
        count, total_dl = db.execute('SELECT count(*),coalesce(sum(c.dl),0) FROM chunks c '
                                    'JOIN selected s ON s.key=c.document').fetchone()
        # Only query-term postings enter Python. Corpus statistics use the exact
        # allowed set (including repeated text in distinct document identities).
        docs, vocab, locations = {}, {}, {}
        loaded_postings = 0
        for term in dict.fromkeys(qterms):
            rows = db.execute('SELECT c.id,c.dl,c.start,s.position,p.count '
                              'FROM postings p JOIN chunks c ON c.id=p.chunk '
                              'JOIN selected s ON s.key=c.document WHERE p.term=? '
                              'ORDER BY s.position,c.start', (term,))
            postings = []
            for cid, dl, start, position, tf in rows:
                loaded_postings += 1
                if loaded_postings > MAX_QUERY_POSTINGS:
                    raise ValueError('查询命中词条超过单次内存预算，请缩短问题或使用更具体的产品词；未返回截断结果')
                cid = str(cid)
                docs[cid] = {'path': cid, 'dl': dl}
                locations[cid] = (position, start)
                postings.append([cid, tf])
            if postings:
                vocab[term] = {'df': len(postings), 'postings': postings}
        index = {'vocab': vocab, 'docs': docs, 'params': {'k1': module.K1, 'b': module.B},
                 'doc_count': count, 'avg_dl': total_dl / count if count else 0}
        module.load_index = lambda: index
        scores = module.query(query, top_k=module.MAX_TOP_RESULTS)
        by_document = {}
        for hit in scores:
            position, start = locations[hit['chunk_id']]
            if position not in by_document:
                text = get_text(entries[position])
                row = db.execute('SELECT text_hash,characters FROM documents WHERE key=?',
                                 (identity(entries[position]),)).fetchone()
                if row != (hashlib.sha256(text.encode()).hexdigest(), len(text)) or not 0 <= start < len(text) or start % STRIDE:
                    raise ValueError('搜索缓存与可验证全文不一致，请更新我的资料库')
                by_document[position] = {'score': hit['score'], 'excerpt': text[start:start + 650],
                                         'excerpt_start': start}
        metrics['loaded_postings'] = loaded_postings
        return by_document, count, len(scores) == module.MAX_TOP_RESULTS, metrics
