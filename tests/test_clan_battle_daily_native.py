"""Offline page contracts; these do not constitute formal-battle acceptance."""
from copy import deepcopy
import gc
import hashlib
import json
from types import SimpleNamespace as NS
import weakref
import time
from datetime import timedelta

import numpy as np
import pytest

from pcr_maa import clan_battle_daily_native as daily
from test_clan_battle_native import calibration, calibration_v2, context_for, plan, Registry, receipt
from test_clan_battle_batch import daily_request, Port
from test_agent_callbacks import registry, FakeContext


@pytest.fixture
def profile(calibration_v2):
    value = deepcopy(calibration_v2)
    label = dict(roi=[10, 10, 100, 30], expected='^离线页面锚点$')
    value['daily'] = dict(version=1, cp_semantics='daily_earned', pages={
        page: dict(labels=[deepcopy(label), deepcopy(label)], rejects=[deepcopy(label)],
            fields={field: [100, 100 + index * 50, 300, 40] for index, field in enumerate(sorted(fields))})
        for page, fields in daily.PAGE_FIELDS.items()},
        samples=[dict(value['samples'][0], page=page) for page in daily.PAGE_FIELDS])
    return value


def reading(profile, plan, page, **changes):
    nodes = daily.daily_pipeline(profile, plan)
    values = dict(remaining_count='0', carry_count='0', cp='0/900', health='0/60000000',
        boss='合成魔物3', phase='第2阶段')
    values.update(changes)
    texts = {f'CBDaily_{page}_{field}': values[field] for field in daily.PAGE_FIELDS[page]}
    hits = {f'CBDaily_{page}_labels_{index}' for index in range(2)}
    context = context_for(nodes, texts, hits)
    return daily.ClanBattleDailyPage(profile, plan), context, np.zeros((720, 1280, 3), np.uint8), hits


def test_daily_profile_has_only_read_operations_and_zero_challenges_is_valid(profile, plan):
    nodes = daily.daily_pipeline(profile, plan)
    assert all(node['action'] == 'DoNothing' for node in nodes.values())
    page, context, image, _ = reading(profile, plan, 'lobby')
    actual = page.read(context, image, 'lobby')
    assert actual['remaining_count'] == actual['carry_count'] == actual['cp'] == 0
    assert actual['cp_semantics'] == 'daily_earned'
    assert actual['evidence']['frame_pixel_sha256'] == hashlib.sha256(image.tobytes()).hexdigest()
    assert not {'used_unit', 'round', 'instance_id', 'reset_verified'} & actual.keys()


def test_daily_formal_detail_distinguishes_defeated_health_without_fabricated_round(profile, plan):
    page, context, image, _ = reading(profile, plan, 'target')
    actual = page.read(context, image, 'target')
    assert actual['formal'] is True and actual['alive'] is False and actual['health'] == 0
    assert actual['boss'] == 3 and actual['phase'] == 'B'
    assert not {'round', 'instance_id', 'appeared_at', 'simulation'} & actual.keys()


def test_daily_formal_detail_reads_current_health_and_cumulative_cp(profile, plan):
    page, context, image, _ = reading(profile, plan, 'target', health='25,000,000 / 60,000,000',
        remaining_count='1', cp='345 / 900')
    actual = page.read(context, image, 'target')
    assert actual['alive'] is True and actual['health'] == 25000000 and actual['health_max'] == 60000000
    assert actual['cp'] == 345 and actual['remaining_count'] == 1


@pytest.mark.parametrize('field,value', [('cp', '901/900'), ('cp', '0/300'),
    ('remaining_count', '4'), ('health', '70000000/60000000'), ('boss', '合成魔物4'),
    ('phase', '未知阶段')])
def test_daily_unknown_or_invalid_values_do_not_produce_observation(profile, plan, field, value):
    page, context, image, _ = reading(profile, plan, 'target', **{field: value})
    assert page.analyze(context, NS(image=image, custom_recognition_param='{"page":"target"}')) is None


def test_daily_reject_markers_block_background_lobby_and_formal_details(profile, plan):
    for location in ('lobby', 'target'):
        page, context, image, hits = reading(profile, plan, location)
        hits.add(f'CBDaily_{location}_rejects_0')
        with pytest.raises(RuntimeError, match='page_unknown'):
            page.read(context, image, location)


