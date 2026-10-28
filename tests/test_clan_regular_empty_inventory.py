"""Strict regular-only marker candidate; synthetic Owner/receipts never authorize a real attack."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import numpy as np
import pytest
import test_clan_battle_daily_native as daily_tests
import test_clan_regular_scope as scope_tests
from pcr_maa import clan_battle_daily_native as daily
from test_clan_battle_native import calibration, calibration_v2, plan, receipt
from test_clan_battle_daily_native import profile, executable, native_protocol, daily_request
from test_clan_battle_calibration import bundle, v2_bundle, daily_bundle, daily_execution_bundle
from pcr_maa.game_day import current_game_day

regular_only = scope_tests.regular_only

@pytest.fixture
def strict_regular(regular_only):
    regular_only['daily']['execution']['formation_mode']['carryover'] = None
    return regular_only


def test_null_marker_uses_strict_regular_scope(strict_regular, plan):
    nodes = daily.require_daily_execution(strict_regular, plan, capability='formal')
    assert 'CBDailyFormationRegular' in nodes and 'CBDailyFormationCarryover' not in nodes
    assert not {'CBPage_carry','CBPage_support','CBAction_select_carry'} & nodes.keys()


@pytest.mark.parametrize('mutation', ['carry_capability','with_support','with_carry',
    'support_not_explicit_none','missing_regular','missing_carry_key','false','zero','empty_string','empty_dict','empty_list'])
def test_null_does_not_remove_other_scope_requirements(strict_regular, executable, plan, mutation):
    value = strict_regular
    mode = value['daily']['execution']['formation_mode']
    capability = 'formal'
    if mutation == 'carry_capability': capability = 'carryover'
    elif mutation in ('with_support','with_carry'):
        value = deepcopy(executable)
        value['support_modes'] = ['none']
        value['daily']['execution']['formation_mode']['carryover'] = None
        if mutation == 'with_support':
            part = 'carry'; names = ('close_carry','open_carry_for_target','select_carry')
            value['daily']['execution']['carry_slots'] = []
            value['daily']['execution']['formation_mode'].update(stock_index=None,time=None)
        else:
            part = 'support'; names = ('open_support','support_back','support_top','support_next')
            value['daily']['execution']['formation_owner'] = None
        value['daily']['execution']['pages'].pop(part)
        for name in names: value['daily']['execution']['actions'].pop(name)
        value['daily']['execution']['samples'] = [r for r in value['daily']['execution']['samples'] if r['page'] != part]
    elif mutation == 'support_not_explicit_none': value.pop('support_modes')
    elif mutation == 'missing_regular': mode['regular'] = None
    elif mutation == 'missing_carry_key': mode.pop('carryover')
    else:
        mode['carryover'] = {'false':False,'zero':0,'empty_string':'','empty_dict':{},'empty_list':[]}[mutation]
    with pytest.raises((ValueError, RuntimeError)):
        daily.require_daily_execution(value, plan, capability=capability)


@pytest.fixture
def live_observation(strict_regular, plan, monkeypatch):
    port = object.__new__(daily.NativeClanDailyAdapter)
    clock = [datetime(2026,10,28,6,tzinfo=timezone.utc)]
    source, valid = ['owner-connection-one'], [True]
    port.calibration, port.plan = strict_regular, plan
    port.pending, port.action = None, None
    port.started, port.battle_start = False, 'not_sent'
    port.support_unit_id, port.account_receipt = None, {'synthetic_account':True}
    port._account_binding = object()
    port.regular_history, port.history_binding = None, None
    port.boss_history, port.stocks, port.calls = {}, [], []
    port.now = lambda: clock[0]
    port._account_source = lambda: source[0]
    port.account_binding_valid = lambda _: valid[0]
    port.enter_lobby = lambda: None
    port.recover_inheritance = lambda: None
    port.run = lambda node, **kwargs: port.calls.append(node) or receipt({'node':node})
    lobby = dict(page='lobby',period=plan['period'], phase='B', carry_count=0, cp=900, remaining_count=3,
        bosses=[dict(boss=n,round=5 if n==plan['boss'] else None,phase='B',available=n==plan['boss']) for n in range(1,6)])
    empty = dict(page='carry_empty',carry_stock_indices=[],evidence={'frame_pixel_sha256':'a'*64})
    def read(page):
        value = deepcopy(lobby if page == 'lobby' else empty)
        if page == 'target':
            value = dict(cp=lobby['cp'], remaining_count=lobby['remaining_count'],
                boss=port.action['boss'], phase=port.action['phase'], alive=True)
        native_receipt = receipt(value)
        native_receipt['nodes'][-1].update(name='CBPage_'+page,completed=True)
        return value,native_receipt
    port.read_page = read
    def wait(names, **kwargs):
        # Native wait retains the qualified receipt from its successful choose.
        if names == ['CBPage_lobby']:
            port.last_receipt = read('lobby')[1]
        return names[0]
    port.wait = wait
    binding = dict(account_id=plan['account_id'],server=plan['server'],period=plan['period'],
        game_day=current_game_day(clock[0]),batch_id='synthetic-batch')
    port.bind_history(binding,{})
    return port,binding,clock,source,valid,lobby,empty


def prepared(live_observation, strict_regular, plan):
    port,binding,clock,source,valid,lobby,empty = live_observation
    before = port.observe(binding)
    target = before['bosses'][plan['boss']-1]
    action = dict(kind='regular',slot=1,stock_index=None,start_seconds=90,
        **{key:target[key] for key in ('boss','phase','round','instance_id')})
    port.action = action
    bound = dict(period=plan['period'],formal=True,
        **{key:action[key] for key in ('boss','phase','round','instance_id')})
    _,_,hits,context = daily_tests.execution_context(strict_regular,plan,'formation')
    hits.update(('CBDailyFormationRegular','CBDailyActionLabel_start'))
    page = daily.ClanBattleDailyExecutionPage(strict_regular,plan,port)
    page.current, page.bound = 'formation',bound
    team = dict(bound,battle_kind='regular',member_count=5,unit_ids=plan['unit_ids'][:],stars=plan['stars'][:],
        support_unit_id=None,support_owner_viewer_id=None)
    port.characters = NS(formation=lambda *_: deepcopy(team))
    ready = dict(unit_ids=plan['unit_ids'][:],stars=plan['stars'][:],support_unit=None,support_owner_viewer_id=None)
    pending = dict(action=deepcopy(action),observed_at=before['observed_at'],game_day=binding['game_day'],
        before=deepcopy(before),ready=ready)
    return port,page,context,hits,before,pending,bound


def test_observed_empty_inventory_allows_regular_formation_and_final_start(live_observation, strict_regular, plan):
    port,page,context,hits,before,pending,bound = prepared(live_observation,strict_regular,plan)
    image = np.zeros((720,1280,3),np.uint8)
    assert page.read(context,image,'formation')['battle_kind'] == 'regular'
    port.pending = pending
    assert page.action(context,image,'start') == strict_regular['daily']['execution']['actions']['start']['target']
    assert page.expected == 'battle'
    assert port.calls == ['CBAction_open_carry','CBAction_close_empty_carry']


def test_constructor_empty_list_is_not_inventory_proof(live_observation, strict_regular, plan):
    port,binding,_,_,_,_,_ = live_observation
    assert port.stocks == []
    port.action = dict(kind='regular',boss=3,phase='B',round=5,instance_id='unobserved',stock_index=None,start_seconds=90)
    with pytest.raises(RuntimeError,match='empty_inventory_unverified'):
        port.require_regular_empty_inventory(dict(formal=True,**port.action))
    assert port.calls == []


@pytest.mark.parametrize('fault', ['nonempty','unknown','bool_zero','bool_one','missing_empty_index','missing_hash','wrong_hash','missing_page'])
def test_actual_observe_requires_exact_zero_and_native_empty_page(live_observation,fault):
    port,binding,clock,source,valid,lobby,empty = live_observation
    if fault == 'nonempty': lobby['carry_count'] = 1
    elif fault == 'unknown': lobby['carry_count'] = None
    elif fault == 'bool_zero': lobby['carry_count'] = False
    elif fault == 'bool_one': lobby['carry_count'] = True
    elif fault == 'missing_empty_index': empty.pop('carry_stock_indices')
    elif fault == 'missing_hash': empty.pop('evidence')
    elif fault == 'wrong_hash': empty['evidence']['frame_pixel_sha256'] = 'unknown'
    else: empty.pop('page')
    with pytest.raises((RuntimeError,ValueError)):
        port.observe(binding)
    assert getattr(port,'_regular_empty_observation',None) is None


@pytest.mark.parametrize('fault', ['native_failed','wrong_node','incomplete','missing_receipt_detail','source_changed_during_read'])
def test_inventory_receipt_and_connection_must_be_real_and_same(live_observation,fault):
    port,binding,clock,source,valid,lobby,empty = live_observation
    original = port.read_page
    def read(page):
        value,rec = original(page)
        if page == 'carry_empty':
            if fault == 'native_failed': rec['native_ok'] = False
            elif fault == 'wrong_node': rec['nodes'][-1]['name'] = 'CBPage_formation'
            elif fault == 'incomplete': rec['nodes'][-1]['completed'] = False
            elif fault == 'missing_receipt_detail': rec['nodes'][-1]['recognition'] = None
            else: source[0] = 'reconnected'
        return value,rec
    port.read_page = read
    with pytest.raises((RuntimeError,ValueError)):
        port.observe(binding)
    assert getattr(port,'_regular_empty_observation',None) is None


@pytest.mark.parametrize('fault', ['stale','future','source','account_rebound','account_invalid','wrong_target','wrong_instance',
    'nonformal','carry_action','nonempty_action','changed_history','new_action_target','no_proof'])
def test_formation_rejects_changed_or_unknown_empty_inventory_binding(live_observation,strict_regular,plan,fault):
    port,page,context,hits,before,pending,bound = prepared(live_observation,strict_regular,plan)
    _,binding,clock,source,valid,lobby,empty = live_observation
    if fault == 'stale':
        clock[0] += timedelta(seconds=61)
        port.pending = pending
    elif fault == 'future': clock[0] -= timedelta(seconds=1)
    elif fault == 'source': source[0] = 'new-owner'
    elif fault == 'account_rebound': port._account_binding = object()
    elif fault == 'account_invalid': valid[0] = False
    elif fault == 'wrong_target': bound['boss'] = 4
    elif fault == 'wrong_instance': bound['instance_id'] = 'new-instance'
    elif fault == 'nonformal': bound['formal'] = False
    elif fault == 'carry_action': port.action['kind'] = 'carryover'
    elif fault == 'nonempty_action': port.action['stock_index'] = 1
    elif fault == 'changed_history': port.history_binding = dict(binding,account_id='99999')
    elif fault == 'new_action_target':
        port.action['round'] += 1
        bound['round'] = port.action['round']
    else: port._regular_empty_observation = None
    with pytest.raises(RuntimeError,match='empty_inventory'):
        page.read(context,np.zeros((720,1280,3),np.uint8),'formation')


@pytest.mark.parametrize('fault', ['no_regular_positive','simulation_reject'])
def test_regular_positive_and_formal_page_still_required(live_observation,strict_regular,plan,fault):
    _,page,context,hits,_,_,_ = prepared(live_observation,strict_regular,plan)
    if fault == 'no_regular_positive': hits.remove('CBDailyFormationRegular')
    else: hits.add('CBDaily_formation_rejects_0')
    with pytest.raises(RuntimeError,match='consumption_mode_unknown|page_unknown'):
        page.read(context,np.zeros((720,1280,3),np.uint8),'formation')


@pytest.mark.parametrize('fault', ['missing_before','changed_before_inventory','changed_inventory_receipt','wrong_observed_time','changed_owner'])
def test_final_start_binds_pending_before_to_this_observe(live_observation,strict_regular,plan,fault):
    port,page,context,hits,before,pending,bound = prepared(live_observation,strict_regular,plan)
    port.pending = pending
    if fault == 'missing_before': pending.pop('before')
    elif fault == 'changed_before_inventory': pending['before']['carry_stock_indices'] = [99]
    elif fault == 'changed_inventory_receipt': pending['before']['reset_evidence']['inventory']['native_ok'] = False
    elif fault == 'wrong_observed_time': pending['observed_at'] = (port.now()-timedelta(seconds=1)).isoformat()
    else: live_observation[3][0] = 'reconnected'
    with pytest.raises(RuntimeError,match='empty_inventory'):
        page.action(context,np.zeros((720,1280,3),np.uint8),'start')
    assert page.expected is None


def test_failed_reobserve_clears_previous_proof(live_observation,strict_regular,plan):
    port,page,context,hits,before,pending,bound = prepared(live_observation,strict_regular,plan)
    live_observation[5]['carry_count'] = 1
    with pytest.raises(RuntimeError): port.observe(live_observation[1])
    assert port._regular_empty_observation is None
    with pytest.raises(RuntimeError,match='empty_inventory_unverified'):
        page.read(context,np.zeros((720,1280,3),np.uint8),'formation')


def test_sixty_second_existing_boundary_still_allows_regular(live_observation,strict_regular,plan):
    port,page,context,hits,before,pending,bound = prepared(live_observation,strict_regular,plan)
    live_observation[2][0] += timedelta(seconds=60)
    port.pending = pending
    assert page.action(context,np.zeros((720,1280,3),np.uint8),'start') == strict_regular['daily']['execution']['actions']['start']['target']


def test_missing_owner_source_cannot_certify_empty_inventory(live_observation):
    port,binding,clock,source,valid,lobby,empty = live_observation
    source[0] = None
    with pytest.raises(RuntimeError,match='empty_inventory_unverified'):
        port.observe(binding)
    assert port._regular_empty_observation is None


def test_observe_nonnull_then_prepare_null_reuses_actual_empty_readback(live_observation,strict_regular,plan,monkeypatch,tmp_path):
    from pcr_maa import clan_battle
    from test_clan_battle_native import Registry
    port,binding,clock,source,valid,lobby,empty = live_observation
    current = deepcopy(strict_regular)
    current['daily']['execution']['formation_mode']['carryover'] = deepcopy(current['daily']['execution']['formation_mode']['regular'])
    port.calibration = current
    before = port.observe(binding)
    target = before['bosses'][plan['boss']-1]
    action = dict(kind='regular',slot=1,stock_index=None,start_seconds=90,
        **{key:target[key] for key in ('boss','phase','round','instance_id')})
    next_plan = dict(plan,confirmed_for='next-strict-null-profile')
    port.directory = tmp_path / 'next-configure-slot'
    port.calibrations = {next_plan['confirmed_for']: strict_regular}
    port.page = daily.ClanBattleDailyExecutionPage(current,plan,port)
    port.tasker = NS(resource=Registry())
    port.prepare_inheritance = lambda: None
    port.form_team = lambda: None
    monkeypatch.setattr(clan_battle,'load_snapshot',lambda _: dict(ownership_complete=True,
        units=[dict(unit_id=uid,battle_rarity=star) for uid,star in zip(plan['unit_ids'],plan['stars'])]))
    _,_,hits,context = daily_tests.execution_context(strict_regular,next_plan,'formation')
    hits.update(('CBDailyFormationRegular','CBDailyActionLabel_start'))
    image = np.zeros((720,1280,3),np.uint8)
    def run(node,**kwargs):
        port.calls.append(node)
        action_name = node.removeprefix('CBAction_')
        if action_name in daily.DESTINATIONS:
            port.page.expected = daily.DESTINATIONS[action_name]
        return receipt({'node':node})
    port.run = run
    def read_team():
        return dict(port.page.read(context,image,'formation'),member_count=5,unit_ids=next_plan['unit_ids'][:],
            stars=next_plan['stars'][:],support_unit_id=None,support_owner_viewer_id=None)
    port.read_team = read_team
    # Production ordering: batch observes current slot before prepare configures the next one.
    ready = port.prepare(action,next_plan,binding)
    assert port.page.calibration['daily']['execution']['formation_mode']['carryover'] is None
    assert port._regular_empty_observation[0] == before
    port.pending = dict(action=deepcopy(action),observed_at=before['observed_at'],game_day=binding['game_day'],
        before=deepcopy(before),ready=ready)
    port.characters.formation = lambda *_: read_team()
    assert port.page.action(context,image,'start') == strict_regular['daily']['execution']['actions']['start']['target']
    assert port.calls == ['CBAction_open_carry','CBAction_close_empty_carry','CBAction_open_boss',
        'CBAction_open_formal','CBAction_open_formation']




def test_gui17_long_confirmation_and_formation_preserves_nonconsuming_read(live_observation,strict_regular,plan):
    # The observed wait was 03:04:42 -> 03:06:32 (+08), before any reserved intent.
    port,binding,clock,_,_,_,_ = live_observation
    clock[0] = datetime(2026,10,27,19,4,42,tzinfo=timezone.utc)
    binding['game_day'] = current_game_day(clock[0])
    port.bind_history(binding,{})
    port,page,context,hits,before,pending,bound = prepared(live_observation,strict_regular,plan)
    clock[0] = datetime(2026,10,27,19,6,32,tzinfo=timezone.utc)
    assert (clock[0]-datetime.fromisoformat(before['observed_at'])).total_seconds() == 110
    assert port.pending is None
    assert page.read(context,np.zeros((720,1280,3),np.uint8),'formation')['battle_kind'] == 'regular'


def test_long_preparation_requires_fresh_exact_observation_before_reserved_start(live_observation,strict_regular,plan):
    port,page,context,hits,before,pending,bound = prepared(live_observation,strict_regular,plan)
    clock,binding = live_observation[2],live_observation[1]
    clock[0] += timedelta(seconds=180)
    assert page.read(context,np.zeros((720,1280,3),np.uint8),'formation')['battle_kind'] == 'regular'
    port.pending = pending
    with pytest.raises(RuntimeError,match='final_start_binding_changed|empty_inventory_binding_changed'):
        page.action(context,np.zeros((720,1280,3),np.uint8),'start')
    port.pending = None
    fresh = port.observe(binding)
    assert fresh['observed_at'] != before['observed_at']
    # Even after a fresh read, the old intent cannot substitute its earlier snapshot.
    port.pending = pending
    with pytest.raises(RuntimeError,match='empty_inventory_binding_changed'):
        port.require_regular_empty_inventory(bound)
    pending.update(before=deepcopy(fresh),observed_at=fresh['observed_at'])
    assert page.action(context,np.zeros((720,1280,3),np.uint8),'start') == strict_regular['daily']['execution']['actions']['start']['target']


@pytest.mark.parametrize('fault',['source','account','invalid_account','day','inventory','future'])
def test_long_nonconsuming_preparation_keeps_identity_day_inventory_and_clock_guards(live_observation,strict_regular,plan,fault):
    port,page,context,hits,before,pending,bound = prepared(live_observation,strict_regular,plan)
    _,binding,clock,source,valid,lobby,_ = live_observation
    clock[0] += timedelta(seconds=180)
    if fault == 'source': source[0] = 'changed-owner-connection'
    elif fault == 'account': port._account_binding = object()
    elif fault == 'invalid_account': valid[0] = False
    elif fault == 'day': clock[0] += timedelta(days=1)
    elif fault == 'future': clock[0] = datetime.fromisoformat(before['observed_at'])-timedelta(seconds=1)
    else:
        lobby['carry_count'] = 1
        with pytest.raises(RuntimeError): port.observe(binding)
    with pytest.raises(RuntimeError,match='empty_inventory'):
        page.read(context,np.zeros((720,1280,3),np.uint8),'formation')


@pytest.fixture
def refresh_navigation(live_observation, strict_regular, plan):
    port, page, context, hits, before, pending, bound = prepared(live_observation, strict_regular, plan)
    port.page, page.current = page, 'lobby'
    port.calls.clear()
    hits.add('CBDailyActionLabel_cancel_formation')
    image = np.zeros((720, 1280, 3), np.uint8)
    old_proof, events = port._regular_empty_observation, []
    port.configure_slot = lambda *args, **kwargs: None
    port.prepare_inheritance = port.form_team = lambda: None
    def run(node, **kwargs):
        if node == 'CBAction_cancel_formation':
            page.action(context, image, 'cancel_formation')
            events.append(('cancel', port._regular_empty_observation is old_proof))
        port.calls.append(node)
        action = node.removeprefix('CBAction_')
        if action in daily.DESTINATIONS:
            page.current, page.expected = daily.DESTINATIONS[action], None
        return receipt({'node': node})
    def wait(names, **kwargs):
        name = 'CBPage_' + page.current
        if name == 'CBPage_formation':
            page.read(context, image, 'formation')
        if name not in names:
            assert names == ['CBPage_carry_empty']
            name, page.current = names[0], 'carry_empty'
        if name == 'CBPage_lobby':
            port.last_receipt = port.read_page('lobby')[1]
        return name
    port.run, port.wait = run, wait
    port.enter_lobby = lambda: daily.NativeClanDailyAdapter.enter_lobby(port)
    def read_team():
        return dict(page.read(context, image, 'formation'), member_count=5,
            unit_ids=plan['unit_ids'][:], stars=plan['stars'][:],
            support_unit_id=None, support_owner_viewer_id=None)
    port.read_team = read_team
    port.characters.formation = lambda *_: read_team()
    inventory = port.inventory
    def refresh():
        events.append(('inventory', port._regular_empty_observation))
        assert port._regular_empty_observation is None
        return inventory()
    port.inventory = refresh
    job = dict(plan, confirmed_for='synthetic-navigation-refresh')
    return port, live_observation[1], page, context, image, before, pending, job, events


def test_prepare_observe_prepare_preserves_proof_only_until_nonconsuming_return(refresh_navigation, live_observation):
    port, binding, page, context, image, before, pending, job, events = refresh_navigation
    action = deepcopy(port.action)
    live_observation[2][0] += timedelta(seconds=180)
    first = port.prepare(action, job, binding)
    assert page.current == 'formation' and port.pending is None
    fresh = port.observe(binding)
    final = port.prepare(action, job, binding)
    assert first['unit_ids'] == final['unit_ids'] and first['stars'] == final['stars']
    assert events == [('cancel', True), ('inventory', None)]
    assert port._regular_empty_observation[0] == fresh and fresh['observed_at'] != before['observed_at']
    assert port.calls == ['CBAction_open_boss', 'CBAction_open_formal', 'CBAction_open_formation',
        'CBAction_cancel_formation', 'CBAction_back_to_lobby', 'CBAction_open_carry',
        'CBAction_close_empty_carry', 'CBAction_open_boss', 'CBAction_open_formal', 'CBAction_open_formation']
    # Reobserving does not let an old pending snapshot substitute for the new proof.
    port.pending = pending
    with pytest.raises(RuntimeError, match='empty_inventory_binding_changed'):
        port.require_regular_empty_inventory(page.bound)
    pending.update(before=deepcopy(fresh), observed_at=fresh['observed_at'], ready=final)
    assert page.action(context, image, 'start') == port.calibration['daily']['execution']['actions']['start']['target']
    assert 'CBStart' not in port.calls


@pytest.mark.parametrize('fault', ['history', 'pending', 'day', 'owner', 'account', 'target', 'navigation', 'inheritance', 'inventory'])
def test_refresh_failures_clear_old_proof_without_consumption(refresh_navigation, live_observation, fault):
    port, binding, page, context, image, before, pending, job, events = refresh_navigation
    port.prepare(deepcopy(port.action), job, binding)
    port.calls.clear()
    def fail():
        raise RuntimeError('synthetic_refresh_failure')
    if fault == 'history': port.history_binding = dict(binding, account_id='changed')
    elif fault == 'pending': port.pending = pending
    elif fault == 'day': live_observation[2][0] += timedelta(days=1)
    elif fault == 'owner': live_observation[3][0] = 'changed-owner'
    elif fault == 'account': port._account_binding = object()
    elif fault == 'target': page.bound['instance_id'] = 'changed-instance'
    elif fault == 'navigation': port.enter_lobby = fail
    elif fault == 'inheritance': port.recover_inheritance = fail
    else: port.inventory = fail
    with pytest.raises(RuntimeError):
        port.observe(binding)
    assert port._regular_empty_observation is None
    assert 'CBStart' not in port.calls
    if fault not in ('inheritance', 'inventory'):
        assert port.calls == []


def test_three_minute_regular_session_starts_once_with_original_stock_time_and_fresh_ready(refresh_navigation, live_observation):
    port,binding,page,context,image,before,_,job,events=refresh_navigation
    port.plan=job
    port.checkpoint=lambda:None
    selections=[]
    def form_team():
        selections.append('formed')
        live_observation[2][0] += timedelta(seconds=180)
    port.form_team=form_team
    ready=port.prepare(deepcopy(port.action),job,binding)
    assert selections==['formed'] and events==[]
    assert 'CBAction_cancel_formation' not in port.calls
    assert port._regular_empty_observation[0]==before
    assert ready['evidence']['prepared_at']==port.now().isoformat()
    assert ready['evidence']['target']['native_ok'] is True
    assert port.prepared_regular_observation(before,ready) is True
    # The batch appends the existing authority record after native preparation.
    ready['evidence'].update(consumption_authorization={'reserved_intent_id':'synthetic-intent'},held_carryovers={})
    assert port.prepared_regular_observation(before,ready) is True
    port.pending=dict(action=deepcopy(port.action),before=deepcopy(before),ready=deepcopy(ready),
        observed_at=before['observed_at'],game_day=binding['game_day'],
        slot_confirmed_for=job['confirmed_for'],intent_id='synthetic-intent')
    assert page.action(context,image,'start')==port.calibration['daily']['execution']['actions']['start']['target']
    port.started,port.battle_start=True,'sent'
    assert port.opening_binding(pausing=True)==port.pending
    assert port.pending['before']['observed_at']==before['observed_at']
    assert (port.now()-datetime.fromisoformat(before['observed_at'])).total_seconds()==180
    assert port._regular_ready_at(before,ready)==ready['evidence']['prepared_at']


@pytest.mark.parametrize('field,value',[('cp',600),('remaining_count',2),('boss',99),('phase','E')])
def test_regular_session_checks_real_target_before_opening_formation(refresh_navigation,field,value):
    port,binding,_,_,_,_,_,job,_=refresh_navigation
    original=port.read_page
    def read(page):
        row,evidence=original(page)
        if page=='target':row[field]=value
        return row,evidence
    port.read_page=read
    with pytest.raises(RuntimeError,match='target_counters_changed'):
        port.prepare(deepcopy(port.action),job,binding)
    assert 'CBAction_open_formation' not in port.calls and port._regular_prepared is None


@pytest.mark.parametrize('fault',['owner','account','day','instance','pending','unknown_start','ready_team','ready_time','old_before','expired_ready','stop'])
def test_retained_session_stops_on_changed_binding_or_unresolved_input(refresh_navigation,live_observation,fault):
    port,binding,_,_,_,before,_,job,_=refresh_navigation
    port.checkpoint=lambda:None
    port.form_team=lambda:live_observation[2].__setitem__(0,live_observation[2][0]+timedelta(seconds=180))
    ready=port.prepare(deepcopy(port.action),job,binding)
    assert port.prepared_regular_observation(before,ready) is True
    if fault=='owner':live_observation[3][0]='another-owner'
    elif fault=='account':port._account_binding=object()
    elif fault=='day':live_observation[2][0]+=timedelta(days=1)
    elif fault=='instance':port.action=dict(port.action,instance_id='changed')
    elif fault=='pending':port.pending={'unknown':'intent'}
    elif fault=='unknown_start':port.battle_start='unknown'
    elif fault=='ready_team':ready['unit_ids'][0]=999
    elif fault=='ready_time':ready['evidence']['prepared_at']=before['observed_at']
    elif fault=='old_before':before=dict(before,cp=600)
    elif fault=='expired_ready':live_observation[2][0]+=timedelta(seconds=61)
    else:
        def stop():raise RuntimeError('stopped')
        port.checkpoint=stop
    port.calls.clear()
    with pytest.raises(RuntimeError):port.prepared_regular_observation(before,ready)
    assert port.calls==[]


def test_observe_reuses_inventory_return_receipt_without_another_lobby_read(live_observation):
    port,binding,clock,_,_,_,_ = live_observation
    read,wait = port.read_page,port.wait
    returned = []
    def qualified_wait(names, **kwargs):
        hit = wait(names, **kwargs)
        if names == ['CBPage_lobby']:
            clock[0] += timedelta(seconds=2)
            returned.append(port.last_receipt)
        return hit
    def read_before_return(page):
        assert not returned, 'must reuse qualified lobby receipt instead of recapturing'
        return read(page)
    port.wait,port.read_page = qualified_wait,read_before_return
    observation = port.observe(binding)
    assert len(returned) == 1 and observation['cp_evidence'] is returned[0]
    assert observation['observed_at'] == clock[0].isoformat()
    assert port.calls == ['CBAction_open_carry','CBAction_close_empty_carry']


def test_inventory_return_wait_failure_never_produces_an_observation(live_observation):
    port,binding,_,_,_,_,_ = live_observation
    port.observe(binding)
    wait = port.wait
    def failed_return(names, **kwargs):
        if names == ['CBPage_lobby']:
            raise RuntimeError('synthetic_lobby_wait_failed')
        return wait(names, **kwargs)
    port.wait = failed_return
    with pytest.raises(RuntimeError,match='synthetic_lobby_wait_failed'):
        port.observe(binding)
    assert port._regular_empty_observation is None
    assert 'CBStart' not in port.calls


@pytest.mark.parametrize('fault',['count','inventory','owner','day'])
def test_reused_return_receipt_keeps_count_inventory_owner_and_day_checks(live_observation,fault):
    port,binding,clock,source,_,_,_ = live_observation
    wait = port.wait
    def changed_return(names, **kwargs):
        hit = wait(names, **kwargs)
        if names == ['CBPage_lobby']:
            reading = daily._detail(port.last_receipt)
            if fault == 'count': reading['remaining_count'] = 2
            elif fault == 'inventory': reading['carry_count'] = 1
            elif fault == 'owner': source[0] = 'changed-owner-after-inventory'
            else: clock[0] += timedelta(days=1)
        return hit
    port.wait = changed_return
    with pytest.raises(RuntimeError):
        port.observe(binding)
    assert port._regular_empty_observation is None
    assert 'CBStart' not in port.calls
