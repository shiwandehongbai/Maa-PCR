"""Database updates commit one pointer; running strategies retain their facts."""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType

import pytest

from pcr_maa.labyrinth import database as db


def master(path, *, position=200, attack=1, missing=None):
    with sqlite3.connect(path) as connection:
        schemas = {
            'unit_data': 'unit_id INTEGER PRIMARY KEY, unit_name TEXT, atk_type INTEGER, search_area_width INTEGER',
            'unit_skill_data': 'unit_id INTEGER PRIMARY KEY, union_burst_1 INTEGER, main_skill_1 INTEGER, ex_skill_1 INTEGER',
            'skill_data': 'skill_id INTEGER PRIMARY KEY, name TEXT, description TEXT, skill_action_1 INTEGER',
            'skill_action': 'action_id INTEGER PRIMARY KEY, action_type INTEGER, target_assignment INTEGER, action_detail_1 INTEGER',
        }
        for name, definition in schemas.items():
            if name != missing:
                connection.execute('CREATE TABLE ' + name + '(' + definition + ')')
        if missing != 'unit_data':
            connection.executemany('INSERT INTO unit_data VALUES (?, ?, ?, ?)',
                [(100101, '日和', attack, position), (100301, '怜', 1, 250), (100199, 'NPC', 1, 10)])
        if missing != 'unit_skill_data':
            connection.execute('INSERT INTO unit_skill_data VALUES (100101, 1001011, 1001012, 1001013)')
        if missing != 'skill_data':
            connection.execute("INSERT INTO skill_data VALUES (1001011, 'skill', 'description', 100101101)")
        if missing != 'skill_action':
            connection.execute('INSERT INTO skill_action VALUES (100101101, 1, 1, 0)')
        connection.execute('CREATE TABLE padding(content BLOB)')
        connection.execute('INSERT INTO padding VALUES(zeroblob(1000000))')
    return path.read_bytes()


class Response(io.BytesIO):
    def __init__(self, content, url):
        super().__init__(content)
        self.url = url
    def geturl(self):
        return self.url


class Source:
    def __init__(self, content, *, version='202610141448', manifest=None):
        self.content = content
        self.manifest = manifest or dict(db_version=version,
            checksum_sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content))
        self.calls = []
        self.cancel = None
    def __call__(self, request, *, timeout):
        url = request.full_url
        self.calls.append(url)
        assert timeout == 5
        if url == db.MANIFEST_URL:
            value = self.manifest if isinstance(self.manifest, bytes) else json.dumps(self.manifest).encode()
        else:
            assert url == db.DATABASE_URL.format(version=self.manifest['db_version'])
            value = self.content
            if self.cancel:
                self.cancel()
        return Response(value, url)


def installed(tmp_path, **kwargs):
    content = master(tmp_path/'source.db', **kwargs)
    source = Source(content)
    cache = db.DatabaseCache(tmp_path/'product', opener=source)
    assert cache.update() == dict(status='updated', version='202610141448')
    return cache, source


def test_validated_database_install_and_no_repeat_download(tmp_path):
    cache, source = installed(tmp_path)
    snapshot = cache.snapshot()
    assert snapshot.version == '202610141448'
    assert dict(snapshot.facts['1001']) == dict(position=200, damageType='PHYSICAL')
    assert '100199' not in snapshot.facts
    assert len(source.calls) == 2
    assert cache.update()['status'] == 'up_to_date'
    assert len(source.calls) == 3
    assert not list(cache.directory.glob('*.candidate'))


def test_facts_snapshot_and_nested_facts_are_immutable(tmp_path):
    cache, _ = installed(tmp_path)
    snapshot = cache.snapshot()
    with pytest.raises(TypeError):
        snapshot.facts['1001']['position'] = 10
    with pytest.raises(TypeError):
        snapshot.facts['9999'] = {}
    newer = Source(master(tmp_path/'newer.db', position=400), version='202610241448')
    cache._opener = newer
    assert cache.update()['status'] == 'updated'
    assert snapshot.facts['1001']['position'] == 200
    assert cache.snapshot().facts['1001']['position'] == 400
    assert len(list(cache.directory.glob('master_cn.*.db'))) == 2


@pytest.mark.parametrize('failure', ['hash', 'size', 'schema', 'header', 'network', 'truncated_network'])
def test_failed_update_keeps_usable_previous_cache_and_metadata(tmp_path, failure):
    cache, _ = installed(tmp_path)
    before = cache.metadata.read_bytes()
    old = cache.snapshot()
    content = master(tmp_path/'newer.db', position=400,
        missing='skill_action' if failure == 'schema' else None)
    if failure == 'header':
        content = b'X'*len(content)
    source = Source(content, version='202610241448')
    if failure == 'hash':
        source.manifest['checksum_sha256'] = 'f'*64
    if failure == 'size':
        source.manifest['size_bytes'] += 1
    def offline(*args, **kwargs):
        raise OSError('private network diagnostic must not escape')
    def truncated(*args, **kwargs):
        from http.client import IncompleteRead
        raise IncompleteRead(b'private response content')
    cache._opener = offline if failure == 'network' else source
    if failure=='truncated_network':cache._opener=truncated
    result = cache.update()
    assert result['status'] == 'failed'
    assert 'private' not in json.dumps(result)
    assert cache.metadata.read_bytes() == before
    assert cache.snapshot() == old
    assert not list(cache.directory.glob('*.candidate'))
    assert not list(cache.directory.glob('*.metadata'))