def test_unknown_guide_phase_mapping_keeps_visible_label_without_inventing_phase(profile, plan):
    profile['phase_labels'] = {}
    page, context, image, _ = reading(profile, plan, 'target')
    actual = page.read(context, image, 'target')
    assert actual['phase_label'] == '第2阶段' and actual['phase'] is None


def test_empty_carry_inventory_requires_both_modal_and_empty_status_markers(profile, plan):
    page, context, image, hits = reading(profile, plan, 'carry_empty')
    assert page.read(context, image, 'carry_empty')['carry_stock_indices'] == []
    hits.remove('CBDaily_carry_empty_labels_1')
    with pytest.raises(RuntimeError, match='page_unknown'):
        page.read(context, image, 'carry_empty')


@pytest.mark.parametrize('failure', ['stop', 'shape', 'disconnect', 'unknown', 'params'])
def test_daily_page_callback_stops_without_readback(profile, plan, failure):
    page, context, image, hits = reading(profile, plan, 'lobby')
    params = dict(page='lobby')
    if failure == 'stop': context.tasker.stopping = True
    elif failure == 'shape': image = image[:100]
    elif failure == 'disconnect': hits.add('LabyDisconnect6002Observed')
    elif failure == 'unknown': params['page'] = 'formation'
    else: params['authorize'] = True
    assert page.analyze(context, NS(image=image, custom_recognition_param=json.dumps(params))) is None


@pytest.mark.parametrize('change', ['sample', 'hash', 'semantics', 'version', 'fields', 'labels', 'extra'])
def test_daily_profile_requires_all_bound_pages_and_known_cp_semantics(profile, plan, change):
    spec = profile['daily']
    if change == 'sample': spec['samples'].pop()
    elif change == 'hash': spec['samples'][0]['sha256'] = '0' * 64
    elif change == 'semantics': spec['cp_semantics'] = 'available_balance'
    elif change == 'version': spec['version'] = True
    elif change == 'fields': spec['pages']['lobby']['fields']['reset_verified'] = [1, 1, 20, 20]
    elif change == 'labels': spec['pages']['target']['labels'].pop()
    else: spec['execute'] = True
    with pytest.raises(ValueError):
        daily.daily_pipeline(profile, plan)


def test_daily_page_callback_belongs_to_native_resource_and_duplicate_registration_stops(profile, plan, tmp_path):
    owner, borrowed = Registry(), Registry()
    tasker = NS(resource=borrowed, _resource_holder=owner)
    port = daily.NativeClanDailyPages(tasker, tmp_path, calibration=profile, plan=plan)
    callback = weakref.ref(port.page)
    del port
    gc.collect()
    assert callback() is owner.callbacks['ClanBattleDailyPage'] and not borrowed.callbacks
    with pytest.raises(RuntimeError, match='callback_registration_failed'):
        daily.NativeClanDailyPages(tasker, tmp_path, calibration=profile, plan=plan)
    assert owner.callbacks['ClanBattleDailyPage'] is callback()


def test_daily_native_probe_dispatches_only_known_read_node(profile, plan, tmp_path, monkeypatch):
    port = daily.NativeClanDailyPages(NS(resource=Registry()), tmp_path, calibration=profile, plan=plan)
    calls = []
    monkeypatch.setattr(port, 'run', lambda node: calls.append(node) or receipt(dict(page='lobby', cp=0)))
    value, evidence = port.read_page('lobby')
    assert value == dict(page='lobby', cp=0) and evidence['native_ok']
    assert calls == ['CBDailyPage_lobby']
    with pytest.raises(ValueError):
        port.read_page('start')
    assert calls == ['CBDailyPage_lobby']


