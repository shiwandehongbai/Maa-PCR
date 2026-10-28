"""Owner-only consumption approvals are scoped to real actions, never request JSON."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import queue
import threading
from types import SimpleNamespace as NS

import pytest

from pcr_maa import alpha_owner
from pcr_maa import clan_battle_batch as batch
from pcr_maa.clan_battle_daily_native import NativeClanDailyAdapter
from pcr_maa.preset_execution import Journal
from test_clan_battle_batch import Port, daily_request, run, ledgers


def approval(preview, plan_hash, request_id='a'*32):
    return dict(kind='clan_consumption', request_id=request_id, root_task_id=3,
        controller_uuid='same-uuid', invocation_id='b'*32, daily_plan_hash=plan_hash,
        timeout_seconds=120, preview=deepcopy(preview), ok=True)


class ConsentPort(Port):
    def __init__(self, request):
        super().__init__(request)
        self.confirmations = []
        self.consent_fault = None
        self.fingerprint = dict(source_sha256='1'*64, calibration_sha256='2'*64,
            owner_core_sha256='3'*64, authorization_generation='4'*32)

    def consumption_binding(self):
        return deepcopy(self.fingerprint)

    def request_consumption_consent(self, plan_hash, preview):
        alpha_owner.validate_clan_preview(preview)
        self.confirmations.append(deepcopy(preview))
        if self.consent_fault == 'deny' or self.consent_fault == 'deny_carry' and preview['kind'] == 'carryover':
            return None
        value = approval(preview, plan_hash, f'{len(self.confirmations):032x}')
        if self.consent_fault == 'mutate_preview':
            value['preview']['actions'][0]['instance_id'] = 'unauthorized'
        if self.consent_fault == 'change_core':
            self.fingerprint['owner_core_sha256'] = 'f'*64
        return value


def test_one_confirmation_allows_only_the_three_specific_regular_instances(tmp_path, daily_request):
    port = ConsentPort(daily_request)
    result, _ = run(tmp_path, daily_request, port)
    assert result['regular_settled'] == 3 and result['outcome'] == 'complete'
    assert len(port.confirmations) == 1
    preview = port.confirmations[0]
    assert preview['kind'] == 'regular' and preview['max_count'] == preview['remaining_count'] == 3
    assert preview['cp'] == 900 and len(preview['actions']) == 3
    reserved = [intent['ready']['evidence']['consumption_authorization'] for intent in port.executions]
    assert len({row['request_id'] for row in reserved}) == 1
    assert [row['reserved_intent_id'] for row in reserved] == [row['intent_id'] for row in port.executions]
    assert len(set(row['reserved_intent_id'] for row in reserved)) == 3


def test_regular_denial_creates_no_intent_and_no_frozen_batch(tmp_path, daily_request):
    port = ConsentPort(daily_request)
    port.consent_fault = 'deny'
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_consumption_not_authorized' and not result['consumption_sent']
    assert not port.executions and not ledgers(tmp_path) and not list(tmp_path.rglob('batch.json'))


def test_denied_carryover_is_held_while_approved_main_attacks_continue(tmp_path, daily_request):
    port = ConsentPort(daily_request)
    port.issue, port.consent_fault = {1}, 'deny_carry'
    result, _ = run(tmp_path, daily_request, port)
    assert result['regular_settled'] == 3 and result['carryover_settled'] == 0
    assert result['outcome'] == 'remind_carryover'
    assert result['held_carryovers'][0]['reason'] == 'carryover_not_authorized'
    assert [row['kind'] for row in port.confirmations] == ['regular', 'carryover']
    assert [row['action']['kind'] for row in port.executions] == ['regular']*3
    # The authorization hold remains replayable in the unchanged v1 Journal.
    again, _ = run(tmp_path, daily_request, port)
    assert again['regular_settled'] == 3 and len(port.executions) == 3


def test_carryover_confirmation_is_separate_and_binds_source_team_time_and_new_instance(tmp_path, daily_request):
    port = ConsentPort(daily_request)
    port.issue = {1}
    result, _ = run(tmp_path, daily_request, port)
    assert result['regular_settled'] == 3 and result['carryover_settled'] == 1
    assert [row['kind'] for row in port.confirmations] == ['regular', 'carryover']
    carry = port.confirmations[1]
    assert carry['max_count'] == 1 and len(carry['actions']) == 1
    action = carry['actions'][0]
    assert action['source_intent_id'] == port.executions[0]['intent_id']
    assert action['source_instance_id'] == port.executions[0]['action']['instance_id']
    assert action['instance_id'] != action['source_instance_id'] and action['stock_index'] == 0
    assert action['start_seconds'] == 34 and action['using_unit'] == daily_request['slot_plans']['1']['unit_ids']


@pytest.mark.parametrize('fault,reason', [('mutate_preview','authorization_invalid'),('change_core','authorization_binding_changed')])
def test_changed_approval_or_core_cannot_reserve_any_input(tmp_path, daily_request, fault, reason):
    port = ConsentPort(daily_request)
    port.consent_fault = fault
    result, _ = run(tmp_path, daily_request, port)
    assert reason in result['error'] and not port.executions and not ledgers(tmp_path)


def test_final_binding_change_after_prepare_cannot_create_intent(tmp_path, daily_request):
    port = ConsentPort(daily_request)
    def changed(ready, state):
        state.fingerprint['authorization_generation'] = 'e'*32
    port.prepare_change = changed
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_consumption_authorization_binding_changed'
    assert len(port.confirmations) == 1 and not port.executions and not ledgers(tmp_path)


def test_unknown_result_keeps_reserved_authorization_and_never_requests_or_sends_again(tmp_path, daily_request):
    port = ConsentPort(daily_request)
    port.fail = 'unknown_after_start'
    first, _ = run(tmp_path, daily_request, port)
    assert first['error'] == 'unknown_after_start' and len(port.executions) == 1
    events = Journal(ledgers(tmp_path)[0].parent).events()
    assert len(events) == 1 and events[0]['ready']['evidence']['consumption_authorization']['reserved_intent_id'] == events[0]['intent_id']
    second, _ = run(tmp_path, daily_request, port)
    assert second['error'] == 'daily_previous_result_unknown'
    assert len(port.executions) == len(port.confirmations) == 1


def test_approved_instances_do_not_expire_between_three_battles_in_the_same_owner(tmp_path, daily_request):
    port = ConsentPort(daily_request)
    original = port.execute
    def slow(intent, job):
        port.clock += timedelta(seconds=70)
        return original(intent, job)
    port.execute = slow
    result, _ = run(tmp_path, daily_request, port)
    assert result['regular_settled'] == 3 and len(port.confirmations) == 1


def transport(fault=None, *, selected_slot=None, on_disconnect=None, on_read=None):
    incoming, sent = queue.Queue(), []
    context = NS(get_task_job=lambda: NS(job_id=3), tasker=NS(controller=NS(uuid='same-uuid')))
    selection = dict(version=1, selected_modules=['clan_battle_daily'], rewards_final=False)
    answer = dict(ok=True, fixed_binding=True, invocation_id='b'*32, root_task_id=3,
        controller_uuid='same-uuid', serial='127.0.0.1:16384', combination=selection,
        execute=True, game_day='', count_context='', remaining_seconds=600,
        input_display=2, screenshot_display='123', schedule_authorizations=[], schedule_attest_current_plan=False,
        owner_core_sha256='3'*64)
    class Input:
        def readline(self, limit):
            if on_read: on_read()
            return incoming.get(timeout=3)
    class Output:
        def write(self, line):
            sent.append(line)
            if line.startswith(alpha_owner.PREFIX):
                reply = answer
            elif line.startswith(alpha_owner.SOURCE_PREFIX):
                reply = dict(json.loads(line[len(alpha_owner.SOURCE_PREFIX):]), ok=True)
            else:
                assert line.startswith(alpha_owner.CLAN_PREFIX)
                if fault == 'defer': return
                reply = dict(json.loads(line[len(alpha_owner.CLAN_PREFIX):]), ok=fault != 'deny')
                if selected_slot is not None: reply['selected_slot'] = selected_slot
                if fault == 'nonce': reply['request_id'] = '0'*32
                if fault == 'echo': reply['preview']['account_id'] = '999'
                if fault == 'extra': reply['authorization'] = True
                if fault == 'missing_ok': reply.pop('ok')
                if fault == 'bool_slot': reply['preview']['actions'][0]['slot'] = True
            encoded=json.dumps(reply)
            if fault == 'duplicate' and line.startswith(alpha_owner.CLAN_PREFIX):
                encoded=encoded.replace('\"ok\": true', '\"ok\": true, \"ok\": true')
            incoming.put(encoded+'\n')
        def flush(self): pass
    provider = alpha_owner.OwnerProvider(Input(), Output(), timeout=.1, on_disconnect=on_disconnect or (lambda: None))
    grant = provider(context, selection)
    return provider, grant, context, incoming, sent


def preview_value():
    action = dict(kind='regular', slot=1, stock_index=None, boss=3, round=7, phase='C',
        instance_id='current-instance', start_seconds=90, source_intent_id=None, source_instance_id=None,
        using_unit=[1,2,3,4,5], support_unit=None, support_owner_viewer_id=None)
    return dict(server='cn-bilibili', account_id='123', period='2026-10', game_day='2026-10-26',
        batch_id='0'*64, source_sha256='1'*64, calibration_sha256='2'*64, owner_core_sha256='3'*64,
        kind='regular', max_count=1, remaining_count=3, cp=900, observed_at=datetime.now(timezone.utc).isoformat(), actions=[action])


@pytest.mark.parametrize('fault', [None,'deny','nonce','echo','extra','missing_ok','bool_slot','duplicate'])
def test_clan_reply_is_exact_live_echo_on_a_separate_queue(fault):
    provider, grant, context, incoming, sent = transport(fault)
    try:
        assert callable(grant.clan_consumption_consent)
        if fault in (None, 'deny'):
            result = grant.clan_consumption_consent('f'*64, preview_value())
            assert (result is None) is (fault == 'deny')
            assert grant.observation_source(context) is True
        else:
            with pytest.raises(RuntimeError):
                grant.clan_consumption_consent('f'*64, preview_value())
            before = list(sent)
            with pytest.raises(RuntimeError):
                grant.clan_consumption_consent('f'*64, preview_value())
            assert sent == before
        assert sum(line.startswith(alpha_owner.CLAN_PREFIX) for line in sent) == 1
        assert sum(line.startswith(alpha_owner.PREFIX) for line in sent) == 1
    finally:
        incoming.put('')


def test_timeout_cannot_send_a_second_confirmation(monkeypatch):
    provider, grant, _, incoming, sent = transport()
    try:
        def expired(*args, **kwargs):
            raise queue.Empty
        monkeypatch.setattr(provider._clan_answers, 'get', expired)
        with pytest.raises(RuntimeError, match='timeout_no_retry'):
            grant.clan_consumption_consent('f'*64, preview_value())
        before = list(sent)
        with pytest.raises(RuntimeError):
            grant.clan_consumption_consent('f'*64, preview_value())
        assert sent == before
    finally:
        incoming.put('')


@pytest.fixture
def timed_out_clan_owner(monkeypatch):
    progressed, disconnected = threading.Event(), threading.Event()
    reads = []
    def on_read():
        reads.append(True)
        if len(reads) >= 4: progressed.set()
    def on_disconnect():
        disconnected.set()
        progressed.set()
    provider, grant, context, incoming, sent = transport('defer', on_disconnect=on_disconnect, on_read=on_read)
    def expired(*args, **kwargs):
        raise queue.Empty
    monkeypatch.setattr(provider._clan_answers, 'get', expired)
    with pytest.raises(RuntimeError, match='timeout_no_retry'):
        grant.clan_consumption_consent('f'*64, preview_value())
    request = json.loads(next(line[len(alpha_owner.CLAN_PREFIX):] for line in sent if line.startswith(alpha_owner.CLAN_PREFIX)))
    try:
        yield provider, grant, incoming, sent, request, progressed, disconnected
    finally:
        incoming.put('')
        assert disconnected.wait(2), 'actual EOF must still disconnect'


def test_late_timeout_denial_keeps_channel_for_atomic_stop_report(timed_out_clan_owner, tmp_path):
    from pcr_maa.supported_daily import _atomic_json
    provider, grant, incoming, sent, request, progressed, disconnected = timed_out_clan_owner
    incoming.put(json.dumps(dict(request, ok=False))+'\n')
    assert progressed.wait(2)
    assert not disconnected.is_set(), 'local timeout denial must not kill report publication'
    assert provider._closed.is_set()
    before = list(sent)
    with pytest.raises(RuntimeError):
        grant.clan_consumption_consent('f'*64, preview_value())
    assert sent == before
    report = dict(outcome='stopped', error='clan_consumption_confirmation_timeout_no_retry', consumption_sent=False)
    _atomic_json(tmp_path/'report.json', report)
    assert json.loads((tmp_path/'report.json').read_text(encoding='utf-8')) == report
    assert not list(tmp_path.glob('.report.json-*.tmp'))
    incoming.put(json.dumps(dict(request, ok=False))+'\n')
    assert disconnected.wait(2), 'a duplicate late denial must still disconnect'


@pytest.mark.parametrize('fault', ['approve', 'request', 'root', 'controller', 'invocation', 'plan', 'preview', 'extra', 'duplicate', 'bool', 'missing_ok', 'bool_slot'])
def test_timeout_does_not_accept_other_late_replies(timed_out_clan_owner, fault):
    provider, _, incoming, _, request, progressed, disconnected = timed_out_clan_owner
    reply = deepcopy(dict(request, ok=False))
    if fault == 'approve': reply['ok'] = True
    if fault == 'request': reply['request_id'] = '0'*32
    if fault == 'root': reply['root_task_id'] += 1
    if fault == 'controller': reply['controller_uuid'] = 'other'
    if fault == 'invocation': reply['invocation_id'] = '0'*32
    if fault == 'plan': reply['daily_plan_hash'] = '0'*64
    if fault == 'preview': reply['preview']['actions'][0]['instance_id'] = 'other'
    if fault == 'extra': reply['authorization'] = True
    if fault == 'bool': reply['ok'] = 0
    if fault == 'missing_ok': reply.pop('ok')
    if fault == 'bool_slot': reply['preview']['actions'][0]['slot'] = True
    encoded = json.dumps(reply)
    if fault == 'duplicate': encoded = encoded.replace('"ok": false', '"ok": false, "ok": false')
    incoming.put(encoded+'\n')
    assert progressed.wait(2)
    assert disconnected.wait(2), fault
    assert provider._closed.is_set()


def test_reply_delivered_at_timeout_is_not_retained_for_a_duplicate(monkeypatch):
    progressed, disconnected = threading.Event(), threading.Event()
    reads = []
    def on_read():
        reads.append(True)
        if len(reads) >= 4: progressed.set()
    provider, grant, _, incoming, sent = transport('defer', on_disconnect=disconnected.set, on_read=on_read)
    reply = []
    def expired(*args, **kwargs):
        request = json.loads(sent[-1][len(alpha_owner.CLAN_PREFIX):])
        reply.append(json.dumps(dict(request, ok=False))+'\n')
        incoming.put(reply[0])
        assert progressed.wait(2)
        raise queue.Empty
    monkeypatch.setattr(provider._clan_answers, 'get', expired)
    try:
        with pytest.raises(RuntimeError, match='timeout_no_retry'):
            grant.clan_consumption_consent('f'*64, preview_value())
        assert not disconnected.is_set() and provider._pending_clan is None
        incoming.put(reply[0])
        assert disconnected.wait(2)
    finally:
        incoming.put('')
        assert disconnected.wait(2)


def test_unsolicited_consumption_reply_still_disconnects():
    disconnected = threading.Event()
    provider, _, _, incoming, sent = transport('defer', on_disconnect=disconnected.set)
    try:
        incoming.put(json.dumps(approval(preview_value(), 'f'*64))+'\n')
        assert disconnected.wait(2)
        assert provider._closed.is_set()
        assert not any(line.startswith(alpha_owner.CLAN_PREFIX) for line in sent)
    finally:
        incoming.put('')


@pytest.mark.parametrize('field,value', [('remaining_count',True),('max_count',True),('cp',901),('source_sha256','local-config')])
def test_untrusted_or_malformed_preview_is_rejected_before_any_request(field, value):
    provider, grant, _, incoming, sent = transport()
    try:
        preview = preview_value()
        preview[field] = value
        with pytest.raises(ValueError):
            grant.clan_consumption_consent('f'*64, preview)
        assert sum(line.startswith(alpha_owner.CLAN_PREFIX) for line in sent) == 0
    finally:
        incoming.put('')


def test_shared_utf8_path_and_raw_content_fixture_has_the_cross_language_digest(tmp_path):
    path = Path(__file__).parent / 'data/clan-owner-binding-fixture.json'
    fixture = json.loads(path.read_text(encoding='utf-8'))
    for item in fixture['files']:
        target = tmp_path / item['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item['text'].encode('utf-8'))
    assert alpha_owner.file_set_sha256(tmp_path, [row['path'] for row in fixture['files']]) == fixture['sha256']
    changed = tmp_path / fixture['files'][0]['path']
    changed.write_bytes(changed.read_bytes()+b'changed')
    assert alpha_owner.file_set_sha256(tmp_path, [row['path'] for row in fixture['files']]) != fixture['sha256']


@pytest.mark.parametrize('path', ['../secret.py','a/../b.py','a\\b.py','C:/secret.py'])
def test_digest_does_not_follow_outside_paths(tmp_path, path):
    with pytest.raises(ValueError, match='digest_path_invalid'):
        alpha_owner.file_set_sha256(tmp_path, [path])


def test_json_has_no_way_to_install_consumption_authority(daily_request):
    value = deepcopy(daily_request)
    value['consumption_consent'] = dict(ok=True, count=3)
    value['confirmed_for'] = batch.request_hash(value)
    with pytest.raises(ValueError, match='daily_request_invalid'):
        batch.validate_daily_request(value)


def test_native_reconnect_generation_invalidates_cached_consents():
    port = object.__new__(NativeClanDailyAdapter)
    port.invalidate_consumption_authorizations()
    first = port._consumption_generation
    port.invalidate_consumption_authorizations()
    assert first != port._consumption_generation and port._consumption_owner is None


@pytest.mark.parametrize('mutation', ['template','sample','profile','index'])
def test_calibration_fingerprint_rechecks_actual_declared_dependencies(tmp_path, monkeypatch, mutation):
    from pcr_maa import clan_battle_calibration as calibration
    def put(name, data):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()
    put('src/pcr_maa/a.py', b'value=1\n')
    template = 'assets/resource/image/a.png'
    sample = 'assets/resource/clan-battle-calibrations/sample.png'
    template_hash = put(template,b'template bytes')
    sample_hash = put(sample,b'sample bytes')
    profile_name='assets/resource/clan-battle-calibrations/profile.json'
    profile=dict(samples=[dict(page='battle',path=sample,sha256=sample_hash)],
        guard=dict(recognition='TemplateMatch',template='a.png',sha256=template_hash),
        timing_measurements=dict(path='missing-and-not-enabled.json',sha256='a'*64))
    profile_hash=put(profile_name,json.dumps(profile).encode())
    index=dict(version=2,entries=[dict(server='cn-bilibili',content_version='version',
        profile_path=profile_name,profile_sha256=profile_hash,capabilities=['formal'])])
    index_hash=put(calibration.INDEX_PATH,json.dumps(index).encode())
    monkeypatch.setattr(calibration,'INDEX_SHA256',index_hash)
    request=dict(slot_plans={'1':dict(server='cn-bilibili',content_version='version')})
    original=batch.consumption_file_binding(request,root=tmp_path)
    path={'template':template,'sample':sample,'profile':profile_name,'index':calibration.INDEX_PATH}[mutation]
    (tmp_path/path).write_bytes((tmp_path/path).read_bytes()+b'changed')
    with pytest.raises(ValueError,match='file_changed'):
        batch.consumption_file_binding(request,root=tmp_path)
    assert original['source_sha256'] and original['calibration_sha256']


def test_source_fingerprint_changes_for_new_and_modified_python_files(tmp_path, monkeypatch):
    from pcr_maa import clan_battle_calibration as calibration
    source=tmp_path/'src/pcr_maa/a.py'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'value=1\n')
    profile=tmp_path/'assets/resource/clan-battle-calibrations/profile.json'
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b'{}')
    index=tmp_path/calibration.INDEX_PATH
    index.write_text(json.dumps(dict(entries=[dict(server='cn-bilibili',content_version='v',
        profile_path=profile.relative_to(tmp_path).as_posix(),profile_sha256=hashlib.sha256(profile.read_bytes()).hexdigest())])),encoding='utf-8')
    monkeypatch.setattr(calibration,'INDEX_SHA256',hashlib.sha256(index.read_bytes()).hexdigest())
    request=dict(slot_plans={'1':dict(server='cn-bilibili',content_version='v')})
    first=batch.consumption_file_binding(request,root=tmp_path)
    source.write_bytes(b'value=2\n')
    second=batch.consumption_file_binding(request,root=tmp_path)
    assert first['source_sha256'] != second['source_sha256'] and first['calibration_sha256'] == second['calibration_sha256']
    (source.parent/'new.py').write_bytes(b'new=1\n')
    assert second['source_sha256'] != batch.consumption_file_binding(request,root=tmp_path)['source_sha256']


def test_same_controller_reverified_account_gets_a_new_authorization_generation(monkeypatch):
    port=object.__new__(NativeClanDailyAdapter)
    port.consumption_consent=lambda *args:None
    port.owner_core_sha256='a'*64
    port._consumption_files=None
    port._account_binding=('account',object())
    port.plan={'account_id':'123'}
    port.request={}
    owner=object()
    port.tasker=NS(observation_source=lambda:owner)
    port.checkpoint=lambda:None
    port.account_binding_valid=lambda plan:True
    port.invalidate_consumption_authorizations()
    monkeypatch.setattr(batch,'consumption_file_binding',lambda request:dict(source_sha256='1'*64,calibration_sha256='2'*64))
    first=port.consumption_binding()
    assert port.consumption_binding()==first
    port._account_binding=('account',object())
    assert port.consumption_binding()['authorization_generation'] != first['authorization_generation']


def test_native_cannot_rebase_an_approved_session_to_changed_source(monkeypatch):
    port=object.__new__(NativeClanDailyAdapter)
    port.consumption_consent=lambda *args:None
    port.owner_core_sha256='a'*64
    port._consumption_files=dict(source_sha256='1'*64,calibration_sha256='2'*64)
    port._account_binding=('account',object())
    port.plan={'account_id':'123'}
    port.request={}
    port.tasker=NS(observation_source=lambda:object())
    port.checkpoint=lambda:None
    port.account_binding_valid=lambda plan:True
    port.invalidate_consumption_authorizations()
    monkeypatch.setattr(batch,'consumption_file_binding',lambda request:dict(source_sha256='f'*64,calibration_sha256='2'*64))
    with pytest.raises(RuntimeError,match='source_or_calibration_changed'):
        port.consumption_binding()



def test_safe_6002_recovery_restarts_preview_and_never_reuses_the_old_permit(tmp_path, daily_request):
    port=ConsentPort(daily_request)
    def reconnect(ready,state):
        state.fingerprint['authorization_generation']='e'*32
        state.last_disconnect_recovery=dict(can_continue=True,pending_preserved=False)
        state.prepare_change=None
    port.prepare_change=reconnect
    result,_=run(tmp_path,daily_request,port)
    assert result['regular_settled']==3 and len(port.confirmations)==2
    assert len(port.executions)==3 and result['disconnect_recovery']['can_continue']
    assert all(row['ready']['evidence']['consumption_authorization']['request_id']==f'{2:032x}' for row in port.executions)


@pytest.mark.parametrize('pending', [False,True])
def test_daily_observe_only_retries_after_proven_nonpending_recovery(monkeypatch,pending):
    from pcr_maa import clan_battle_recovery as recovery
    port=object.__new__(NativeClanDailyAdapter)
    port.plan=dict(account_id='123')
    port.pending={'intent_id':'existing'} if pending else None
    error=recovery.ClanDisconnected({},'sent' if pending else 'not_sent')
    calls=[]
    def once(binding,*,result_intent=None):
        calls.append('observe')
        if len(calls)==1:raise error
        return {'fresh':True}
    port._observe_once=once
    def recover(actual,plan,exc,*,pending):
        assert actual is port and exc is error and pending is (port.pending is not None)
        return dict(can_continue=not pending)
    monkeypatch.setattr(recovery,'recover_disconnect',recover)
    if pending:
        with pytest.raises(recovery.ClanDisconnected) as stopped:
            port.observe({})
        assert stopped.value is error and calls==['observe']
    else:
        assert port.observe({})=={'fresh':True} and calls==['observe','observe']


def test_daily_prepare_refreshes_the_full_observation_before_retry(monkeypatch):
    from pcr_maa import clan_battle_recovery as recovery
    port=object.__new__(NativeClanDailyAdapter)
    port.pending=None
    calls=[]
    error=recovery.ClanDisconnected({},'not_sent')
    def prepare(action,job,binding):
        calls.append('prepare')
        if len(calls)==1:raise error
        return {'prepared':True}
    port._prepare_once=prepare
    port.observe=lambda binding:calls.append('fresh_observe')
    monkeypatch.setattr(recovery,'recover_disconnect',lambda *args,**kwargs:dict(can_continue=True))
    assert port.prepare({},dict(account_id='123'),{})=={'prepared':True}
    assert calls==['prepare','fresh_observe','prepare']


@pytest.mark.parametrize('already_recovered',[False,True])
def test_daily_execute_recovers_for_observation_only_and_keeps_pending_stop(monkeypatch,already_recovered):
    from pcr_maa import clan_battle_recovery as recovery
    port=object.__new__(NativeClanDailyAdapter)
    port._clan_disconnect_recovery_used=already_recovered
    port.pending={'intent_id':'existing'}
    error=recovery.ClanDisconnected({},'unknown')
    calls=[]
    def execute(*args):
        calls.append('execute_once')
        raise error
    port._execute_once=execute
    port.finish_opening_capture=lambda:calls.append('capture_finished')
    def recover(*args,**kwargs):
        assert kwargs['pending'] is True
        calls.append('recover_only')
        return dict(can_continue=False)
    monkeypatch.setattr(recovery,'recover_disconnect',recover)
    with pytest.raises(recovery.ClanDisconnected) as stopped:
        port.execute({},dict(account_id='123'))
    assert stopped.value is error and port.pending=={'intent_id':'existing'}
    assert calls==(['execute_once','capture_finished'] if already_recovered else ['execute_once','recover_only','capture_finished'])


def test_only_verified_settlement_releases_the_per_battle_start_latch():
    port=object.__new__(NativeClanDailyAdapter)
    port.pending=None
    port.started,port.battle_start=True,'sent'
    port.checkpoint=lambda:None
    port.after_settlement({}, {})
    assert port.started is False and port.battle_start=='not_sent'
    port.pending={'intent_id':'unknown'}
    port.started,port.battle_start=True,'unknown'
    with pytest.raises(RuntimeError,match='previous_input_unresolved'):
        port.after_settlement({}, {})
    assert port.started is True and port.battle_start=='unknown'


def test_daily_successful_6002_observation_preserves_exact_receipt_for_recovery(monkeypatch):
    from pcr_maa.guide_trial_native import NativeGuideTrial
    from pcr_maa.clan_battle_recovery import ClanDisconnected
    port=object.__new__(NativeClanDailyAdapter)
    port.started,port.battle_start=False,'not_sent'
    cleared=[]
    port.invalidate_account_binding=lambda:cleared.append(True)
    receipt=dict(native_ok=True,tasker_inited=True,task_id=3,nodes=[dict(name='LabyDisconnect6002Observed',completed=True)])
    monkeypatch.setattr(NativeGuideTrial,'run',lambda *args,**kwargs:receipt)
    with pytest.raises(ClanDisconnected) as stopped:
        port.run('GTChoose')
    assert stopped.value.receipt is receipt and port.last_receipt is receipt
    assert stopped.value.battle_start=='not_sent' and cleared==[True]
