"""Same-Owner capture handoff contracts; fake captures never qualify live UB evidence."""
from copy import deepcopy

import pytest

from pcr_maa import clan_battle
from pcr_maa.clan_battle_native import _detail
from test_clan_opening_anchor_owner import owner, TOKEN, UNITS
from test_clan_opening_observation import contract, SCOPE


def ready(owner):
    port = owner.port
    port.plan.update(unit_ids=list(UNITS), set_by_unit={str(uid): True for uid in UNITS}, auto=True)
    port.battle_speed = 2
    start = port.run('CBStart', click=True)
    port.opening_paused = True
    return start, _detail(start)


def test_pause_anchor_does_not_seal_timed_capture_before_owner_finishes(owner):
    start, state = ready(owner)
    timed = owner.reader.opening_timed_observer
    assert timed is owner.port._opening_capture_timed_observer
    assert timed is not owner.observer and timed.sealed is None
    assert timed.samples == 0 and owner.observer.samples == 0
    assert state['first_ub_observation']['coverage'] == 'unknown'
    assert state['first_ub_observation']['not_fired_unit_ids'] == []
    assert 'capture_pending' in state['first_ub_observation']['reasons']
    assert not owner.capture.released
    result = owner.port.finish_opening_capture()
    assert owner.capture.released and timed.sealed is not None
    assert result['anchors']['status'] == 'captured'
    assert result['timed_observation']['coverage'] == 'unknown'
    assert 'capture_timeline_or_marker_unverified' in result['timed_observation']['reasons']
    assert result['timed_observation']['not_fired_unit_ids'] == []
    assert result['first_ub_verified'] is False
    assert _detail(start)['first_ub_observation']['coverage'] == 'unknown'


def test_unqualified_fake_frame_times_never_grant_first_ub_permission(owner):
    _, state = ready(owner)
    timed = owner.reader.opening_timed_observer
    timed.calibration, timed.scope_sha256 = contract(), SCOPE
    refs = owner.observer.anchor_refs
    for index, phase in enumerate(('before_start', 'battle', 'paused')):
        sha = refs[phase]['frame_pixel_sha256']
        packet = dict(source='adb_command_screencap', capture_id=f'synthetic-{index}',
            lower_ns=index*20, upper_ns=index*20+10, frame_pixel_sha256=sha)
        timed.feed(sha, phase, owner_source=timed.owner_source, capture=packet,
            events={str(uid): 'clear' for uid in UNITS})
    assert timed.sealed is None
    result = owner.port.finish_opening_capture()['timed_observation']
    assert result['coverage'] == 'unknown' and result['not_fired_unit_ids'] == []
    assert 'capture_timeline_or_marker_unverified' in result['reasons']
    with pytest.raises(RuntimeError, match='first_ub_unverified'):
        clan_battle.require_first_ub(dict(state, first_ub_observation=result), UNITS)


@pytest.mark.parametrize('failure', ['owner', 'team', 'token', 'start_receipt'])
def test_capture_handoff_rejects_changed_binding(owner, monkeypatch, failure):
    ready(owner)
    timed = owner.reader.opening_timed_observer
    if failure == 'owner':
        monkeypatch.setattr(owner.port, '_opening_capture_source', lambda: (object(), *owner.expected[1:]))
    elif failure == 'team':
        owner.port.plan['unit_ids'] = [1,2,3,4,99]
    elif failure == 'token':
        timed.start_token = 'another-start'
    else:
        owner.port._opening_anchor_start_receipt['task_id'] += 1
    result = owner.port.finish_opening_capture()
    assert result['timed_observation']['coverage'] == 'unknown'
    assert result['timed_observation']['not_fired_unit_ids'] == []
    assert 'owner_capture_binding_unverified' in result['timed_observation']['reasons']


def prepare_change(owner, monkeypatch):
    start, state = ready(owner)
    port = owner.port
    port.plan['set_by_unit'][str(UNITS[0])] = False
    timed = owner.reader.opening_timed_observer
    timed.calibration, timed.scope_sha256 = contract(), SCOPE
    calls = []
    def choose(names):
        assert names == ['CBOpeningRead']
        assert owner.capture.released and timed.sealed is not None
        calls.append('pause_rechecked')
        port.last_receipt = deepcopy(start)
        return 'CBOpeningRead'
    monkeypatch.setattr(port, 'choose', choose)
    return port, start, state, timed, calls


def test_calibrated_change_finishes_capture_then_rechecks_pause(owner, monkeypatch):
    port, start, state, timed, calls = prepare_change(owner, monkeypatch)
    fresh = port.read_battle_settings(start)
    assert calls == ['pause_rechecked'] and owner.capture.released
    assert timed.sealed['coverage'] == 'unknown' and timed.sealed['not_fired_unit_ids'] == []
    assert fresh['remaining_seconds'] == state['remaining_seconds']
    assert fresh['first_ub_observation'] == state['first_ub_observation']
    with pytest.raises(RuntimeError, match='first_ub_unverified'):
        clan_battle.require_first_ub(fresh, [UNITS[0]])


def test_full_match_or_missing_calibration_never_waits_for_capture(owner, monkeypatch):
    port, start, _, timed, calls = prepare_change(owner, monkeypatch)
    port.plan['set_by_unit'][str(UNITS[0])] = True
    assert port.read_battle_settings(start)['paused'] is True
    assert not owner.capture.released and timed.sealed is None and calls == []
    port.plan['set_by_unit'][str(UNITS[0])] = False
    timed.calibration = None
    assert port.read_battle_settings(start)['paused'] is True
    assert not owner.capture.released and timed.sealed is None and calls == []