@pytest.fixture
def executable(profile):
    value = deepcopy(profile)
    daily_spec = value['daily']
    daily_spec['version'] = 2
    daily_spec['pages']['lobby']['fields'].update(period=[10, 10, 200, 30], phase=[220, 10, 120, 30])
    label = dict(roi=[10, 10, 100, 30], expected='^离线页面锚点$')
    def layout(count, y):
        return dict(slots=[[10 + i * 70, y, 55, 55] for i in range(count)],
            empty=[deepcopy(label) for _ in range(count)], borrowed=[deepcopy(label) for _ in range(count)])
    pages = {page: dict(labels=[deepcopy(label), deepcopy(label)], rejects=[deepcopy(label)],
        fields={field: [100, 100 + index * 50, 300, 40] for index, field in enumerate(sorted(fields))})
        for page, fields in daily.EXECUTION_FIELDS.items()}
    actions = {name: dict(page=page, label=deepcopy(label), target=[10, 10, 20, 20])
        for name, page in daily.ACTION_PAGES.items()}
    for name in ('characters_top', 'characters_next', 'support_top', 'support_next'):
        actions[name].update(begin=[1000, 350, 1, 1], end=[1000, 180, 1, 1])
    daily_spec['execution'] = dict(pages=pages, actions=actions,
        used_units=layout(15, 100), boss_slots={str(boss): dict(round=[100 * boss, 200, 90, 30],
            defeated=deepcopy(label)) for boss in range(1, 6)}, formation_owner=[600, 600, 120, 30],
        speed_one=deepcopy(label), carry_slots=[dict(empty=deepcopy(label), occupied=deepcopy(label),
            stock_index=[100, 300, 100, 30], time=[100, 340, 100, 30], members=layout(5, 100 + index * 150),
            support_owner=[100, 380, 100, 30], target=[100, 300, 200, 200]) for index in range(3)],
        samples=[dict(value['samples'][0], page=page) for page in daily.EXECUTION_FIELDS])
    daily_spec['execution']['formation_mode'] = dict(regular=deepcopy(label), carryover=deepcopy(label),
        stock_index=[600, 550, 100, 30], time=[720, 550, 100, 30])
    return value


def test_complete_execution_profile_adds_only_guarded_single_inputs(executable, plan):
    nodes = daily.require_daily_execution(executable, plan)
    assert nodes['CBAction_open_formal']['custom_recognition_param'] == dict(page='boss_detail', action='open_formal')
    assert nodes['CBAction_characters_next']['action'] == 'Swipe'
    assert all(node['recognition'] == 'Custom' and node['max_hit'] == 1
        for node in nodes.values() if node['action'] in ('Click', 'Swipe'))


@pytest.mark.parametrize('field', ['pages', 'actions', 'samples', 'boss_slots', 'used_units', 'carry_slots'])
def test_execution_profile_missing_consumption_evidence_is_rejected(executable, plan, field):
    executable['daily']['execution'][field] = []
    with pytest.raises(ValueError):
        daily.require_daily_execution(executable, plan)


def execution_context(executable, plan, page, **texts):
    nodes = daily.require_daily_execution(executable, plan)
    hits = {f'CBDaily_{page}_labels_{index}' for index in range(2)}
    actual_texts = {f'CBDaily_{page}_{field}': value for field, value in texts.items()}
    return nodes, actual_texts, hits, context_for(nodes, actual_texts, hits)


def test_formal_battle_and_result_require_continuous_reserved_target(executable, plan):
    _, texts, hits, context = execution_context(executable, plan, 'battle', remaining_seconds='1:30')
    port = NS(support_unit_id=None, pending={'intent_id': 'reserved'}, started=True, battle_start='sent')
    page = daily.ClanBattleDailyExecutionPage(executable, plan, port)
    image = np.zeros((720, 1280, 3), np.uint8)
    with pytest.raises(RuntimeError, match='transition_unverified'):
        page.read(context, image, 'battle')
    page.bound = dict(period=plan['period'], boss=plan['boss'], phase='B', round=5, instance_id='actual-round-five', formal=True)
    page.current, page.expected = 'formation', 'battle'
    hits.add('CBDailyOneSpeed')
    state = page.read(context, image, 'battle')
    assert state['formal'] is True and state['round'] == 5 and state['battle_speed'] == 1
    hits.clear()
    hits.update({'CBDaily_result_labels_0', 'CBDaily_result_labels_1'})
    texts['CBDaily_result_damage'] = '12,345'
    # A result may follow the running battle without another input operation.
    assert page.read(context, image, 'result')['damage'] == 12345


def test_used_units_reader_excludes_support_in_multiple_completed_teams(executable, plan):
    _, _, hits, context = execution_context(executable, plan, 'used_units', game_day='2026-10-25')
    slots = executable['daily']['execution']['used_units']['slots']
    units = list(range(100001, 100016))
    port = NS(all_units=units, catalogue=NS(match=lambda image, uid, roi: {'box': roi}
        if uid == units[slots.index(roi)] else None))
    page = daily.ClanBattleDailyExecutionPage(executable, plan, port)
    hits.update({'CBDailyUsedBorrowed4', 'CBDailyUsedBorrowed9', 'CBDailyUsedBorrowed14'})
    reading = page.read(context, np.zeros((720, 1280, 3), np.uint8), 'used_units')
    assert reading['used_unit'] == [uid for index, uid in enumerate(units) if index not in (4, 9, 14)]