@pytest.mark.parametrize('manifest', [
    {'db_version':'../../outside', 'checksum_sha256':'f'*64},
    {'db_version':'202610241448', 'checksum_sha256':'wrong'},
    {'db_version':'202610241448', 'checksum_sha256':'f'*64, 'size_bytes':True},
    {'db_version':'202610241448', 'checksum_sha256':'f'*64, 'size_bytes':db.MAX_DATABASE_BYTES+1},
    b'{"db_version":"202610241448","db_version":"202610251448"}',
    b'[]',
])
def test_untrusted_manifest_never_reaches_download(tmp_path, manifest):
    source = Source(b'X', manifest=manifest)
    cache = db.DatabaseCache(tmp_path, opener=source)
    assert cache.update()['status'] == 'failed'
    assert source.calls == [db.MANIFEST_URL]
    assert cache.snapshot().facts == {}


def test_atomic_pointer_failure_and_missing_file_recovery(tmp_path, monkeypatch):
    cache, _ = installed(tmp_path)
    before = cache.metadata.read_bytes()
    cache._opener = Source(master(tmp_path/'newer.db', position=400), version='202610241448')
    replace = db.os.replace
    def failed_commit(source, destination):
        if Path(destination) == cache.metadata:
            raise OSError('disk full')
        return replace(source, destination)
    monkeypatch.setattr(db.os, 'replace', failed_commit)
    assert cache.update()['status'] == 'failed'
    assert cache.metadata.read_bytes() == before
    assert cache.snapshot().facts['1001']['position'] == 200
    monkeypatch.setattr(db.os, 'replace', replace)
    assert cache.update()['status'] == 'updated'
    metadata = json.loads(cache.metadata.read_text(encoding='utf-8'))
    current = cache.directory/('master_cn.'+metadata['sha256']+'.db')
    current.unlink()
    assert cache.snapshot().facts['1001']['position'] == 200
    assert cache.update()['status'] == 'updated'
    assert cache.snapshot().facts['1001']['position'] == 400


def test_same_version_corrupt_cache_is_redownloaded(tmp_path):
    cache, source = installed(tmp_path)
    metadata = json.loads(cache.metadata.read_text(encoding='utf-8'))
    (cache.directory/('master_cn.'+metadata['sha256']+'.db')).write_bytes(b'broken')
    assert cache.snapshot().version == ''
    assert cache.update()['status'] == 'updated'
    assert len(source.calls) == 4
    assert cache.snapshot().facts['1001']['position'] == 200


def test_old_manifest_cannot_roll_back_good_cache(tmp_path):
    cache, _ = installed(tmp_path)
    old = Source(master(tmp_path/'older.db', position=10), version='202509141448')
    cache._opener = old
    assert cache.update() == dict(status='failed', reason='database_version_rollback')
    assert old.calls == [db.MANIFEST_URL]
    assert cache.snapshot().facts['1001']['position'] == 200


def test_cancelled_download_never_commits_or_emits_after_close(tmp_path):
    cache, _ = installed(tmp_path)
    before = cache.metadata.read_bytes()
    source = Source(master(tmp_path/'newer.db', position=400), version='202610241448')
    cache._opener = source
    source.cancel = lambda: cache._cancelled.set()
    result = cache.update()
    assert result == dict(status='failed', reason='database_update_cancelled')
    assert cache.metadata.read_bytes() == before
    assert cache.close() is True
    emitted = []
    thread = cache.start_update(emitted.append)
    thread.join(3)
    assert not thread.is_alive()
    assert emitted == []


def test_concurrent_updater_has_one_file_owner(tmp_path):
    cache, _ = installed(tmp_path)
    before = cache.metadata.read_bytes()
    with db._update_lock(cache.directory/'.update.lock'):
        other = db.DatabaseCache(tmp_path/'product', opener=lambda *args, **kwargs: pytest.fail('must not fetch'))
        assert other.update() == dict(status='failed', reason='database_update_busy')
    assert cache.metadata.read_bytes() == before
    assert cache.update()['status'] == 'up_to_date'


@pytest.mark.parametrize('url', ['http://github.com/file', 'https://evil.example/db',
    'https://github.com@evil.example/db', 'https://user:pass@github.com/db',
    'https://github.com:444/db', 'file:///tmp/db'])
def test_download_redirects_cannot_escape_https_release_hosts(url):
    with pytest.raises(db.DatabaseError):
        db._safe_url(url)