@pytest.mark.parametrize('incomplete', ['empty', 'scope', 'pause_tail'])
def test_incomplete_calibration_never_waits_for_capture(owner, monkeypatch, incomplete):
    port, start, _, timed, calls = prepare_change(owner, monkeypatch)
    if incomplete == 'empty':
        timed.calibration = {}
    elif incomplete == 'scope':
        timed.scope_sha256 = None
    else:
        timed.calibration['pause_keeps_event_visible'] = False
    assert port.read_battle_settings(start)['paused'] is True
    assert not owner.capture.released and timed.sealed is None and calls == []


def test_mp4_capture_cannot_trigger_png_calibration_wait(owner, monkeypatch):
    from pcr_maa.clan_battle_opening_observation import OpeningMp4Materials
    port, start, _, timed, calls = prepare_change(owner, monkeypatch)
    port._opening_capture = object.__new__(OpeningMp4Materials)
    assert port.read_battle_settings(start)['paused'] is True
    assert not owner.capture.released and timed.sealed is None and calls == []


@pytest.mark.parametrize('change', ['clock', 'control', 'token', 'source', 'receipt'])
def test_no_change_input_if_pause_or_owner_binding_changed(owner, monkeypatch, change):
    port, start, _, _, calls = prepare_change(owner, monkeypatch)
    if change == 'source':
        monkeypatch.setattr(port, '_opening_capture_source', lambda: (object(), *owner.expected[1:]))
    elif change == 'receipt':
        start = deepcopy(start)
        start['task_id'] += 1
    else:
        def choose(names):
            assert names == ['CBOpeningRead'] and owner.capture.released
            fresh = deepcopy(start)
            state = _detail(fresh)
            if change == 'clock': state['remaining_seconds'] -= 1
            elif change == 'control': state['sets'][0] = not state['sets'][0]
            else: state['opening_start_token'] = 'another-start'
            calls.append('pause_rechecked')
            port.last_receipt = fresh
            return 'CBOpeningRead'
        monkeypatch.setattr(port, 'choose', choose)
    with pytest.raises(RuntimeError, match='opening_not_verified|switch_state_changed|capture_binding_changed'):
        port.read_battle_settings(start)
    assert calls == ([] if change in ('source', 'receipt') else ['pause_rechecked'])


def material_rows(tmp_path):
    import hashlib
    import cv2
    import numpy as np
    rows=[]
    for index, (lower,upper) in enumerate(((0,5),(15,20),(35,40))):
        image=np.full((720,1280,3),index*40,np.uint8)
        ok,encoded=cv2.imencode('.png',image);assert ok
        data=encoded.tobytes();name=f'capture-{index:04d}.png'
        (tmp_path/name).write_bytes(data)
        rows.append(dict(source='adb_command_screencap',capture_id=f'fixture-{index}',
            lower_ns=lower,upper_ns=upper,png_sha256=hashlib.sha256(data).hexdigest(),
            physical_size=[1280,720],display_id='0',path=name))
    markers=[dict(phase='dispatch',node='CBStart',battle_start='unknown',host_monotonic_ns=10,
                  meaning='business_call_boundary_not_capture_pts'),
             dict(phase='returned',node='CBStart',battle_start='sent',host_monotonic_ns=30,
                  meaning='business_call_boundary_not_capture_pts')]
    return dict(status='captured',source_bound=True,frames=rows,markers=markers)


def test_png_materials_bind_each_pixel_hash_to_its_command_interval_but_not_ub(tmp_path):
    import hashlib
    import cv2
    from pcr_maa.clan_battle_opening_observation import bind_screencap_materials
    capture=material_rows(tmp_path)
    result=bind_screencap_materials(tmp_path,capture,display_id='0',owner_bound=True)
    assert result['status']=='bound_material_only' and result['coverage']=='unknown'
    assert result['first_ub_verified'] is False and result['not_fired_unit_ids']==[]
    assert result['time_semantics']=='host_command_interval_only' and result['max_gap_ns']==25
    assert [(x['lower_ns'],x['upper_ns']) for x in result['frames']]==[(0,5),(15,20),(35,40)]
    for index,row in enumerate(result['frames']):
        image=cv2.imread(str(tmp_path/f'capture-{index:04d}.png'))
        assert row['frame_pixel_sha256']==hashlib.sha256(image.tobytes()).hexdigest()


@pytest.mark.parametrize('failure', ['owner','display','duplicate','overlap','sha','path','pre','post','marker','mp4','size','format'])
def test_invalid_material_binding_never_becomes_timed_proof(tmp_path, failure):
    from pcr_maa.clan_battle_opening_observation import bind_screencap_materials
    capture=material_rows(tmp_path);kwargs=dict(display_id='0',owner_bound=True)
    if failure=='owner':kwargs['owner_bound']=False
    elif failure=='display':capture['frames'][1]['display_id']='1'
    elif failure=='duplicate':capture['frames'][1]['capture_id']=capture['frames'][0]['capture_id']
    elif failure=='overlap':capture['frames'][1]['lower_ns']=1
    elif failure=='sha':capture['frames'][1]['png_sha256']='0'*64
    elif failure=='path':capture['frames'][1]['path']='../foreign.png'
    elif failure=='pre':capture['frames'][0]['upper_ns']=11
    elif failure=='post':capture['markers'][1]['host_monotonic_ns']=41
    elif failure=='marker':capture['markers'][1]['battle_start']='unknown'
    elif failure=='mp4':capture['format']='mp4'
    elif failure=='size':capture['frames'][1]['physical_size']=[999999999,999999999]
    else:capture['format']='unverified'
    result=bind_screencap_materials(tmp_path,capture,**kwargs)
    assert result['status']=='unknown' and result['frames']==[]
    assert result['coverage']=='unknown' and result['not_fired_unit_ids']==[]
    assert result['first_ub_verified'] is False