def test_nonempty_carry_stock_requires_actual_index_time_and_bound_team(executable, plan):
    _, texts, hits, context = execution_context(executable, plan, 'carry')
    layout = executable['daily']['execution']['carry_slots'][0]['members']['slots']
    units = plan['unit_ids']
    port = NS(all_units=units, catalogue=NS(match=lambda image, uid, roi: {'box': roi}
        if uid == units[layout.index(roi)] else None))
    page = daily.ClanBattleDailyExecutionPage(executable, plan, port)
    hits.update({'CBDailyCarry0Occupied', 'CBDailyCarry1Empty', 'CBDailyCarry2Empty', 'CBDailyCarry0Borrowed4'})
    texts.update(CBDailyCarry0Index='2', CBDailyCarry0Time='34', CBDailyCarry0Owner='987654321')
    result = page.read(context, np.zeros((720, 1280, 3), np.uint8), 'carry')['stocks']
    assert result[0]['stock_index'] == 2 and result[0]['time'] == 34
    assert result[0]['using_unit'] == units and result[0]['support_owner_viewer_id'] == '987654321'
    texts['CBDailyCarry0Owner'] = '昵称不是玩家ID'
    with pytest.raises(RuntimeError, match='field_unknown'):
        page.read(context, np.zeros((720, 1280, 3), np.uint8), 'carry')


def test_daily_native_start_receipt_is_bound_to_exact_formal_intent(tmp_path, monkeypatch):
    from pcr_maa import native
    port = object.__new__(daily.NativeClanDailyAdapter)
    daily.NativeClanBattle.__init__(port, NS(), tmp_path)
    port.pending, port.action = dict(intent_id='reserved'), dict(kind='regular')
    port.page = NS(expected=None)
    evidence = dict(input_execution='not_sent')
    def reject(*args, **kwargs):
        raise native.NativeInputNotSent(evidence, 'no start dispatched')
    monkeypatch.setattr(daily.NativeGuideTrial, 'run', reject)
    with pytest.raises(native.NativeInputNotSent) as exc:
        port.run('CBStart', click=True)
    assert port.battle_start == 'not_sent'
    assert exc.value.receipt['daily_intent_id'] == 'reserved'
    assert exc.value.receipt['clan_battle_phase'] == 'regular_start'
    with pytest.raises(RuntimeError, match='start_not_reserved'):
        port.run('CBStart', click=True)


def test_daily_native_unknown_start_cannot_be_resent(tmp_path, monkeypatch):
    port = object.__new__(daily.NativeClanDailyAdapter)
    daily.NativeClanBattle.__init__(port, NS(), tmp_path)
    port.pending, port.action = dict(intent_id='reserved'), dict(kind='carryover')
    monkeypatch.setattr(daily.NativeGuideTrial, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('unknown SDK outcome')))
    with pytest.raises(RuntimeError, match='unknown SDK'):
        port.run('CBStart', click=True)
    assert port.battle_start == 'unknown'
    with pytest.raises(RuntimeError, match='start_not_reserved'):
        port.run('CBStart', click=True)


@pytest.mark.parametrize('change', ['round', 'phase', 'dead'])
def test_final_boss_selection_reads_current_round_instead_of_cached_identity(executable, plan, change, monkeypatch):
    from pcr_maa.clan_battle_batch import _hash
    actual = dict(boss=plan['boss'], phase='B', round=5, available=True)
    action = dict(actual, instance_id=_hash(dict(period=plan['period'], boss=plan['boss'], phase='B', round=5)))
    page = daily.ClanBattleDailyExecutionPage(executable, plan, NS(action=action))
    if change == 'round': actual['round'] = 6
    elif change == 'phase': actual['phase'] = 'C'
    else: actual['available'] = False
    monkeypatch.setattr(page, 'read', lambda *args: dict(period=plan['period'], bosses=[actual]))
    monkeypatch.setattr(page, 'hit', lambda *args: True)
    with pytest.raises(RuntimeError, match='target_changed_before_selection'):
        page.action(NS(), np.zeros((720, 1280, 3), np.uint8), 'open_boss')


