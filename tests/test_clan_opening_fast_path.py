"""Same-frame, no-input opening match using saved images and synthetic Owner bindings."""
import json
from copy import deepcopy
from types import SimpleNamespace as NS

import numpy as np
import pytest

from pcr_maa import clan_battle, clan_battle_native as backend, clan_battle_daily_native as daily, native
from pcr_maa.clan_battle_calibration import load_calibration
from pcr_maa.clan_battle_characters import ClanBattleCharacters
from pcr_maa.replay import SavedImages
from test_clan_formal_opening import formal_reader
from test_clan_battle_daily_native import executable, profile
from test_clan_battle_native import calibration, calibration_v2, plan


ROOT = native.ROOT / '.runtime/clan-open-20261025/live-completion-20261026/opening/live-owner'
HUD90 = ROOT / '97e360f0b759487980eb09c8c9724fa2/anchor-battle.png'
HUD27 = ROOT / 'cb648e5ab5e64913819c5cd7b048734c/anchor-battle.png'
PAUSED = ROOT / 'cb648e5ab5e64913819c5cd7b048734c/anchor-paused.png'
HUD25 = ROOT / 'c4a01ed0ed9e49b5b84c6459658cf9cf/anchor-battle.png'


def d4_plan():
    units = [128401,126501,133501,107001,135401]
    return dict(account_id='123456789',server='cn-bilibili',content_version='202610141448',
        period='2026-10',phase='D',round_min=23,round_max=None,boss=4,
        unit_ids=units,stars=[5]*5,support_mode='none',support_unit_id=None,auto=True,
        set_by_unit={str(uid):True for uid in units},switches=[])


def replay(monkeypatch, plan, *, speed=2, initial=None, second=None, paused=None, fallback=True, direct=False, match_calls=None, match_times=None, legacy_failure=False):
    if not all(path.is_file() for path in (HUD90,HUD27,PAUSED)):
        pytest.skip('private real D4 source frames are not distributed')
    profile = load_calibration(plan)
    resource = native.load_resource()
    page = backend.ClanBattlePage(plan,profile)
    page.current_page = 'formation'
    page.bound = dict(boss=plan['boss'],phase='D',period='2026-10',simulation=True,round=None,
        round_source='selected_difficulty', selected_difficulty=dict(phase='D',boss=plan['boss'],simulation=True))
    characters = ClanBattleCharacters(plan,profile,page)
    monkeypatch.setattr(characters,'formation',lambda context,image:dict(unit_ids=plan['unit_ids']))
    monkeypatch.setattr(page,'require_team',lambda detail:None)
    monkeypatch.setattr(page,'action',lambda context,image,name:[5,5,6,6])
    token = 'synthetic-start-on-real-d4'
    characters.begin_opening(token,owner_source='synthetic-owner')
    nodes=backend.calibration_pipeline(profile,plan)
    if direct:
        # Exercise the legacy recognizer in isolation, not the production Start chain.
        nodes['CBStart']['next'] = ['CBOpeningMatch', 'CBPauseOpening']
    if match_calls is not None or match_times is not None:
        import time
        original_analyze = characters.analyze
        def track_slow_match(context, argv):
            if json.loads(argv.custom_recognition_param).get('mode') == 'opening_match':
                if match_calls is not None:
                    match_calls.append('opening_match')
                    time.sleep(.5)
                    return None
                started = time.monotonic()
                try:
                    return original_analyze(context, argv)
                finally:
                    match_times.append(time.monotonic() - started)
            return original_analyze(context, argv)
        monkeypatch.setattr(characters, 'analyze', track_slow_match)
    nodes['CBStart']['custom_recognition_param']=dict(mode='start',opening_start_token=token)
    nodes['CBOpeningMatch']['custom_recognition_param']=dict(mode='opening_match',opening_start_token=token,
        start_seconds=90,battle_speed=speed)
    nodes['CBStart']['timeout']=3000
    nodes['CBPauseOpening']['timeout']=4000
    if legacy_failure:
        for name in ('CBStart', 'CBPauseOpening', 'CBPauseRetry', 'CBOpeningRead'):
            nodes[name]['on_error'] = []
        nodes['CBPauseRetry']['timeout'] = 1000
        nodes['CBOpeningRead']['timeout'] = 1000
    initial = native.read_image(HUD90) if initial is None else initial
    second=native.read_image(HUD27) if second is None else second
    stages=[[np.zeros((720,1280,3),np.uint8)],[initial,second] if fallback else [initial],
        [native.read_image(PAUSED) if paused is None else paused]]
    assert resource.register_custom_recognition('ClanBattlePage',page)
    assert resource.register_custom_recognition('ClanBattleCharacters',characters)
    assert resource.override_pipeline(nodes)
    controller=SavedImages(stages)
    tasker=native.bind(resource,controller);tasker._resource_holder=resource
    try:
        receipt=native.run(tasker,'CBStart',reset_hits=('CBStart','CBPauseOpening','CBPauseRetry'))
        return receipt, controller.clicks[:]
    finally:
        stopped=native.stop_tasker(tasker);assert stopped['idle'] and stopped['stop_completed']


