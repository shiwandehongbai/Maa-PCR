"""One request/reply over a GUI-owned child's redirected standard streams.

Endpoint possession is the trust boundary, not JSON or a saved configuration.
No discovery, controller construction, permission file, environment grant or retry.
The parent must bind its actual AppendTask job before answering the request.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import math
import queue
import threading
import time

PREFIX = 'pcr-owner-request:'
SOURCE_PREFIX = 'pcr-owner-source:'
CLAN_PREFIX = 'pcr-clan-consent:'
MAX_LINE = 16384


def config_snapshot(value):
    """Validated v1-v4 data; serialization grants no live authority."""
    from .daily import validate_config
    from .device import validate_connection, ADB_PATH, SERIAL
    config = validate_config(value)
    connection = validate_connection(config.get('connection', dict(adb_path=ADB_PATH, serial=SERIAL)))
    return json.dumps(dict(config=config, connection=connection), sort_keys=True,
                      ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def bind_config_snapshot(entry, snapshot):
    """Keep the original post-authorize load/validation and reject any drift."""
    expected = config_snapshot(json.loads(snapshot)['config'])
    if expected != snapshot:
        raise ValueError('invalid_config_snapshot')
    original_load = entry.load_config

    def checked_load(path):
        value = original_load(path)
        if config_snapshot(value) != expected:
            raise ValueError('config_changed_since_confirmation')
        return value

    entry.load_config = checked_load


def file_set_sha256(root, paths):
    root = Path(root).resolve()
    records = []
    for relative in sorted(set(paths), key=lambda name: name.encode('utf-16-be')):
        if (type(relative) is not str or not relative or '\\' in relative or ':' in relative
                or relative.startswith('/') or any(part in ('', '.', '..') for part in relative.split('/'))):
            raise ValueError('clan_owner_digest_path_invalid')
        path = root / relative
        if path.resolve() != path or not path.is_file():
            raise ValueError('clan_owner_digest_path_invalid')
        for part in [path, *path.parents]:
            if part == root:
                break
            if part.is_symlink() or getattr(part.stat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('clan_owner_digest_path_invalid')
        records.append(relative + '\0' + hashlib.sha256(path.read_bytes()).hexdigest() + '\n')
    if not records:
        raise ValueError('clan_owner_digest_empty')
    return hashlib.sha256(''.join(records).encode('utf-8')).hexdigest()


def validate_clan_preview(value):
    keys = {'server', 'account_id', 'period', 'game_day', 'batch_id', 'source_sha256',
        'calibration_sha256', 'owner_core_sha256', 'kind', 'max_count', 'observed_at', 'remaining_count', 'cp', 'actions'}
    action_keys = {'kind', 'slot', 'stock_index', 'boss', 'round', 'phase', 'instance_id', 'start_seconds',
        'source_intent_id', 'source_instance_id', 'using_unit', 'support_unit', 'support_owner_viewer_id'}
    if (type(value) is not dict or set(value) != keys or value['server'] != 'cn-bilibili'
            or type(value['account_id']) is not str or re.fullmatch(r'[0-9]{1,19}', value['account_id']) is None
            or type(value['period']) is not str or re.fullmatch(r'20[0-9]{2}-[0-9]{2}', value['period']) is None
            or type(value['game_day']) is not str or re.fullmatch(r'20[0-9]{2}-[0-9]{2}-[0-9]{2}', value['game_day']) is None
            or any(type(value[key]) is not str or re.fullmatch('[0-9a-f]{64}', value[key]) is None
                for key in ('batch_id', 'source_sha256', 'calibration_sha256', 'owner_core_sha256'))
            or type(value['remaining_count']) is not int or not 0 <= value['remaining_count'] <= 3
            or type(value['cp']) is not int or not 0 <= value['cp'] <= 900
            or value['kind'] not in ('regular', 'carryover', 'external_regular_baseline') or type(value['max_count']) is not int
            or type(value['actions']) is not list
            or (value['kind'] == 'external_regular_baseline' and (value['max_count'] != 1
                or len(value['actions']) != 3 or value['cp'] != 900 or value['remaining_count'] != 2))
            or (value['kind'] != 'external_regular_baseline' and not 1 <= value['max_count'] == len(value['actions']) <= 3)
            or value['kind'] == 'carryover' and value['max_count'] != 1
            or value['kind'] == 'regular' and value['max_count'] > value['remaining_count']):
        raise ValueError('clan_consumption_preview_invalid')
    from .clan_battle_batch import ACTION_KEYS, _action, _timestamp
    observed = _timestamp(value['observed_at'])
    seen = set()
    for action in value['actions']:
        if type(action) is not dict or set(action) != action_keys:
            raise ValueError('clan_consumption_preview_invalid')
        _action({key: action[key] for key in ACTION_KEYS})
        if (action['kind'] != ('regular' if value['kind'] == 'external_regular_baseline' else value['kind']) or action['slot'] in seen
                or type(action['using_unit']) is not list or len(action['using_unit']) != 5
                or any(type(uid) is not int or uid <= 0 for uid in action['using_unit'])
                or len(set(action['using_unit'])) != 5 or action['support_unit'] is not None
                or action['support_owner_viewer_id'] is not None
                or action['kind'] == 'regular' and (action['source_intent_id'] is not None or action['source_instance_id'] is not None)
                or action['kind'] == 'carryover' and (type(action['source_intent_id']) is not str
                    or re.fullmatch('[0-9a-f]{64}', action['source_intent_id']) is None
                    or type(action['source_instance_id']) is not str or not action['source_instance_id'])):
            raise ValueError('clan_consumption_preview_invalid')
        seen.add(action['slot'])
    if value['kind'] == 'external_regular_baseline' and seen != {1, 2, 3}:
        raise ValueError('clan_consumption_preview_invalid')
    return observed


class OwnerProvider:
    def __init__(self, incoming, outgoing, *, timeout=5.0, on_disconnect=None):
        self.incoming, self.outgoing = incoming, outgoing
        self.timeout = timeout
        self.on_disconnect = on_disconnect or self.revoke
        self._used = False
        self._lock = threading.Lock()
        self._answers = queue.Queue(maxsize=1)
        self._closed = threading.Event()
        self._source_answers = queue.Queue(maxsize=1)
        self._source_key = None
        self._clan_answers = queue.Queue(maxsize=1)
        self._pending_clan = None
        self._clan_allowed = False
        self._deadline = 0
        # Start reading only after requesting: a buffered reply cannot authorize
        # anything without this provider's actual callback root and controller.

    def _read(self):
        try:
            line = self.incoming.readline(MAX_LINE + 1)
            if not line or len(line) > MAX_LINE or not line.endswith('\n'):
                raise ValueError('owner_channel_closed_or_invalid')
            self._answers.put(json.loads(line), timeout=self.timeout)
        except BaseException as exc:
            self._answers.put(exc)

    def __call__(self, context, selection):
        with self._lock:
            if self._used or self._closed.is_set():
                raise ValueError('owner_request_already_consumed')
            self._used = True
        root = context.get_task_job().job_id
        uuid = context.tasker.controller.uuid
        request = dict(root_task_id=root, controller_uuid=uuid, combination=selection)
        from .player_log import write_protocol
        write_protocol(self.outgoing, PREFIX + json.dumps(request, allow_nan=False) + '\n')
        threading.Thread(target=self._read, daemon=True).start()
        try:
            answer = self._answers.get(timeout=self.timeout)
        except queue.Empty:
            raise ValueError('owner_reply_timeout_no_retry') from None
        if isinstance(answer, BaseException):
            raise ValueError('owner_channel_closed_or_invalid') from answer
        if type(answer) is not dict or answer.get('ok') is not True:
            raise ValueError('owner_denied')
        if (answer.get('root_task_id') != root or answer.get('controller_uuid') != uuid
                or answer.get('combination') != selection):
            raise ValueError('owner_reply_binding_mismatch')
        remaining = answer.pop('remaining_seconds', None)
        max_seconds = 3600 if selection.get('selected_modules') == ['clan_battle_daily'] else 1800
        if type(remaining) not in (int, float) or not math.isfinite(remaining) or not 0 < remaining <= max_seconds:
            raise ValueError('owner_reply_invalid_remaining_time')
        answer.pop('ok')
        answer['deadline'] = time.monotonic() + remaining
        auth = answer.get('schedule_authorizations')
        if type(auth) is not list or any(type(item) is not str for item in auth):
            raise ValueError('owner_invalid_schedule_authorizations')
        answer['schedule_authorizations'] = tuple(auth)
        fixed_binding = answer.pop('fixed_binding', False)
        if type(fixed_binding) is not bool:
            raise ValueError('owner_invalid_fixed_binding')
        if fixed_binding:
            self._source_key = (context, root, uuid, answer['invocation_id'])
            answer['observation_source'] = self.check_source
            if selection.get('selected_modules') == ['clan_battle_daily']:
                if type(answer.get('owner_core_sha256')) is not str or re.fullmatch('[0-9a-f]{64}', answer['owner_core_sha256']) is None:
                    raise ValueError('clan_owner_core_binding_required')
                self._clan_allowed = True
                self._deadline = answer['deadline']
                answer['clan_consumption_consent'] = lambda daily_plan_hash, preview: self.request_clan_consumption(context, daily_plan_hash, preview)
        from .agent_entry import ExecutionContext
        grant = ExecutionContext(**answer)
        if self.on_disconnect:
            threading.Thread(target=self._watch_disconnect, daemon=True).start()
        return grant

    def check_source(self, context):
        """One bounded, non-retried check of actual objects retained by Owner."""
        from uuid import uuid4
        with self._lock:
            key = self._source_key
            if self._closed.is_set() or key is None or context is not key[0]:
                raise RuntimeError('Owner source closed or mismatched')
            if context.get_task_job().job_id != key[1] or context.tasker.controller.uuid != key[2]:
                self.revoke()
                raise RuntimeError('Owner source root/controller changed')
            request = dict(root_task_id=key[1], controller_uuid=key[2],
                           invocation_id=key[3], probe_id=uuid4().hex)
            try:
                from .player_log import write_protocol
                write_protocol(self.outgoing, SOURCE_PREFIX + json.dumps(request) + '\n')
                try:
                    answer = self._source_answers.get(timeout=self.timeout)
                except queue.Empty:
                    raise RuntimeError('Owner source timeout; no retry') from None
                if (self._closed.is_set() or type(answer) is not dict
                        or answer != dict(request, ok=True)):
                    raise RuntimeError('Owner source changed or revoked')
            except BaseException:
                self.revoke()
                raise
            return True

    def request_clan_consumption(self, context, daily_plan_hash, preview):
        from uuid import uuid4
        if type(daily_plan_hash) is not str or re.fullmatch('[0-9a-f]{64}', daily_plan_hash) is None:
            raise ValueError('clan_consumption_plan_binding_required')
        observed = validate_clan_preview(preview)
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if not 0 <= age <= 60:
            raise ValueError('clan_consumption_preview_stale')
        self.check_source(context)
        with self._lock:
            key = self._source_key
            if (not self._clan_allowed or self._closed.is_set() or key is None or context is not key[0]
                    or context.get_task_job().job_id != key[1] or context.tasker.controller.uuid != key[2]
                    or self._pending_clan is not None):
                raise RuntimeError('clan_consumption_owner_changed')
            request = dict(kind='clan_consumption', request_id=uuid4().hex, root_task_id=key[1],
                controller_uuid=key[2], invocation_id=key[3], daily_plan_hash=daily_plan_hash,
                timeout_seconds=120, preview=deepcopy(preview))
            wait = min(120 - age, self._deadline - time.monotonic())
            if wait <= 0:
                raise RuntimeError('clan_consumption_preview_stale')
            self._pending_clan = request
            timed_out = False
            try:
                from .player_log import write_protocol
                write_protocol(self.outgoing, CLAN_PREFIX + json.dumps(request, allow_nan=False) + '\n')
                try:
                    answer = self._clan_answers.get(timeout=wait)
                except queue.Empty:
                    timed_out = True
                    raise RuntimeError('clan_consumption_confirmation_timeout_no_retry') from None
                expected = dict(request, ok=answer.get('ok')) if type(answer) is dict else {}
                if preview['kind'] == 'external_regular_baseline' and expected.get('ok') is True:
                    selected = answer.get('selected_slot')
                    if type(selected) is not int or selected not in (1, 2, 3):
                        raise RuntimeError('clan_external_regular_selection_invalid')
                    expected['selected_slot'] = selected
                if (self._closed.is_set() or type(answer) is not dict or type(answer.get('ok')) is not bool
                        or json.dumps(answer, sort_keys=True, allow_nan=False)
                            != json.dumps(expected, sort_keys=True, allow_nan=False)):
                    raise RuntimeError('clan_consumption_reply_binding_mismatch')
                if (datetime.now(timezone.utc) - observed).total_seconds() > 120:
                    raise RuntimeError('clan_consumption_confirmation_expired')
                approved = deepcopy(answer) if answer['ok'] else None
            except BaseException:
                self.revoke()
                raise
            finally:
                # A matching late denial must not kill the stopped callback before its report is written.
                if not timed_out:
                    self._pending_clan = None
        self.check_source(context)
        return approved

    def revoke(self):
        self._closed.set()

    def _watch_disconnect(self):
        try:
            # No second execution grant is accepted. A fixed-binding channel
            # accepts only live source replies; invalid data/EOF revokes it.
            # Parent Stop also stops the native tasker.
            if self._source_key is None:
                self.incoming.read(1)
            else:
                # A rejected source probe revokes this execution grant, but
                # Maa still needs the active callback's response. Keep watching
                # the pipe until actual EOF/error; local revoke is not disconnect.
                while True:
                    line = self.incoming.readline(MAX_LINE + 1)
                    if not line or len(line) > MAX_LINE or not line.endswith('\n'):
                        break
                    answer = json.loads(line)
                    if type(answer) is dict and answer.get('kind') == 'clan_consumption':
                        def unique(pairs):
                            value = {}
                            for key, item in pairs:
                                if key in value:
                                    raise ValueError('duplicate_clan_consumption_reply_key')
                                value[key] = item
                            return value
                        answer = json.loads(line, object_pairs_hook=unique)
                        pending = self._pending_clan
                        if pending is None or answer.get('request_id') != pending['request_id']:
                            raise ValueError('unsolicited_clan_consumption_reply')
                        self._pending_clan = None
                        if self._closed.is_set():
                            if (json.dumps(answer, sort_keys=True, allow_nan=False)
                                    != json.dumps(dict(pending, ok=False), sort_keys=True, allow_nan=False)):
                                raise ValueError('clan_consumption_reply_binding_mismatch')
                            continue
                        self._clan_answers.put_nowait(answer)
                    else:
                        self._source_answers.put_nowait(answer)
        except BaseException:
            pass  # Every malformed/unsolicited response revokes this channel.
        finally:
            self._closed.set()
            try:
                self._source_answers.put_nowait(None)
            except queue.Full:
                pass
            try:
                self._clan_answers.put_nowait(None)
            except queue.Full:
                pass
            self.on_disconnect()