@pytest.mark.parametrize('changed', ['regular_mode', 'stock', 'time'])
def test_carry_formation_cannot_fall_back_to_regular_or_another_stock(executable, plan, changed):
    _, texts, hits, context = execution_context(executable, plan, 'formation')
    action = dict(kind='carryover', stock_index=2, start_seconds=34)
    page = daily.ClanBattleDailyExecutionPage(executable, plan, NS(action=action, support_unit_id=None))
    page.bound = dict(period=plan['period'], formal=True, boss=plan['boss'], phase='B', round=5, instance_id='real-round')
    page.expected = 'formation'
    hits.add('CBDailyFormationRegular' if changed == 'regular_mode' else 'CBDailyFormationCarryover')
    texts.update(CBDailyFormationStock='3' if changed == 'stock' else '2',
        CBDailyFormationTime='33' if changed == 'time' else '34')
    with pytest.raises(RuntimeError, match='consumption_mode_unknown|selected_carry_changed'):
        page.read(context, np.zeros((720, 1280, 3), np.uint8), 'formation')


@pytest.mark.parametrize('change', ['owner', 'order', 'day', 'stale'])
def test_final_start_callback_rechecks_pending_team_and_game_day(executable, plan, monkeypatch, change):
    from pcr_maa.game_day import current_game_day
    now = daily.datetime.now(daily.timezone.utc)
    actual = dict(unit_ids=plan['unit_ids'][:], stars=plan['stars'][:], support_unit_id=plan['unit_ids'][-1],
        support_owner_viewer_id='987654321')
    ready = dict(unit_ids=plan['unit_ids'][:], stars=plan['stars'][:], support_unit=plan['unit_ids'][-1],
        support_owner_viewer_id='987654321')
    action = dict(kind='regular')
    pending = dict(action=action, game_day=current_game_day(now), observed_at=now.isoformat(), ready=ready)
    if change == 'owner': actual['support_owner_viewer_id'] = '888888888'
    elif change == 'order': actual['unit_ids'].reverse()
    elif change == 'day': now += timedelta(days=1)
    else: now += timedelta(seconds=61)
    port = NS(action=action, pending=pending, now=lambda: now, characters=NS(formation=lambda *args: actual))
    page = daily.ClanBattleDailyExecutionPage(executable, plan, port)
    monkeypatch.setattr(page, 'read', lambda *args: {})
    monkeypatch.setattr(page, 'hit', lambda *args: True)
    monkeypatch.setattr(page, 'require_team', lambda value: None)
    with pytest.raises(RuntimeError, match='final_start_binding_changed|final_team_changed'):
        page.action(NS(), np.zeros((720, 1280, 3), np.uint8), 'start')


def test_empty_team_does_not_require_a_support_owner_before_support_is_selected(monkeypatch):
    reader = object.__new__(daily._DailyTeamReader)
    reader.page = NS(word=lambda *args: pytest.fail('empty team must not read an owner ID'))
    monkeypatch.setattr(daily.ClanBattleCharacters, 'formation', lambda *args: dict(support_unit_id=None))
    assert reader.formation(NS(), np.zeros((720, 1280, 3), np.uint8))['support_owner_viewer_id'] is None


def test_native_observe_uses_actual_round_identity_and_only_verified_refresh_appearance(plan, monkeypatch):
    from pcr_maa.game_day import current_game_day
    port = object.__new__(daily.NativeClanDailyAdapter)
    port.plan, port.last_bosses, port.boss_history = plan, {}, {}
    port.account_receipt = {'account': plan['account_id']}
    clock = [daily.datetime.now(daily.timezone.utc)]
    bosses = [dict(boss=number, phase='B', round=1 if number == 3 else None, available=number == 3)
        for number in range(1, 6)]
    monkeypatch.setattr(port, 'now', lambda: clock[0])
    monkeypatch.setattr(port, 'enter_lobby', lambda: None)
    monkeypatch.setattr(port, 'run', lambda *args, **kwargs: {})
    monkeypatch.setattr(port, 'wait', lambda names, **kwargs: names[0])
    monkeypatch.setattr(port, 'inventory', lambda: ([], {'stock_empty': True}))
    def read(page):
        if page == 'used_units':
            return dict(game_day=current_game_day(clock[0]), used_unit=[]), {'record_day': current_game_day(clock[0])}
        return dict(period=plan['period'], remaining_count=0, carry_count=0, cp=0, bosses=deepcopy(bosses)), {'frame': clock[0].isoformat()}
    monkeypatch.setattr(port, 'read_page', read)
    binding = dict(game_day=current_game_day(clock[0]))
    first = port.observe(binding)['bosses'][2]
    assert first['instance_id'] and first['appeared_at'] is None
    clock[0] += timedelta(seconds=1)
    assert port.observe(binding)['bosses'][2]['instance_id'] == first['instance_id']
    bosses[2].update(available=False, round=None)
    assert port.observe(binding)['bosses'][2]['instance_id'] is None
    bosses[2].update(available=True, round=2)
    clock[0] += timedelta(seconds=1)
    refreshed = port.observe(binding)['bosses'][2]
    assert refreshed['instance_id'] != first['instance_id']
    assert refreshed['appeared_at'] == clock[0].isoformat() and 'previous' in refreshed['evidence']