@pytest.mark.private_replay
def test_real_d4_hud_direct_match_has_only_start_input(monkeypatch):
    selected=d4_plan()
    receipt,clicks=replay(monkeypatch,selected,fallback=False,direct=True)
    native.require_native_input(receipt,('CBStart',))
    assert [row['name'] for row in receipt['nodes']] == ['CBStart','CBOpeningMatch']
    assert len(clicks)==1
    state=backend._detail(receipt)
    assert state['opening_direct'] is True and state['paused'] is False and state['same_frame'] is True
    assert state['remaining_seconds']==90 and state['battle_speed']==2 and state['auto'] is True
    assert dict(zip(state['unit_ids'],state['sets']))=={uid:True for uid in selected['unit_ids']}
    assert 'first_ub_evidence' not in state and 'first_ub_observation' not in state


@pytest.mark.parametrize('difference',['auto','set','speed','unknown_speed'])
@pytest.mark.private_replay
def test_real_hud_difference_falls_back_to_pause_without_live_settings_inputs(monkeypatch,difference):
    if not all(path.is_file() for path in (HUD90, HUD27, PAUSED)):
        pytest.skip('private real D4 source frames are not distributed')
    selected=d4_plan(); speed=2; initial=None
    if difference=='auto':selected['auto']=False
    elif difference=='set':selected['set_by_unit'][str(selected['unit_ids'][0])]=False
    elif difference=='speed':speed=1
    elif difference=='unknown_speed':
        initial=native.read_image(HUD90);initial[617:710,1175:1270]=0
    receipt,clicks=replay(monkeypatch,selected,speed=speed,initial=initial)
    native.require_native_input(receipt,('CBStart','CBPauseOpening'))
    names=[row['name'] for row in receipt['nodes']]
    assert 'CBOpeningMatch' not in names and names[-1]=='CBOpeningRead'
    assert len(clicks)==2 and backend._detail(receipt)['paused'] is True
    assert not {'CBOpeningSet','CBOpeningAuto','CBOpeningSpeed','CBResumeOpening'} & set(names)


def test_formal_match_rechecks_reservation_and_keeps_ub_unknown(formal_reader,monkeypatch):
    port,reader,context,image,_,_=formal_reader
    token=port.pending['intent_id'];reader.begin_opening(token);reader.opening_started=True
    selected=port.plan
    state=dict(unit_ids=selected['unit_ids'],sets=[selected['set_by_unit'][str(uid)] for uid in selected['unit_ids']],
        auto=selected['auto'],battle_speed=2,remaining_seconds=34,same_frame=True,frame_pixel_sha256='a'*64)
    monkeypatch.setattr(reader,'battle_control',lambda *args:reader.AnalyzeResult(box=[0,0,1,1],detail=deepcopy(state)))
    params=dict(mode='opening_match',opening_start_token=token,start_seconds=34,battle_speed=2)
    argv=NS(image=image,custom_recognition_param=json.dumps(params))
    result=reader.analyze(context,argv)
    assert result is not None and result.detail['opening_direct'] is True
    assert not {'first_ub_evidence','first_ub_observation'} & set(result.detail)
    for patch in (dict(opening_start_token='wrong'),dict(start_seconds=90),dict(battle_speed=1),dict(battle_speed=True)):
        argv.custom_recognition_param=json.dumps(dict(params,**patch))
        assert reader.analyze(context,argv) is None
    argv.custom_recognition_param=json.dumps(params)
    context.tasker.stopping=True
    assert reader.analyze(context,argv) is None
    context.tasker.stopping=False
    port.pending['action']['instance_id']='changed'
    assert reader.analyze(context,argv) is None