def test_live_optimizer_uses_facts_without_changing_ratings_or_base(tmp_path):
    from pcr_maa.labyrinth.roles import load_optimizer, Context
    base = load_optimizer()
    cache, _ = installed(tmp_path, position=50, attack=2)
    before = base.profiles['1001'].data.copy()
    updated = cache.snapshot().overlay_optimizer(base)
    role = updated.profiles['1001']
    assert role.position == 50
    assert role.data['damageType'] == 'MAGIC'
    assert role.rating == base.profiles['1001'].rating
    assert role.data['functions'] == base.profiles['1001'].data['functions']
    assert role.data['databaseVersion'] == '202610141448'
    assert base.profiles['1001'].data == before
    # The changed position becomes the actual front inspected by the scorer.
    members = ('1001','1003','1002','1004','1005')
    expected = role.vanguard(Context(enemy='MAGIC'))
    assert updated.score(members, Context(enemy='MAGIC')).frontline == expected
    updated.profiles['1001'].data['personalScore'] = 99
    assert updated.profiles['1001'].rating == 99
    assert cache.snapshot().facts['1001']['position'] == 50


def test_nonfinite_and_unknown_unit_facts_do_not_invent_positions(tmp_path):
    path = tmp_path/'facts.db'
    master(path)
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE unit_data SET atk_type=99, search_area_width=-1 WHERE unit_id=100101')
    facts = db.validate_database(path)
    assert '1001' not in facts
    assert facts['1003']['position'] == 250


def test_background_update_is_one_owned_worker_and_emits_safe_status(tmp_path):
    content = master(tmp_path/'source.db')
    source = Source(content)
    cache = db.DatabaseCache(tmp_path/'product', opener=source)
    events = []
    thread = cache.start_update(events.append)
    assert cache.start_update(events.append) is thread
    thread.join(5)
    assert not thread.is_alive()
    assert events == [dict(kind='database', status='updated', version='202610141448')]
    assert cache.close() is True
    assert cache.snapshot().version == '202610141448'


def obfuscated_master(path, *, ambiguous=False, broken_reference=False):
    """Same ID relationships as upstream; table/column hashes carry no meaning."""
    definitions = {
        'a01': [('u0','INTEGER PRIMARY KEY')] + [('u'+str(i),'INTEGER') for i in range(1,13)] + [('n1','TEXT'),('n2','TEXT')],
        'a02': [('v0','INTEGER PRIMARY KEY')] + [('v'+str(i),'INTEGER') for i in range(1,26)],
        'a03': [('s0','INTEGER PRIMARY KEY')] + [('s'+str(i),'INTEGER') for i in range(1,13)] + [('d1','TEXT'),('d2','TEXT')],
        'a04': [('b0','INTEGER PRIMARY KEY')] + [('b'+str(i),'INTEGER') for i in range(1,12)],
    }
    with sqlite3.connect(path) as connection:
        for name, columns in definitions.items():
            connection.execute('CREATE TABLE '+name+'('+','.join(column+' '+kind for column,kind in columns)+')')
        for index, unit in enumerate(db.ANCHORS):
            skill = 1_000_001+index
            action = 100_000_001+index
            connection.execute('INSERT INTO a01 VALUES('+','.join('?'*15)+')', (unit,)+tuple([0]*12)+('a','b'))
            connection.execute('INSERT INTO a02 VALUES('+','.join('?'*26)+')', (unit, skill)+tuple([0]*24))
            connection.execute('INSERT INTO a03 VALUES('+','.join('?'*15)+')', (skill, action)+tuple([0]*11)+('a','b'))
            if not broken_reference or index < 6:
                connection.execute('INSERT INTO a04 VALUES('+','.join('?'*12)+')', (action,)+tuple([0]*11))
        if ambiguous:
            columns = definitions['a01']
            connection.execute('CREATE TABLE a05('+','.join(column+' '+kind for column,kind in columns)+')')
            connection.execute('INSERT INTO a05 SELECT * FROM a01')
        connection.execute('CREATE TABLE padding(content BLOB)')
        connection.execute('INSERT INTO padding VALUES(zeroblob(1000000))')


def test_obfuscated_schema_follows_unit_skill_action_reference_chain(tmp_path):
    path = tmp_path/'obfuscated.db'
    obfuscated_master(path)
    assert db.validate_database(path) == {}
    with sqlite3.connect(path) as connection:
        resolved = db._resolve(connection, db._schema(connection))
    assert resolved == dict(unit='a01', unit_skill='a02', skill='a03', action='a04')


@pytest.mark.parametrize('kwargs', [dict(ambiguous=True), dict(broken_reference=True)])
def test_obfuscated_ambiguous_or_unrelated_tables_are_rejected(tmp_path, kwargs):
    path = tmp_path/'obfuscated.db'
    obfuscated_master(path, **kwargs)
    with pytest.raises(db.DatabaseError, match='database_schema_invalid'):
        db.validate_database(path)


def test_cancel_interrupts_sqlite_validation_before_any_install(tmp_path):
    path = tmp_path/'source.db'
    master(path)
    with pytest.raises(db.DatabaseError, match='database_update_cancelled'):
        db.validate_database(path, cancelled=lambda: True)