@pytest.mark.parametrize('kind', ['regular', 'carryover'])
def test_native_prepare_uses_formal_target_and_distinct_regular_or_stock_entry(plan, monkeypatch, kind):
    port = object.__new__(daily.NativeClanDailyAdapter)
    action = dict(kind=kind, slot=1, boss=plan['boss'], round=5, phase='B', instance_id='five',
        stock_index=None if kind == 'regular' else 2, start_seconds=90 if kind == 'regular' else 34)
    port.pending, port.support_unit_id = None, None
    port.last_bosses = {plan['boss']: deepcopy(action)}
    port.account_receipt, port.calibration = {'account': plan['account_id']}, {'calibration_id': 'offline'}
    port.page = NS(bound=None, require_team=lambda reading: None)
    calls = []
    monkeypatch.setattr(port, 'enter_lobby', lambda: calls.append('lobby'))
    monkeypatch.setattr(port, 'configure_slot', lambda job, capability: calls.append(capability))
    monkeypatch.setattr(port, 'run', lambda name, **kwargs: calls.append(name))
    monkeypatch.setattr(port, 'wait', lambda names, **kwargs: names[0])
    monkeypatch.setattr(port, 'form_team', lambda: calls.append('form_team'))
    monkeypatch.setattr(port, 'read_team', lambda: dict(unit_ids=plan['unit_ids'], stars=plan['stars'], support_unit_id=None))
    ready = port.prepare(action, dict(plan, confirmed_for='reviewed'), {'account_id': plan['account_id']})
    assert ready['kind'] == kind and ready['evidence']['mode'] == 'formal'
    assert calls[:4] == ['lobby', 'formal' if kind == 'regular' else 'carryover', 'CBAction_open_boss', 'CBAction_open_formal']
    assert ('form_team' in calls) == (kind == 'regular')
    assert ('CBAction_select_carry' in calls) == (kind == 'carryover')


def test_daily_adapter_callbacks_remain_on_real_owner_when_slot_reader_changes(executable, plan, tmp_path, monkeypatch):
    from pcr_maa import clan_battle
    plan = dict(plan, confirmed_for='synthetic-reviewed-plan')
    monkeypatch.setattr(clan_battle, 'load_snapshot', lambda _: dict(ownership_complete=True,
        units=[dict(unit_id=uid) for uid in plan['unit_ids']]))
    owner, borrowed = Registry(), Registry()
    tasker = NS(resource=borrowed, _resource_holder=owner)
    port = daily.NativeClanDailyAdapter(tasker, tmp_path, request={'slot_plans': {'1': plan}},
        calibrations={plan['confirmed_for']: executable})
    previous = weakref.ref(port.characters)
    router = owner.callbacks['ClanBattleCharacters']
    port.configure_slot(plan)
    gc.collect()
    assert previous() is None and router.port.characters is port.characters
    reference = weakref.ref(port)
    del port
    gc.collect()
    assert reference() is router.port and not borrowed.callbacks