def test_formal_direct_match_skips_resume_and_keeps_speed_two(plan):
    state=dict(unit_ids=plan['unit_ids'],sets=[plan['set_by_unit'][str(uid)] for uid in plan['unit_ids']],
        auto=plan['auto'],battle_speed=2,remaining_seconds=34,same_frame=True,paused=False,
        opening_direct=True,opening_start_token='intent',frame_pixel_sha256='a'*64)
    port=NS(calibration={'version':2,'daily':{'execution':{'opening':{'battle_speed':2}}}},
        action={'kind':'regular'},
        formal_opening=True,opening_paused=False,battle_speed=2,start_seconds=34,
        read_battle_settings=lambda initial:deepcopy(state),
        tasker=NS(resource=NS(override_pipeline=lambda _:pytest.fail('no setting input expected'))))
    settings=daily.NativeClanDailyAdapter.configure_opening(port,plan,{'synthetic':'receipt'})
    assert settings['actions']==[] and settings['battle_speed']==2
    assert clan_battle._settings({'settings_evidence':settings},plan)
    state['battle_speed']=1
    with pytest.raises(RuntimeError,match='opening_not_verified'):
        daily.NativeClanDailyAdapter.configure_opening(port,plan,{'synthetic':'receipt'})


def test_paused_dialog_with_initial_clock_never_matches_running_fast_path():
    if not HUD90.is_file() or not PAUSED.is_file():
        pytest.skip('private real D4 source frames are not distributed')
    selected=d4_plan();profile=load_calibration(selected)
    frame=native.read_image(PAUSED)
    frame[21:47,1080:1123]=native.read_image(HUD90)[21:47,1080:1123]
    page=backend.ClanBattlePage(selected,profile);page.current_page='battle'
    page.bound=dict(boss=4,phase='D',period='2026-10',simulation=True,round=None,
        round_source='selected_difficulty',selected_difficulty=dict(phase='D',boss=4,simulation=True))
    characters=ClanBattleCharacters(selected,profile,page)
    characters.begin_opening('synthetic');characters.opening_started=True
    resource=native.load_resource()
    assert resource.register_custom_recognition('ClanBattlePage',page)
    assert resource.register_custom_recognition('ClanBattleCharacters',characters)
    nodes=backend.calibration_pipeline(profile,selected)
    nodes['CBOpeningMatch'].update(timeout=1500,custom_recognition_param=dict(mode='opening_match',
        opening_start_token='synthetic',start_seconds=90,battle_speed=2))
    assert resource.override_pipeline(nodes)
    controller=SavedImages([[frame]])
    tasker=native.bind(resource,controller);tasker._resource_holder=resource
    try:
        receipt=native.run(tasker,'CBOpeningMatch')
        assert not receipt['native_ok'] and not controller.clicks
    finally:
        stopped=native.stop_tasker(tasker);assert stopped['idle'] and stopped['stop_completed']


@pytest.mark.private_replay
def test_simulation_direct_match_flows_to_result_without_menu(monkeypatch,tmp_path):
    from test_clan_battle_native import result_page_receipt
    selected=d4_plan()
    start,clicks=replay(monkeypatch,selected,fallback=False,direct=True)
    port=backend.NativeClanBattle(NS(),tmp_path,calibration=load_calibration(selected),battle_speed=2)
    port.plan=selected
    calls=[]
    monkeypatch.setattr(port,'verify_team',lambda _:dict(native_evidence={'synthetic':True}))
    def arm(token, *, start_seconds, action_kind):
        assert type(start_seconds) is int and start_seconds == 90 and action_kind == 'simulation'
        calls.append('arm')
    monkeypatch.setattr(port,'arm_opening_start',arm)
    monkeypatch.setattr(port,'run',lambda node,**kw:calls.append(node) or start)
    def wait(names,*,seconds):
        calls.append(names)
        port.last_receipt=result_page_receipt(dict(period=selected['period'],phase='D',boss=4,simulation=True,
            round=None,round_source='selected_difficulty',selected_difficulty=dict(phase='D',boss=4,simulation=True),damage=123))
        return names[0]
    monkeypatch.setattr(port,'wait',wait)
    monkeypatch.setattr(port,'checkpoint',lambda:pytest.fail('no setting input expected'))
    result=port._start_and_read_bound(selected)
    assert len(clicks)==1 and calls==['arm','CBStart',['CBPage_result']]
    assert result['settings_evidence']['actions']==[] and result['settings_evidence']['battle_speed']==2
    assert clan_battle._settings(result,selected)
    assert result['damage']==123 and result['result_verified'] is True


