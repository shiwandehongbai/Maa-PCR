"""Validated CN master updates; each run keeps its own immutable unit facts."""
import contextlib
import copy
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from types import MappingProxyType
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MANIFEST_URL = 'https://github.com/wbero/autopcr-db-builder/releases/latest/download/manifest.json'
DATABASE_URL = 'https://github.com/wbero/autopcr-db-builder/releases/download/db-v{version}/{version}.db'
MAX_DATABASE_BYTES = 512 * 1024 * 1024
MIN_DATABASE_BYTES = 1_000_000
DATABASE_SNAPSHOT = ContextVar('labyrinth_database_snapshot', default=None)
ANCHORS = (100101, 100301, 101101, 102301, 105901, 107501,
           108801, 108901, 109101, 117101, 122501, 124201)


class DatabaseError(ValueError):
    pass


def _version(value):
    if type(value) is not str or re.fullmatch(r'[0-9]{12}', value) is None:
        raise DatabaseError('database_manifest_invalid')
    return value


def _hash(value):
    if type(value) is not str or re.fullmatch(r'[a-fA-F0-9]{64}', value) is None:
        raise DatabaseError('database_manifest_invalid')
    return value.lower()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DatabaseError('database_manifest_invalid')
        result[key] = value
    return result


def _json(raw):
    if len(raw) > 65536:
        raise DatabaseError('database_manifest_invalid')
    value = json.loads(raw, object_pairs_hook=_object)
    if type(value) is not dict:
        raise DatabaseError('database_manifest_invalid')
    return value


def _quoted(name):
    if type(name) is not str or re.fullmatch(r'[A-Za-z0-9_]{1,128}', name) is None:
        raise DatabaseError('database_schema_invalid')
    return '"' + name + '"'


def _schema(db):
    schemas = {}
    for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if re.fullmatch(r'[A-Za-z0-9_]{1,128}', name):
            schemas[name] = tuple(db.execute('PRAGMA table_info(' + _quoted(name) + ')'))
    if len(schemas) > 2000:
        raise DatabaseError('database_schema_invalid')
    return schemas


def _primary(columns):
    found = [row for row in columns if row[5]]
    return found[0][1] if len(found) == 1 and found[0][2].upper().strip() == 'INTEGER' else None