@pytest.mark.parametrize('capability', ['formal', 'carryover'])
def test_formal_slot_excludes_simulation_opening_without_changing_shared_calibration(
        executable, plan, tmp_path, monkeypatch, capability):
    from pcr_maa import clan_battle, clan_battle_calibration
    from pcr_maa.clan_battle_native import calibration_pipeline
    from test_clan_opening import sampled_plan
    shipped = clan_battle_calibration.load_calibration(sampled_plan())
    executable['auto_battle'] = deepcopy(shipped['auto_battle'])
    executable['pages']['battle']['speed_two'] = deepcopy(shipped['pages']['battle']['speed_two'])
    original = deepcopy(executable)
    plan = dict(plan, confirmed_for='synthetic-reviewed-plan')
    monkeypatch.setattr(clan_battle, 'load_snapshot', lambda _: dict(ownership_complete=True,
        units=[dict(unit_id=uid) for uid in plan['unit_ids']]))
    loaded = []
    def carry_calibration(job, *, capability):
        loaded.append(capability)
        return executable
    monkeypatch.setattr(clan_battle_calibration, 'load_calibration', carry_calibration)
    resource = Registry()
    port = daily.NativeClanDailyAdapter(NS(resource=resource), tmp_path,
        request={'slot_plans': {'1': plan}}, calibrations={plan['confirmed_for']: executable})
    port.configure_slot(plan, capability=capability)
    assert resource.nodes['CBStart']['next'] == []
    assert 'CBPauseOpening' not in resource.nodes and 'auto_battle' not in port.calibration
    assert executable == original and port.calibrations[plan['confirmed_for']] == original
    assert loaded == (['carryover'] if capability == 'carryover' else [])
    assert calibration_pipeline(executable, plan)['CBStart']['next'] == ['CBPauseOpening']