@pytest.mark.private_replay
def test_fallback_requires_first_ub_evidence_before_difference_toggle(monkeypatch):
    selected=d4_plan();selected['set_by_unit'][str(selected['unit_ids'][0])]=False
    receipt,clicks=replay(monkeypatch,selected)
    state=backend._detail(receipt)
    port=NS(calibration=load_calibration(selected),opening_paused=True,battle_speed=2,start_seconds=90,
        read_battle_settings=lambda _:deepcopy(state),
        checkpoint=lambda:pytest.fail('unknown first UB must prevent toggle'),
        toggle_set=lambda *a,**kw:pytest.fail('no input while first UB is unknown'))
    with pytest.raises(RuntimeError,match='first_ub_unverified'):
        clan_battle.execute_switches(port,selected,initial=receipt)
    assert len(clicks)==2


def test_real_d4_125_hud_direct_match_has_only_start_input(monkeypatch):
    if not HUD25.is_file():
        pytest.skip('private real D4 1:25 source frame is not distributed')
    selected=d4_plan()
    receipt,clicks=replay(monkeypatch,selected,initial=native.read_image(HUD25),fallback=False,direct=True)
    native.require_native_input(receipt,('CBStart',))
    assert [row['name'] for row in receipt['nodes']]==['CBStart','CBOpeningMatch']
    state=backend._detail(receipt)
    assert len(clicks)==1 and state['remaining_seconds']==85 and state['battle_speed']==2
    assert state['auto'] is True and all(state['sets']) and state['same_frame'] is True
    assert set(state['unit_ids'])==set(selected['unit_ids'])
    assert state['opening_direct'] is True and state['paused'] is False
    assert not {'first_ub_observation','first_ub_evidence'} & set(state)


@pytest.mark.parametrize('second,valid', [(0,False),(1,True),(34,True),(85,True),(90,True),(91,False),(True,False)])
def test_unpaused_fast_path_accepts_only_positive_remaining_seconds(plan,second,valid):
    state=dict(unit_ids=plan['unit_ids'],sets=[plan['set_by_unit'][str(uid)] for uid in plan['unit_ids']],
        auto=plan['auto'],battle_speed=2,remaining_seconds=second,same_frame=True,paused=False,
        opening_direct=True,opening_start_token='intent',frame_pixel_sha256='a'*64)
    port=NS(calibration={'version':2,'auto_battle':{'speeds':[1,2,4]}},opening_paused=False,
        battle_speed=2,start_seconds=90,read_battle_settings=lambda _:deepcopy(state),
        checkpoint=lambda:pytest.fail('no setting input expected'))
    if valid:
        result=clan_battle.execute_switches(port,plan,initial={'synthetic':'receipt'})
        assert result['actions']==[] and result['observation']['remaining_seconds']==second
    else:
        with pytest.raises(RuntimeError,match='opening_not_verified'):
            clan_battle.execute_switches(port,plan,initial={'synthetic':'receipt'})


def test_other_unpaused_path_keeps_original_start_seconds_rule(plan):
    state=dict(unit_ids=plan['unit_ids'],sets=[plan['set_by_unit'][str(uid)] for uid in plan['unit_ids']],
        auto=plan['auto'],battle_speed=2,remaining_seconds=85,same_frame=True,paused=False,
        frame_pixel_sha256='a'*64)
    port=NS(calibration={'version':2},opening_paused=False,battle_speed=2,start_seconds=90,
        read_battle_settings=lambda _:deepcopy(state))
    with pytest.raises(RuntimeError,match='opening_not_verified'):
        clan_battle.execute_switches(port,plan,initial={'synthetic':'receipt'})


