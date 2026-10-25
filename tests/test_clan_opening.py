"""Opening-only control changes use a frozen, verified clan simulation menu."""
from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from pcr_maa import clan_battle, clan_battle_native as backend, native
from pcr_maa.clan_battle_calibration import load_calibration
from pcr_maa.clan_battle_characters import ClanBattleCharacters
from test_clan_battle import Controls, plan


def paused_port(plan):
    port = Controls(plan)
    port.calibration = {'version': 2, 'auto_battle': {'speeds': [1, 2, 4]}}
    port.opening_paused = True
    port.state.update(paused=True, remaining_seconds=90, same_frame=True, zero_damage_verified=True, zero_tp_verified=True)
    return port


@pytest.mark.parametrize('auto', [False, True])
def test_paused_opening_reuses_identity_mapping_and_readback(plan, auto):
    plan['auto'] = auto
    plan['set_by_unit'][str(plan['unit_ids'][0])] = False
    port = paused_port(plan)
    port.state['auto'] = not auto
    evidence = clan_battle.execute_switches(port, plan)
    assert evidence['opening_evidence']['paused'] is True
    assert evidence['opening_evidence']['remaining_seconds'] == 90
    assert evidence['set_by_unit'] == plan['set_by_unit']
    assert ('set', plan['unit_ids'][0], True, False) in port.actions
    assert port.actions[-1][0] == ('auto' if auto else 'set')


@pytest.mark.parametrize('patch', [dict(paused=False), dict(remaining_seconds=88), dict(remaining_seconds=True),
    dict(same_frame=False), dict(battle_speed=None), dict(battle_speed=True),
    dict(zero_damage_verified=False), dict(zero_tp_verified=None)])
def test_pause_flag_cannot_replace_opening_evidence(plan, patch):
    port = paused_port(plan)
    port.state.update(patch)
    with pytest.raises(RuntimeError, match='opening_not_verified'):
        clan_battle.execute_switches(port, plan)
    assert port.actions == []


def test_paused_opening_at_89_requires_zero_damage_and_all_zero_tp(plan):
    port = paused_port(plan)
    port.state['remaining_seconds'] = 89
    plan['set_by_unit'][str(plan['unit_ids'][0])] = False
    evidence = clan_battle.execute_switches(port, plan)
    assert evidence['opening_evidence']['remaining_seconds'] == 89
    assert len(port.actions) == 1


@pytest.mark.parametrize('patch', [dict(paused=False), dict(remaining_seconds=89), dict(battle_speed=2), dict(same_frame=False)])
def test_unknown_after_first_set_stops_without_more_inputs(plan, patch):
    plan['set_by_unit'] = {str(uid): False for uid in plan['unit_ids']}
    port = paused_port(plan)
    toggle = port.toggle_set
    def changed(*args, **kwargs):
        return dict(toggle(*args, **kwargs), **patch)
    port.toggle_set = changed
    with pytest.raises(RuntimeError, match='opening_not_verified'):
        clan_battle.execute_switches(port, plan)
    assert len(port.actions) == 1


@pytest.mark.parametrize('speed', [0, 3, 8, True, 2.0, '4'])
def test_invalid_speed_rejected_before_input(plan, speed):
    with pytest.raises(ValueError, match='speed_invalid'):
        backend.require_battle_speed({}, plan, speed)


def test_acceleration_requires_opening_calibration_and_no_axis(plan):
    profile = {'auto_battle': {'speeds': [1, 2, 4]}}
    backend.require_battle_speed(profile, plan, 4)
    with pytest.raises(RuntimeError, match='auto_speed_uncalibrated'):
        backend.require_battle_speed({}, plan, 4)
    plan['switches'] = [{'remaining_seconds': 60}]
    with pytest.raises(RuntimeError, match='auto_speed_uncalibrated'):
        backend.require_battle_speed(profile, plan, 2)


def sampled_plan():
    return dict(account_id='123456789', server='cn-bilibili', content_version='202610141448',
        period='2026-10', phase='B', round_min=1, round_max=6, boss=5,
        unit_ids=[129701, 134701, 107101, 135501, 134901], stars=[5, 5, 6, 5, 5],
        support_mode='none', support_unit_id=None, auto=True, set_by_unit={}, switches=[])


@pytest.mark.parametrize('sample,auto,sets,speed,second', [
    ('paused-all-on.png', False, [True]*5, 4, 90), ('paused-all-off.png', False, [False]*5, 4, 90),
    ('paused-mixed-1.png', True, [False, True, True, True, True], 1, 90),
    ('paused-mixed-2.png', True, [False, True, True, True, True], 2, 90),
    ('paused-mixed-4.png', True, [False, True, True, True, True], 4, 90),
    ('paused89-all-off.png', False, [False]*5, 1, 89),
    ('paused89-first-on-two.png', True, [True, False, False, False, False], 2, 89)])