@pytest.mark.filterwarnings('error::pytest.PytestUnraisableExceptionWarning')
def test_real_sdk_dispatches_daily_callbacks_after_port_gc_without_input(executable, plan, tmp_path, monkeypatch):
    from pcr_maa import clan_battle, native
    from pcr_maa.replay import SavedImages
    plan = dict(plan, confirmed_for='synthetic-reviewed-plan')
    monkeypatch.setattr(clan_battle, 'load_snapshot', lambda _: dict(ownership_complete=True,
        units=[dict(unit_id=uid) for uid in plan['unit_ids']]))
    resource = native.load_resource()
    controller = SavedImages([[np.zeros((720, 1280, 3), np.uint8)]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    port = daily.NativeClanDailyAdapter(tasker, tmp_path, request={'slot_plans': {'1': plan}},
        calibrations={plan['confirmed_for']: executable})
    reads, analyze = [], daily.ClanBattleDailyExecutionPage.analyze
    def watched(self, context, argv):
        reads.append('page')
        return analyze(self, context, argv)
    monkeypatch.setattr(daily.ClanBattleDailyExecutionPage, 'analyze', watched)
    reference = weakref.ref(port)
    del port
    gc.collect()
    try:
        assert reference() is not None
        assert native.run(tasker, 'CBPage_lobby')['native_ok'] is False
        assert native.run(tasker, 'CBReadTeam')['native_ok'] is False
        assert reads and not controller.clicks and not controller.swipes
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


def test_daily_adapter_registers_and_dispatches_through_owner_scope(registry, executable, plan, tmp_path, monkeypatch):
    from pcr_maa import clan_battle
    plan = dict(plan, confirmed_for='synthetic-reviewed-plan')
    monkeypatch.setattr(clan_battle, 'load_snapshot', lambda _: dict(ownership_complete=True,
        units=[dict(unit_id=uid) for uid in plan['unit_ids']]))
    context, calls = FakeContext(), []
    with registry.scope(context, deadline=time.monotonic() + 10) as facade:
        port = daily.NativeClanDailyAdapter(facade, tmp_path, request={'slot_plans': {'1': plan}},
            calibrations={plan['confirmed_for']: executable})
        assert facade._callback_scope.bindings['ClanBattleDailyPage'] is port.page
        assert facade._callback_scope.bindings['ClanBattleCharacters'] is port.callback
        def analyze(ctx, argv):
            calls.append(argv.custom_recognition_name)
            return port.page.AnalyzeResult(box=[0, 0, 1, 1], detail={'scope_checked': True})
        monkeypatch.setattr(port.page, 'analyze', analyze)
        argv = NS(custom_recognition_name='ClanBattleDailyPage', task_detail=NS(task_id=1),
            image=np.zeros((720, 1280, 3), np.uint8), custom_recognition_param='{"page":"lobby"}')
        with facade._callback_scope.dispatch():
            assert registry.recognitions['ClanBattleDailyPage'].analyze(context, argv) is not None
    assert calls == ['ClanBattleDailyPage'] and not facade._callback_scope.bindings
    assert registry.recognitions['ClanBattleDailyPage'].analyze(context, argv) is None


@pytest.fixture
def native_protocol(tmp_path, daily_request, executable, monkeypatch):
    from pcr_maa import clan_battle, clan_battle_calibration
    class Protocol(daily.NativeClanDailyAdapter):
        def __init__(self):
            self.state = Port(daily_request)
            self.directory = tmp_path / 'native-protocol'
            self.directory.mkdir()
            self.pending, self.action, self.started = None, None, False
            self.battle_start, self.stocks, self.return_failure = 'not_sent', [], False
            self.page = NS(require_team=lambda team: None)
            self.calibration = {'version': 2}
            self.timing_capability = self.state.timing_capability
            self.calls = []
        def checkpoint(self): pass
        def now(self): return self.state.now()
        def prepare(self, action, job, binding):
            self.action, self.plan, self.start_seconds = action, job, action['start_seconds']
            self.current_ready = self.state.prepare(action, job, binding)
            return deepcopy(self.current_ready)
        def read_team(self):
            row = self.current_ready
            return dict(unit_ids=row['unit_ids'], stars=row['stars'], support_unit_id=row['support_unit'],
                support_owner_viewer_id=row['support_owner_viewer_id'])
        def run(self, node, *, click=False):
            self.calls.append(node)
            if node == 'CBStart':
                self.started, self.battle_start = True, 'sent'
                self.finished = self.state.execute(self.pending, self.plan)
                if self.finished['grant']:
                    self.stocks.append(deepcopy(self.finished['grant']))
                self.stocks = [row for row in self.stocks if row['stock_index'] in self.state.inventory]
            if node == 'CBAction_return_to_lobby' and self.return_failure:
                raise RuntimeError('synthetic_return_failed')
            self.last_receipt = receipt({'node': node})
            return self.last_receipt
        def wait(self, names, **kwargs): return names[0]
        def read_page(self, name):
            assert name == 'result'
            return {'damage': self.finished['damage']}, receipt({'damage': self.finished['damage']})
        def observe(self, binding):
            row = self.state.observe(binding)
            row['reset_evidence']['inventory'] = {'source': 'synthetic_bound_stock_page'}
            if hasattr(self, 'finished') and self.finished['grant']:
                self.stock_observed_at = self.finished['grant']['issued_at']
            else:
                self.stock_observed_at = row['observed_at']
            return row
    port = Protocol()
    monkeypatch.setattr(clan_battle, 'execute_switches', lambda owner, job, initial: port.finished['settings_evidence'])
    monkeypatch.setattr(clan_battle_calibration, 'load_calibration', lambda job, capability: executable)
    return port


def test_native_execute_protocol_settles_three_regular_and_bound_carryover(native_protocol, daily_request, tmp_path):
    from pcr_maa.clan_battle_batch import execute_batch
    from pcr_maa.preset_execution import Journal
    native_protocol.state.issue = {1}
    result = execute_batch(native_protocol, Journal(tmp_path / 'ledger'), daily_request,
        now=native_protocol.now(), explicit_start=True)
    assert result.get('error') is None and result['outcome'] == 'complete'
    assert result['regular_settled'] == 3 and result['carryover_settled'] == 1
    assert native_protocol.calls.count('CBStart') == 4
    assert len(list(native_protocol.directory.glob('result-*.json'))) == 4


def test_native_execute_persists_damage_before_return_failure_and_keeps_intent_pending(native_protocol, daily_request, tmp_path):
    from pcr_maa.clan_battle_batch import execute_batch
    from pcr_maa.preset_execution import Journal
    native_protocol.return_failure = True
    journal = Journal(tmp_path / 'ledger')
    result = execute_batch(native_protocol, journal, daily_request, now=native_protocol.now(), explicit_start=True)
    assert result['error'] == 'synthetic_return_failed' and result['consumption_sent'] is True
    saved = json.loads(next(native_protocol.directory.glob('result-*.json')).read_text(encoding='utf-8'))
    assert saved['damage'] == 12345678
    again = execute_batch(native_protocol, journal, daily_request, now=native_protocol.now(), explicit_start=True)
    assert again['error'] == 'daily_previous_result_unknown'
    assert native_protocol.calls.count('CBStart') == 1