@pytest.mark.parametrize('difference',['auto','set','speed','unknown_speed'])
def test_observed_difference_cannot_be_erased_by_later_match(difference,monkeypatch):
    selected=d4_plan();reader=object.__new__(ClanBattleCharacters)
    reader.team=selected['unit_ids'];reader.plan=selected
    reader.begin_opening('one');reader.opening_started=True
    state=dict(unit_ids=selected['unit_ids'],sets=[True]*5,auto=True,battle_speed=2,
        remaining_seconds=85,same_frame=True,frame_pixel_sha256='a'*64)
    if difference=='auto':state['auto']=False
    elif difference=='set':state['sets'][0]=False
    elif difference=='speed':state['battle_speed']=1
    else:state['battle_speed']=None
    def read(*_):return reader.AnalyzeResult(box=[0,0,1,1],detail=deepcopy(state))
    monkeypatch.setattr(reader,'battle_control',read)
    context=NS(tasker=NS(stopping=False))
    argv=NS(image=np.zeros((720,1280,3),np.uint8),custom_recognition_param=json.dumps(
        dict(mode='opening_match',opening_start_token='one',start_seconds=90,battle_speed=2)))
    assert reader.analyze(context,argv) is None and reader.opening_match_fallback is True
    state.update(sets=[True]*5,auto=True,battle_speed=2)
    assert reader.analyze(context,argv) is None
    reader.begin_opening('two');reader.opening_started=True
    argv.custom_recognition_param=json.dumps(dict(mode='opening_match',opening_start_token='two',start_seconds=90,battle_speed=2))
    assert reader.analyze(context,argv).detail['opening_direct'] is True


def test_unreadable_loading_does_not_block_later_valid_hud(monkeypatch):
    selected=d4_plan();reader=object.__new__(ClanBattleCharacters)
    reader.team=selected['unit_ids'];reader.plan=selected
    reader.begin_opening('one');reader.opening_started=True
    state=dict(unit_ids=selected['unit_ids'],sets=[True]*5,auto=True,battle_speed=2,
        remaining_seconds=85,same_frame=True,frame_pixel_sha256='a'*64)
    samples=iter([None,reader.AnalyzeResult(box=[0,0,1,1],detail=state)])
    monkeypatch.setattr(reader,'battle_control',lambda *_:next(samples))
    context=NS(tasker=NS(stopping=False))
    argv=NS(image=np.zeros((720,1280,3),np.uint8),custom_recognition_param=json.dumps(
        dict(mode='opening_match',opening_start_token='one',start_seconds=90,battle_speed=2)))
    assert reader.analyze(context,argv) is None and reader.opening_match_fallback is False
    assert reader.analyze(context,argv).detail['opening_direct'] is True


@pytest.mark.parametrize('second,expected',[(0,False),(1,True),(34,True),(35,False),(90,False)])
def test_carry_fast_path_respects_reserved_start_seconds(formal_reader,monkeypatch,second,expected):
    port,reader,context,image,_,_=formal_reader
    token=port.pending['intent_id'];reader.begin_opening(token);reader.opening_started=True
    selected=port.plan
    state=dict(unit_ids=selected['unit_ids'],sets=[selected['set_by_unit'][str(uid)] for uid in selected['unit_ids']],
        auto=selected['auto'],battle_speed=2,remaining_seconds=second,same_frame=True,frame_pixel_sha256='a'*64)
    reader.calibration={'version':2}
    reader.page=NS(port=port,read=lambda *_:dict(remaining_seconds=second,battle_speed=2))
    monkeypatch.setattr(reader,'read_controls',lambda _:deepcopy(state))
    argv=NS(image=image,custom_recognition_param=json.dumps(dict(mode='opening_match',
        opening_start_token=token,start_seconds=34,battle_speed=2)))
    result=reader.analyze(context,argv)
    assert (result is not None) is expected


D3_HUD28 = ROOT / 'bb8ca30d3569437c88d45450006efa7c/anchor-battle.png'


