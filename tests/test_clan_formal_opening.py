"""Formal opening contracts use synthetic evidence and never grant device qualification."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest

from pcr_maa import clan_battle, clan_battle_daily_native as daily
from pcr_maa.clan_battle_batch import execute_batch
from pcr_maa.clan_battle_inheritance import BattleInheritance
from pcr_maa.game_day import current_game_day
from pcr_maa.preset_execution import Journal
from test_clan_battle import Controls
from test_clan_battle_batch import daily_request, Port
from test_clan_battle_daily_native import executable, profile
from test_clan_battle_native import calibration, calibration_v2, plan, Registry
from test_clan_inheritance import SettingsPort


@pytest.mark.parametrize('field,value,code', [
    ('battle_speed', 4, 'daily_speed_invalid'), ('battle_speed', True, 'daily_speed_invalid'),
    ('speed', {'1': {}}, 'daily_speed_invalid'), ('clock', [0, 0, 1281, 1], 'roi_invalid'),
    ('inheritance', {}, 'inheritance_calibration_invalid'), ('samples', [], 'opening_samples_missing')])
def test_formal_opening_requires_complete_independent_calibration(executable, plan, field, value, code):
    executable['daily']['execution']['opening'][field] = value
    with pytest.raises(ValueError, match=code):
        daily.require_daily_execution(executable, plan)


def test_simulation_pause_section_cannot_replace_formal_opening(executable, plan):
    spec = executable['daily']['execution'].pop('opening')
    executable['auto_battle'] = spec
    with pytest.raises(ValueError, match='execution_calibration_missing'):
        daily.require_daily_execution(executable, plan)


@pytest.mark.parametrize('patch,code', [({'support_mode':'specified'}, 'support_mode_uncalibrated'),
    ({'switches':[{'remaining_seconds':30}]}, 'opening_axis_unsupported')])
def test_formal_scope_is_rejected_before_native_start(executable, plan, patch, code):
    with pytest.raises(RuntimeError, match=code):
        daily.require_daily_execution(executable, dict(plan, **patch))


def test_formal_samples_cannot_repeat_a_page(executable, plan):
    rows = executable['daily']['execution']['opening']['samples']
    rows.append(deepcopy(rows[0]))
    with pytest.raises(ValueError, match='opening_samples_missing'):
        daily.require_daily_execution(executable, plan)


@pytest.fixture
def formal_reader(executable, plan, monkeypatch, tmp_path):
    plan = dict(plan, confirmed_for='reviewed')
    action = dict(kind='carryover', boss=plan['boss'], phase='B', round=5, instance_id='next-instance',
        start_seconds=34, stock_index=2, slot=1)
    now = datetime.now(timezone.utc)
    port = daily.NativeClanDailyAdapter.__new__(daily.NativeClanDailyAdapter)
    daily.NativeClanBattle.__init__(port, NS(resource=Registry()), tmp_path)
    port.plan, port.action, port.start_seconds = plan, action, 34
    executable['daily']['execution']['opening']['start_seconds'] = [90,34]
    port.calibration = executable
    port.pending = dict(intent_id='reserved', action=deepcopy(action), slot_confirmed_for='reviewed',
        observed_at=now.isoformat(), game_day=current_game_day(now))
    port.started, port.battle_start = True, 'sent'
    port.now = lambda: now
    page = daily.ClanBattleDailyExecutionPage(executable, plan, port)
    page.current, page.expected = 'formation', 'battle'
    page.bound = dict(formal=True, period=plan['period'], **{key:action[key] for key in ('boss','phase','round','instance_id')})
    port.page = page
    reader = daily._DailyTeamReader.__new__(daily._DailyTeamReader)
    reader.page, reader.team, reader.plan, reader.calibration = page, plan['unit_ids'], plan, executable
    monkeypatch.setattr(reader, 'read_paused', lambda image:[dict(unit_id=uid) for uid in reversed(plan['unit_ids'])])
    hits = {'CBFormalOpeningBoss', 'CBFormalOpening_auto_off', 'CBFormalOpeningSpeed1'}
    hits.update(f'CBFormalOpening_paused_guards_{i}' for i in range(3))
    hits.update(f'CBFormalOpeningEmptyTp{i}' for i in range(5))
    hits.update(f'CBFormalOpeningSet{i}' for i in range(1,5))
    monkeypatch.setattr(page, 'hit', lambda context,image,node:node in hits)
    clock = ['0:34']
    monkeypatch.setattr(page, 'word', lambda context,image,node:clock[0])
    image = np.zeros((720,1280,3),np.uint8)
    for i in range(5):
        image[286:346,358+116*i:363+116*i] = (0,0,255) if i == 0 else (255,255,0)
    context = NS(tasker=NS(stopping=False))
    return port, reader, context, image, hits, clock


def read(reader, context, image, **params):
    return reader.analyze(context, NS(image=image, custom_recognition_param=json.dumps(dict(mode='opening_read', **params))))


def test_short_compensation_uses_intent_clock_and_role_identity(formal_reader):
    port, reader, context, image, _, _ = formal_reader
    result = read(reader, context, image)
    assert result is not None
    state = result.detail
    assert state['remaining_seconds'] == 34 and state['intent_id'] == 'reserved'
    assert state['unit_ids'] == list(reversed(port.plan['unit_ids']))
    assert state['sets'] == [False,True,True,True,True] and state['formal']
    assert port.page.current == 'battle'


@pytest.mark.parametrize('change', ['clock','stock','target','day','unknown_start','stop','tp','speed4','speed_bool'])
def test_formal_unknown_binding_never_returns_a_control_target(formal_reader, change):
    port, reader, context, image, hits, clock = formal_reader
    params = {}
    if change == 'clock': clock[0]='1:30'
    elif change == 'stock': port.action=dict(port.action,stock_index=3)
    elif change == 'target': port.page.bound['instance_id']='old-instance'
    elif change == 'day': port.now=lambda:datetime.now(timezone.utc)+timedelta(days=1)
    elif change == 'unknown_start': port.battle_start='not_sent'
    elif change == 'stop': context.tasker.stopping=True
    elif change == 'tp': image[388,365]=[220,180,50]
    elif change == 'speed_bool': params['battle_speed']=True
    else:
        hits.remove('CBFormalOpeningSpeed1'); hits.add('CBFormalOpeningSpeed4')
    assert read(reader,context,image,**params) is None


def test_compensation_pause_reuses_controls_without_simulation_90_89_rule(plan):
    port=Controls(plan)
    port.calibration={'version':2,'daily':{'execution':{'opening':{'battle_speed':2}}}}
    port.opening_paused=True
    port.formal_opening=True
    port.start_seconds=34
    port.state.update(paused=True, remaining_seconds=34, same_frame=True,
        zero_damage_verified=True,zero_tp_verified=True,battle_speed=1)
    plan['set_by_unit'][str(plan['unit_ids'][0])]=False
    result=clan_battle.execute_switches(port,plan)
    assert result['opening_evidence']['remaining_seconds']==34
    assert port.actions==[('set',plan['unit_ids'][0],True,False)]
    port.state['remaining_seconds']=33
    with pytest.raises(RuntimeError,match='opening_not_verified'):
        clan_battle.execute_switches(port,plan)


def test_formal_restore_failure_is_after_persistent_consumption_settlement(daily_request,tmp_path):
    port=Port(daily_request)
    calls=[]
    def restore(intent,result):
        calls.append(intent['intent_id'])
        raise RuntimeError('restore_readback_unknown')
    port.after_settlement=restore
    journal=Journal(tmp_path/'ledger')
    report=execute_batch(port,journal,daily_request,now=port.now(),explicit_start=True)
    assert report['regular_settled']==1 and report['attempts'][0]['damage']==12345678
    assert report['error']=='restore_readback_unknown' and len(calls)==1
    rows=list((tmp_path/'ledger').rglob('transactions.jsonl'))
    assert rows
    events=[json.loads(line) for line in rows[0].read_text(encoding='utf-8').splitlines()]
    assert len(events)==2 and events[-1]['state']=='settled'


def test_explicit_inheritance_recovery_restores_without_disabling_again(tmp_path):
    port=SettingsPort()
    first=BattleInheritance(port,Journal(tmp_path))
    first.prepare()
    second=BattleInheritance(port,Journal(tmp_path))
    second.recover()
    assert port.actions==[(True,False),(False,True)] and port.state is True
    assert len(second.journal.events())==2 and second.intent is None

@pytest.mark.parametrize('second',[90,89])
@pytest.mark.parametrize('speed',[1,2,4])
def test_mixed_profile_simulation_keeps_its_own_opening_clock_and_speed(plan,second,speed):
    port=Controls(plan)
    port.calibration={'version':2,'auto_battle':{'speeds':[1,2,4]},
        'daily':{'execution':{'opening':{'battle_speed':2,'start_seconds':[90,34]}}}}
    port.opening_paused=True
    port.state.update(paused=True,remaining_seconds=second,same_frame=True,
        zero_damage_verified=True,zero_tp_verified=True,battle_speed=speed)
    assert clan_battle.execute_switches(port,plan)['opening_evidence']['remaining_seconds']==second


@pytest.mark.parametrize('seconds', [[],[34],[90,90],[90,True],[90,0],[90,91],90])
def test_formal_start_second_coverage_is_explicit(executable,plan,seconds):
    executable['daily']['execution']['opening']['start_seconds']=seconds
    with pytest.raises(ValueError,match='start_time_calibration_invalid'):
        daily.require_daily_execution(executable,plan)


@pytest.mark.parametrize('reason',['carryover_capability_not_calibrated','carryover_opening_start_time_uncalibrated'])
def test_unqualified_carryover_is_held_while_three_other_main_attacks_finish(daily_request,tmp_path,reason):
    port=Port(daily_request)
    port.issue={1}
    port.blocked_carryovers=lambda request,grants:{stock:reason for stock in grants}
    result=execute_batch(port,Journal(tmp_path),daily_request,now=port.now(),explicit_start=True)
    assert result.get('error') is None and result['outcome']=='remind_carryover'
    assert result['regular_settled']==3 and result['carryover_settled']==0
    assert result['held_carryovers'][0]['reason']==reason


@pytest.mark.parametrize('failure',['missing','short','corrupt'])
def test_native_carryover_filter_only_holds_known_unsupported_calibration(executable,plan,monkeypatch,failure):
    from pcr_maa import clan_battle_calibration
    port=daily.NativeClanDailyAdapter.__new__(daily.NativeClanDailyAdapter)
    grant={'source_slot':1,'time':34}
    def load(*args,**kwargs):
        if failure=='missing': raise RuntimeError('clan_battle_calibration_capability_missing:carryover')
        if failure=='corrupt': raise ValueError('clan_battle_calibration_file_changed')
        return executable
    monkeypatch.setattr(clan_battle_calibration,'load_calibration',load)
    if failure=='corrupt':
        with pytest.raises(ValueError,match='file_changed'):
            port.blocked_carryovers({'slot_plans':{'1':plan}},{2:grant})
    else:
        reason=port.blocked_carryovers({'slot_plans':{'1':plan}},{2:grant})[2]
        assert reason==('carryover_capability_not_calibrated' if failure=='missing' else 'carryover_opening_start_time_uncalibrated')