def test_real_saved_menu_has_same_frame_team_controls_and_speed(sample, auto, sets, speed, second, monkeypatch):
    from pcr_maa.replay import SavedImages
    plan = sampled_plan()
    profile = load_calibration(plan)
    path = native.ROOT / 'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples' / sample
    frame = native.read_image(path)
    resource = native.load_resource()
    page = backend.ClanBattlePage(plan, profile)
    page.current_page = 'battle'
    characters = ClanBattleCharacters(plan, profile, page)
    assert resource.register_custom_recognition('ClanBattlePage', page)
    assert resource.register_custom_recognition('ClanBattleCharacters', characters)
    assert resource.override_pipeline(backend.calibration_pipeline(profile, plan))
    controller = SavedImages([[frame]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    try:
        receipt = native.run(tasker, 'CBOpeningRead')
        state = backend._detail(receipt)
        assert state['unit_ids'] == plan['unit_ids']
        assert state['sets'] == sets and state['auto'] is auto
        assert state['paused'] is True and state['remaining_seconds'] == second and state['battle_speed'] == speed
        assert state['zero_damage_verified'] and state['zero_tp_verified']
        assert state['same_frame'] and state['frame_pixel_sha256'] == hashlib.sha256(frame.tobytes()).hexdigest()
        assert controller.clicks == [] and controller.swipes == []
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


def test_speed_result_requires_matching_opening_and_resumed_readback(plan):
    port = paused_port(plan)
    evidence = clan_battle.execute_switches(port, plan)
    evidence['battle_speed'] = 4
    evidence['opening_evidence']['battle_speed'] = 4
    evidence['observation']['battle_speed'] = 4
    result = {'settings_evidence': evidence}
    assert clan_battle._settings(result, plan)
    evidence['observation']['battle_speed'] = 1
    assert not clan_battle._settings(result, plan)


@pytest.mark.parametrize('where', ['opening_evidence', 'observation'])
def test_boolean_speed_cannot_pass_integer_one_evidence(plan, where):
    evidence = clan_battle.execute_switches(paused_port(plan), plan)
    evidence['battle_speed'] = 1
    evidence[where]['battle_speed'] = True
    assert not clan_battle._settings({'settings_evidence': evidence}, plan)


def test_actual_two_speed_hud_cannot_match_one_or_four():
    import cv2
    import numpy as np
    profile = load_calibration(sampled_plan())
    frame = native.read_image(native.ROOT/'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples/battle-two.png')
    scores = {}
    for speed, key in ((1, 'speed_one'), (2, 'speed_two'), (4, 'speed_four')):
        marker = profile['pages']['battle'][key]
        x,y,w,h = marker['roi']
        template = cv2.imdecode(np.frombuffer((native.RESOURCE/'image'/marker['template']).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        scores[speed] = float(cv2.minMaxLoc(cv2.matchTemplate(frame[y:y+h,x:x+w], template, cv2.TM_CCOEFF_NORMED))[1])
    assert scores[2] > .99 and scores[1] < .9 and scores[4] < .9


def test_any_filled_tp_pixel_rejects_opening_before_control_input():
    from pcr_maa.replay import SavedImages
    plan = sampled_plan()
    profile = load_calibration(plan)
    frame = native.read_image(native.ROOT/'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples/paused89-first-on-two.png')
    frame[388,365] = [220,180,50]
    resource = native.load_resource()
    page = backend.ClanBattlePage(plan, profile)
    page.current_page = 'battle'
    characters = ClanBattleCharacters(plan, profile, page)
    assert resource.register_custom_recognition('ClanBattlePage', page)
    assert resource.register_custom_recognition('ClanBattleCharacters', characters)
    assert resource.override_pipeline(backend.calibration_pipeline(profile, plan))
    controller = SavedImages([[frame]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    try:
        receipt = native.run(tasker, 'CBOpeningRead')
        assert not receipt['native_ok']
        assert not controller.clicks and not controller.swipes
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


@pytest.mark.parametrize('previous', [True, False])
def test_native_single_role_set_on_and_off_with_real_menu_readback(previous):
    from pcr_maa.replay import SavedImages
    plan = sampled_plan()
    profile = load_calibration(plan)
    directory = native.ROOT/'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples'
    before = [previous, True, True, True, True]
    after = [not previous, True, True, True, True]
    initial = 'paused-all-on-auto-one.png' if previous else 'paused-mixed-1.png'
    final = 'paused-mixed-1.png' if previous else 'paused-all-on-auto-one.png'
    nodes = backend.calibration_pipeline(profile, plan)
    nodes['CBOpeningSet']['custom_recognition_param'] = dict(mode='opening_set', slot=0,
        previous=previous, desired=not previous, unit_ids=plan['unit_ids'], sets=before, auto=True, battle_speed=1)
    nodes['CBOpeningConfirmed']['custom_recognition_param'] = dict(mode='opening_read',
        unit_ids=plan['unit_ids'], sets=after, auto=True, battle_speed=1)
    resource = native.load_resource()
    page = backend.ClanBattlePage(plan, profile)
    page.current_page = 'battle'
    characters = ClanBattleCharacters(plan, profile, page)
    assert resource.register_custom_recognition('ClanBattlePage', page)
    assert resource.register_custom_recognition('ClanBattleCharacters', characters)
    assert resource.override_pipeline(nodes)
    controller = SavedImages([[native.read_image(directory/initial)], [native.read_image(directory/final)]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    try:
        receipt = native.run(tasker, 'CBOpeningSet', reset_hits=('CBOpeningSet',))
        native.require_native_input(receipt, ('CBOpeningSet',))
        state = backend._detail(receipt)
        assert state['unit_ids'] == plan['unit_ids'] and state['sets'] == after
        assert state['auto'] is True and state['battle_speed'] == 1 and state['remaining_seconds'] == 90
        assert state['zero_tp_verified'] and state['zero_damage_verified']
        assert len(controller.clicks) == 1 and not controller.swipes
        x,y = controller.clicks[0]
        assert 403 <= x <= 409 and 311 <= y <= 317
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


def test_native_opening_pause_chain_reads_guards_once_per_frame_then_frozen_team():
    from pcr_maa.replay import SavedImages
    plan = sampled_plan()
    profile = load_calibration(plan)
    directory = native.ROOT / 'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples'
    nodes = backend.calibration_pipeline(profile, plan)
    assert nodes['CBStart']['next'] == ['CBPauseOpening'] and nodes['CBStart']['rate_limit'] == 1
    assert nodes['CBPauseOpening']['recognition'] == 'And' and nodes['CBPauseOpening']['max_hit'] == 1
    resource = native.load_resource()
    page = backend.ClanBattlePage(plan, profile)
    page.current_page = 'battle'
    characters = ClanBattleCharacters(plan, profile, page)
    assert resource.register_custom_recognition('ClanBattlePage', page)
    assert resource.register_custom_recognition('ClanBattleCharacters', characters)
    assert resource.override_pipeline(nodes)
    controller = SavedImages([[native.read_image(directory/'battle-opening.png')],
                              [native.read_image(directory/'paused-all-on.png')]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    try:
        receipt = native.run(tasker, 'CBPauseOpening', reset_hits=('CBPauseOpening',))
        native.require_native_input(receipt, ('CBPauseOpening',))
        state = backend._detail(receipt)
        assert state['paused'] and state['remaining_seconds'] == 90 and state['sets'] == [True]*5
        assert len(controller.clicks) == 1 and not controller.swipes
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


@pytest.mark.parametrize('fading', [False, True])
def test_native_readiness_excludes_intro_even_when_clock_and_menu_templates_match(fading):
    import numpy as np
    from pcr_maa.replay import SavedImages
    plan = sampled_plan()
    nodes = backend.calibration_pipeline(load_calibration(plan), plan)
    path = native.ROOT / 'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples/battle-opening.png'
    frame = native.read_image(path)
    if fading:
        frame = (frame.astype(float)*.6).astype(np.uint8)
    resource = native.load_resource()
    assert resource.override_pipeline({
        'OpeningReadiness': dict(recognition='DirectHit', action='DoNothing', next=['CBOpeningInputReady', 'OpeningNotReady'],
            timeout=1000, pre_delay=0, post_delay=0),
        'OpeningNotReady': dict(recognition='DirectHit', action='DoNothing', next=[], pre_delay=0, post_delay=0),
        'CBOpeningInputReady': nodes['CBOpeningInputReady']})
    controller = SavedImages([[frame]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    try:
        receipt = native.run(tasker, 'OpeningReadiness')
        assert receipt['native_ok'] and receipt['nodes'][-1]['name'] == ('OpeningNotReady' if fading else 'CBOpeningInputReady')
        assert not controller.clicks and not controller.swipes
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']