@pytest.mark.parametrize('intro', [False, True])
def test_start_pauses_on_real_d3_hud_without_expensive_running_match(monkeypatch, intro):
    if not D3_HUD28.is_file():
        pytest.skip('private real D3 1:28 source frame is not distributed')
    calls = []
    # The first sample is a real D4 1:30 intro when requested; the next is the
    # D3 1:28 frame on which the previous slow match delayed the actual pause.
    initial = native.read_image(HUD90 if intro else D3_HUD28)
    second = native.read_image(D3_HUD28)
    receipt, clicks = replay(monkeypatch, d4_plan(), initial=initial, second=second,
        fallback=intro, match_calls=calls)
    native.require_native_input(receipt, ('CBStart', 'CBPauseOpening'))
    names = [row['name'] for row in receipt['nodes']]
    assert names == ['CBStart', 'CBPauseOpening', 'CBOpeningRead']
    assert len(clicks) == 2 and calls == []
    recognition = receipt['nodes'][1]['recognition']
    clock = next(row for row in recognition if row['name'] == 'CBOpeningReadyClock')
    assert clock['detail']['best']['text'] == '1:28'
    assert backend._detail(receipt)['paused'] is True


def test_old_start_order_replays_actual_d3_late_paused_failure(monkeypatch):
    late = native.ROOT / '.runtime/supported-daily-work/run-cc52d4a55bdc4adfab660d0616e6cac0/219-CBStart.png'
    if not D3_HUD28.is_file() or not late.is_file():
        pytest.skip('private real D3 battle and delayed pause source frames are not distributed')
    selected = d4_plan()
    selected['boss'] = 3
    selected['unit_ids'] = [135001, 134001, 130701, 130801, 124101]
    selected['set_by_unit'] = {str(uid): True for uid in selected['unit_ids']}
    timings = []
    # Exercise the old chain only to reproduce its failure, using the original
    # same-frame running callback and the later real 1:04 paused screenshot.
    receipt, clicks = replay(monkeypatch, selected, initial=native.read_image(D3_HUD28),
        paused=native.read_image(late), fallback=False, direct=True, match_times=timings, legacy_failure=True)
    assert not receipt['native_ok']
    assert timings and all(value > 0 for value in timings)
    assert len(clicks) == 2
    assert [row['name'] for row in receipt['nodes']][:2] == ['CBStart', 'CBPauseOpening']
    assert not any(row['name'] in ('CBOpeningRead', 'CBPauseRetry') for row in receipt['nodes'])
    print('actual_old_opening_match_elapsed_seconds=', timings)