def _resolve(db, schemas):
    # Resolve readable restored columns first; never persist a current hash name.
    def named(columns, kind):
        columns = {row[1].lower() for row in columns}
        has = lambda prefix: any(key.startswith(prefix) for key in columns)
        if kind == 'unit':
            return 'unit_id' in columns and bool({'name', 'unit_name'} & columns)
        if kind == 'unit_skill':
            return 'unit_id' in columns and all(has(key) for key in ('union_burst_', 'main_skill_', 'ex_skill_'))
        if kind == 'skill':
            return {'skill_id', 'name'} <= columns and bool({'description', 'description_2'} & columns) and has('skill_action_')
        return {'action_id', 'action_type'} <= columns and bool({'target_assignment', 'target_type'} & columns) and has('action_detail_')
    resolved = {}
    for kind in ('unit', 'unit_skill', 'skill', 'action'):
        found = [name for name, columns in schemas.items() if named(columns, kind)]
        if len(found) == 1:
            resolved[kind] = found[0]
    if len(resolved) == 4 and len(set(resolved.values())) == 4:
        return resolved

    def structure(columns):
        types = [row[2].upper().strip() for row in columns]
        return types.count('INTEGER'), types.count('TEXT'), types.count('REAL')
    def hits(name, candidates):
        pk = _primary(schemas[name])
        if not pk or not candidates:
            return 0
        count = 0
        values = tuple(sorted(candidates))
        for offset in range(0, len(values), 300):
            chunk = values[offset:offset+300]
            sql = 'SELECT DISTINCT ' + _quoted(pk) + ' FROM ' + _quoted(name) + ' WHERE ' + _quoted(pk) + ' IN (' + ','.join('?' for _ in chunk) + ')'
            count += len(db.execute(sql, chunk).fetchall())
        return count
    def references(name, candidates, low, high):
        pk = _primary(schemas[name])
        columns = [row[1] for row in schemas[name] if row[2].upper().strip() == 'INTEGER']
        values = tuple(sorted(candidates)); found = set()
        for offset in range(0, len(values), 300):
            chunk = values[offset:offset+300]
            sql = 'SELECT ' + ','.join(_quoted(key) for key in columns) + ' FROM ' + _quoted(name) + ' WHERE ' + _quoted(pk) + ' IN (' + ','.join('?' for _ in chunk) + ')'
            for row in db.execute(sql, chunk):
                found.update(value for value in row if type(value) is int and low <= value <= high)
        if len(found) > 10000:
            raise DatabaseError('database_schema_invalid')
        return found
    def choose(kind, candidates, references_set=None):
        if references_set is not None and len(references_set) < 8:
            raise DatabaseError('database_schema_invalid')
        ranked = []
        for name in candidates:
            integer, text, real = structure(schemas[name])
            count = hits(name, references_set or ANCHORS)
            score = count if references_set is not None else count*100 + (min(integer, 50) if kind == 'unit_skill' else integer+text*2+real)
            ranked.append((score, count, name))
        ranked.sort(reverse=True)
        minimum = (len(references_set)*80+99)//100 if references_set is not None else 8
        margin = max(3, len(references_set)//10) if references_set is not None else 5
        if (not ranked or ranked[0][1] < minimum or len(ranked) > 1 and
                (ranked[0][0]-ranked[1][0] < margin)):
            raise DatabaseError('database_schema_invalid')
        return ranked[0][2]
    columns = {name: structure(cols) for name, cols in schemas.items() if _primary(cols)}
    unit = choose('unit', [name for name, (i, t, r) in columns.items() if i >= 10 and 2 <= t <= 10 and 15 <= len(schemas[name]) <= 45])
    unit_skill = choose('unit_skill', [name for name, (i, t, r) in columns.items() if i >= 25 and t == r == 0])
    skill_ids = references(unit_skill, ANCHORS, 1_000_000, 99_999_999)
    skill = choose('skill', [name for name, (i, t, r) in columns.items() if i >= 10 and t >= 2 and 15 <= len(schemas[name]) <= 50], skill_ids)
    action_ids = references(skill, skill_ids, 100_000_000, 9_999_999_999)
    action = choose('action', [name for name, (i, t, r) in columns.items() if i >= 8 and 12 <= len(schemas[name]) <= 50], action_ids)
    if len({unit, unit_skill, skill, action}) != 4:
        raise DatabaseError('database_schema_invalid')
    return dict(unit=unit, unit_skill=unit_skill, skill=skill, action=action)


def validate_database(path, *, cancelled=lambda: False):
    """Read-only bounded validation; returns only mechanically known unit facts."""
    path = Path(path)
    if cancelled():
        raise DatabaseError('database_update_cancelled')
    if not MIN_DATABASE_BYTES <= path.stat().st_size <= MAX_DATABASE_BYTES:
        raise DatabaseError('database_size_invalid')
    with path.open('rb') as stream:
        if stream.read(16) != b'SQLite format 3\0':
            raise DatabaseError('database_header_invalid')
    deadline = time.monotonic() + 10
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: cancelled() or time.monotonic() >= deadline, 10000)
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise DatabaseError('database_integrity_invalid')
        if cancelled():
            raise DatabaseError('database_update_cancelled')
        schemas = _schema(db); resolved = _resolve(db, schemas)
        unit = resolved['unit']; columns = {row[1].lower(): row[1] for row in schemas[unit]}
        # A hashed column is never guessed from its order or an old master value.
        if not {'unit_id', 'atk_type', 'search_area_width'} <= set(columns):
            return MappingProxyType({})
        sql = 'SELECT ' + ','.join(_quoted(columns[key]) for key in ('unit_id', 'atk_type', 'search_area_width')) + ' FROM ' + _quoted(unit) + ' LIMIT 10001'
        facts = {}
        for ordinal, (unit_id, attack, position) in enumerate(db.execute(sql)):
            if ordinal >= 10000:
                raise DatabaseError('database_schema_invalid')
            if type(unit_id) is not int or unit_id % 100 != 1 or not 100000 <= unit_id <= 99999999:
                continue
            fact = {}
            if type(attack) is int and attack in (1, 2):
                fact['damageType'] = 'PHYSICAL' if attack == 1 else 'MAGIC'
            if type(position) in (int, float) and 0 <= position <= 5000:
                fact['position'] = position
            if fact:
                facts[str(unit_id // 100)] = MappingProxyType(fact)
        if cancelled():
            raise DatabaseError('database_update_cancelled')
        return MappingProxyType(facts)
    except sqlite3.DatabaseError:
        raise DatabaseError('database_integrity_invalid') from None
    finally:
        db.close()


@dataclass(frozen=True)
class DatabaseSnapshot:
    version: str = ''
    sha256: str = ''
    facts: object = None

    def overlay_optimizer(self, base):
        """Keep opinions/estimates; overlay verified position and attack-type facts."""
        if not self.facts:
            return base
        from .roles import Optimizer, Profile
        profiles = {}
        for key, profile in base.profiles.items():
            data = copy.deepcopy(profile.data)
            fact = self.facts.get(key)
            if fact:
                data.update(fact)
                confidence = data.setdefault('dataConfidence', {})
                for field in fact:
                    confidence[field] = 'master-cn-' + self.version
                data['databaseVersion'] = self.version
            profiles[key] = Profile(data)
        return Optimizer(profiles, copy.deepcopy(base.config))


def _safe_url(url):
    value = urlsplit(url)
    if value.scheme != 'https' or value.username or value.password or value.port not in (None, 443) or value.hostname not in (
            'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
        raise DatabaseError('database_download_url_invalid')
    return url


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        _safe_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


@contextlib.contextmanager
def _update_lock(path):
    stream = open(path, 'a+b')
    locked = False
    try:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DatabaseError('database_update_busy') from None
        locked = True
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class DatabaseCache:
    def __init__(self, root, *, opener=None):
        self.directory = Path(root) / 'data/labyrinth/master-cn'
        self.metadata = self.directory / 'master_cn.metadata.json'
        self._opener = opener or build_opener(_Redirects()).open
        self._cancelled = threading.Event()
        self._thread = None
        self._guard = threading.Lock()

    def _load(self, metadata):
        version = _version(metadata['version']); digest = _hash(metadata['sha256'])
        path = self.directory / ('master_cn.' + digest + '.db')
        if path.stat().st_size > MAX_DATABASE_BYTES or self._digest(path) != digest:
            raise DatabaseError('database_checksum_invalid')
        facts = validate_database(path)
        return DatabaseSnapshot(version, digest, facts)

    def snapshot(self):
        try:
            with self.metadata.open('rb') as stream:
                metadata = _json(stream.read(65537))
            for value in (metadata, metadata.get('previous')):
                if type(value) is dict:
                    try:
                        return self._load(value)
                    except (OSError, ValueError, KeyError):
                        pass
        except (OSError, ValueError):
            pass
        return DatabaseSnapshot(facts=MappingProxyType({}))

    def _check(self, deadline):
        if self._cancelled.is_set():
            raise DatabaseError('database_update_cancelled')
        if time.monotonic() >= deadline:
            raise DatabaseError('database_update_timeout')

    def _read(self, url, destination, limit, deadline):
        self._check(deadline)
        request = Request(_safe_url(url), headers={'User-Agent': 'Maa-PCR database updater', 'Accept': 'application/octet-stream'})
        total = 0
        with self._opener(request, timeout=5) as response:
            _safe_url(response.geturl())
            while True:
                self._check(deadline)
                block = response.read(min(65536, limit + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise DatabaseError('database_size_invalid')
                destination.write(block)
        return total

    @staticmethod
    def _digest(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    def update(self):
        candidate = metadata_temp = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with _update_lock(self.directory / '.update.lock'):
                import io
                deadline = time.monotonic() + 120
                body = io.BytesIO()
                self._read(MANIFEST_URL, body, 65536, deadline)
                manifest = _json(body.getvalue())
                version = _version(manifest.get('db_version')); digest = _hash(manifest.get('checksum_sha256'))
                size = manifest.get('size_bytes')
                if size is not None and (type(size) is not int or not MIN_DATABASE_BYTES <= size <= MAX_DATABASE_BYTES):
                    raise DatabaseError('database_manifest_invalid')
                current = self.snapshot()
                if current.version and version < current.version:
                    raise DatabaseError('database_version_rollback')
                if (current.version, current.sha256) == (version, digest):
                    return dict(status='up_to_date', version=version)
                fd, name = tempfile.mkstemp(prefix='master-cn-', suffix='.candidate', dir=self.directory)
                candidate = Path(name)
                with os.fdopen(fd, 'wb') as stream:
                    actual_size = self._read(DATABASE_URL.format(version=version), stream, MAX_DATABASE_BYTES, deadline)
                    stream.flush(); os.fsync(stream.fileno())
                if size is not None and actual_size != size:
                    raise DatabaseError('database_size_invalid')
                if self._digest(candidate) != digest:
                    raise DatabaseError('database_checksum_invalid')
                validate_database(candidate, cancelled=self._cancelled.is_set)
                self._check(deadline)
                metadata = dict(version=version, sha256=digest)
                if current.version:
                    metadata['previous'] = dict(version=current.version, sha256=current.sha256)
                fd, name = tempfile.mkstemp(prefix='master-cn-', suffix='.metadata', dir=self.directory)
                metadata_temp = Path(name)
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                    json.dump(metadata, stream, separators=(',', ':'))
                    stream.flush(); os.fsync(stream.fileno())
                target = self.directory / ('master_cn.' + digest + '.db')
                if not target.exists():
                    os.replace(candidate, target)
                elif self._digest(target) != digest:
                    os.replace(candidate, target)
                self._check(deadline)
                # The pointer is the sole commit; the old file remains available.
                os.replace(metadata_temp, self.metadata)
                return dict(status='updated', version=version)
        except DatabaseError as error:
            return dict(status='failed', reason=str(error))
        except (OSError, ValueError, KeyError, TypeError, HTTPException):
            return dict(status='failed', reason='database_update_failed')
        finally:
            for path in (candidate, metadata_temp):
                if path is not None:
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)

    def start_update(self, emit=None):
        with self._guard:
            if self._thread is not None:
                return self._thread
            def run():
                result = self.update()
                if emit is not None and not self._cancelled.is_set():
                    with contextlib.suppress(Exception):
                        emit(dict(kind='database', **result))
            self._thread = threading.Thread(target=run, name='labyrinth-database-update', daemon=True)
            self._thread.start()
            return self._thread

    def close(self, timeout=.2):
        self._cancelled.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0, min(timeout, 5)))
            return not self._thread.is_alive()
        return True