@pytest.mark.parametrize('capability,start,clock', [('formal', 90, '1:29'), ('carryover', 34, '0:33')])
@pytest.mark.parametrize('ready', [True, False])
def test_formal_and_carry_start_chain_pauses_only_with_valid_binding(
        formal_reader, executable, monkeypatch, capability, start, clock, ready):
    from pcr_maa import clan_battle_calibration
    port, _, _, image, _, _ = formal_reader
    plan = port.plan
    port.action['start_seconds'] = start
    port.pending['action']['start_seconds'] = start
    port.start_seconds = start
    port.calibrations = {plan['confirmed_for']: executable}
    resource = native.load_resource()
    port.tasker = NS(resource=resource)
    monkeypatch.setattr(clan_battle, 'load_snapshot', lambda _: dict(ownership_complete=True,
        units=[dict(unit_id=uid) for uid in plan['unit_ids']]))
    monkeypatch.setattr(clan_battle_calibration, 'load_calibration', lambda *args, **kwargs: executable)
    port.configure_slot(plan, capability=capability)
    reader = port.characters
    token = port.pending['intent_id']
    reader.begin_opening(token)
    monkeypatch.setattr(reader, 'formation', lambda context, frame: dict(unit_ids=plan['unit_ids']))
    monkeypatch.setattr(port.page, 'require_team', lambda detail: None)
    monkeypatch.setattr(port.page, 'action', lambda context, frame, name: [5, 5, 6, 6])
    monkeypatch.setattr(port.page, 'word', lambda context, frame, node, **opts: clock if ready else 'unknown')
    calls = []
    original = reader.analyze
    def record_match(context, argv):
        if json.loads(argv.custom_recognition_param).get('mode') == 'opening_match':
            calls.append('opening_match')
        return original(context, argv)
    monkeypatch.setattr(reader, 'analyze', record_match)
    assert resource.register_custom_recognition('ClanBattleCharacters', reader)
    # Formal/compensation pages are synthetic; only the real callback binding
    # and production Start ordering are under test, not formal visual calibration.
    direct = dict(recognition='DirectHit', action='DoNothing', next=[], pre_delay=0, post_delay=0)
    override = {f'CBFormalOpening_opening_guards_{i}': direct for i in range(3)}
    override.update(CBDailyOpeningStartClock=direct, CBOpeningRead=direct,
        CBStart=dict(custom_recognition_param=dict(mode='start', opening_start_token=token), timeout=2500, on_error=[]),
        CBDailyPauseOpening=dict(timeout=1200, on_error=[]))
    assert resource.override_pipeline(override)
    controller = SavedImages([[image], [image], [image]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    try:
        receipt = native.run(tasker, 'CBStart', reset_hits=('CBStart', 'CBDailyPauseOpening'))
        assert not calls
        if ready:
            native.require_native_input(receipt, ('CBStart', 'CBDailyPauseOpening'))
            assert [row['name'] for row in receipt['nodes']] == ['CBStart', 'CBDailyPauseOpening', 'CBOpeningRead']
            assert len(controller.clicks) == 2
        else:
            assert not receipt['native_ok'] and len(controller.clicks) == 1
            assert not any(row['name'] == 'CBDailyPauseOpening' for row in receipt['nodes'])
        assert not controller.swipes
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


@pytest.mark.private_replay
def test_real_matching_paused_controls_resume_without_settings_changes(monkeypatch, tmp_path):
    from test_clan_battle_native import result_page_receipt
    selected = d4_plan()
    if not all(path.is_file() for path in (HUD90, HUD27, PAUSED)):
        pytest.skip('private real D4 source frames are not distributed')
    start, clicks = replay(monkeypatch, selected, initial=native.read_image(HUD27), fallback=False)
    native.require_native_input(start, ('CBStart', 'CBPauseOpening'))
    state = backend._detail(start)
    assert state['paused'] and state['auto'] and state['battle_speed'] == 2 and all(state['sets'])
    calls = []
    updates = []
    resource = NS(override_pipeline=lambda update: updates.append(update) or True)
    port = backend.NativeClanBattle(NS(resource=resource), tmp_path,
        calibration=load_calibration(selected), battle_speed=2)
    port.plan = selected
    monkeypatch.setattr(port, 'verify_team', lambda _: dict(native_evidence={'synthetic': True}))
    def arm(token, *, start_seconds, action_kind):
        assert type(start_seconds) is int and start_seconds == 90 and action_kind == 'simulation'
        calls.append('arm')
    monkeypatch.setattr(port, 'arm_opening_start', arm)
    monkeypatch.setattr(port, 'run', lambda node, **kw: calls.append(node) or start)
    monkeypatch.setattr(port, 'read_battle_settings', lambda _: deepcopy(state))
    def wait(names, *, seconds):
        calls.append(names)
        if names == ['CBPage_result']:
            port.last_receipt = result_page_receipt(dict(period=selected['period'], phase='D',
                boss=4, simulation=True, round=None, round_source='selected_difficulty',
                selected_difficulty=dict(phase='D', boss=4, simulation=True), damage=123))
        else:
            port.last_receipt = {'synthetic': 'resume readback'}
        return names[0]
    monkeypatch.setattr(port, 'wait', wait)
    monkeypatch.setattr(port, 'checkpoint', lambda: pytest.fail('no setting change is needed'))
    result = port._start_and_read_bound(selected)
    assert len(clicks) == 2 and calls == ['arm', 'CBStart', 'CBResumeOpening',
        ['CBSettingsConfirmed'], ['CBPage_result']]
    assert result['settings_evidence']['actions'] == []
    assert len(updates) == 1 and set(updates[0]) == {'CBResumeOpening', 'CBSettingsConfirmed'}
    assert result['damage'] == 123 and result['result_verified'] is True
