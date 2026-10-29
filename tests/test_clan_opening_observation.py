"""Synthetic coverage/capture contracts; none of these fixtures qualify a live device."""
from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

from pcr_maa import clan_battle
from pcr_maa import clan_battle_opening_observation as observation
from pcr_maa.clan_battle_characters import ClanBattleCharacters

UNITS = [1,2,3,4,5]
OWNER = object()
SCOPE = 'c'*64


def contract():
    return dict(source='adb_command_screencap', capture_calibration_sha256='a'*64,
        marker_calibration_sha256='b'*64, scope_sha256=SCOPE, min_visible_ns=100,
        unit_ids=UNITS, speeds=[1,2], pause_keeps_event_visible=True)


def trace(*, calibration=None, changes=None):
    calibration = contract() if calibration is None else calibration
    observer=observation.OpeningObservation.begin('start',UNITS,owner_source=OWNER,
        scope_sha256=SCOPE,calibration=calibration)
    changes=changes or {}
    for index,phase in enumerate(['before_start','battle','paused']):
        frame=f'{index}'*64
        values=dict(frame_sha256=frame,phase=phase,owner_source=OWNER,
            capture=dict(source='adb_command_screencap',capture_id=f'fixture-call-{index}',
                lower_ns=20*index,upper_ns=20*index+10,frame_pixel_sha256=frame),
            events={str(uid):'clear' for uid in UNITS})
        values.update(changes.get(index,{}))
        observer.feed(**values)
    return observer


def seal(observer):
    return observer.seal('start','2'*64,owner_source=OWNER,battle_speed=2)


def test_complete_intervals_and_calibrated_visibility_prove_only_the_observed_roles():
    result=seal(trace())
    assert result['coverage']=='complete' and result['not_fired_unit_ids']==UNITS
    assert result['max_gap_ns']==30 and result['unknown_unit_ids']==[]
    state=dict(opening_start_token='start',first_ub_observation=result)
    clan_battle.require_first_ub(state,UNITS)


@pytest.mark.parametrize('change,reason', [
    ({'capture':None},'capture_interval_unknown'),
    ({'owner_source':object()},'owner_source_changed'),
    ({'phase':'paused'},'phase_order_unknown'),
])
def test_unknown_capture_owner_or_sequence_never_becomes_not_fired(change,reason):
    result=seal(trace(changes={1:change}))
    assert result['coverage']=='unknown' and result['not_fired_unit_ids']==[]
    assert reason in result['reasons']


@pytest.mark.parametrize('upper', [100,110,10000])
def test_worst_case_capture_gap_includes_uncertainty_and_rejects_equal_visibility_window(upper):
    capture=dict(source='adb_command_screencap',capture_id='late',lower_ns=upper-10,
        upper_ns=upper,frame_pixel_sha256='1'*64)
    result=seal(trace(changes={1:{'capture':capture}}))
    assert result['coverage']=='unknown' and result['not_fired_unit_ids']==[]


def test_reused_capture_id_and_reordered_interval_are_not_new_renderer_frames():
    for capture in (dict(source='adb_command_screencap',capture_id='fixture-call-0',lower_ns=20,
        upper_ns=30,frame_pixel_sha256='1'*64),
        dict(source='adb_command_screencap',capture_id='ordered-unknown',lower_ns=5,
            upper_ns=15,frame_pixel_sha256='1'*64)):
        result=seal(trace(changes={1:{'capture':capture}}))
        assert result['coverage']=='unknown' and result['not_fired_unit_ids']==[]


@pytest.mark.parametrize('field,value', [('min_visible_ns',True),('capture_calibration_sha256','bad'),
    ('marker_calibration_sha256',None),('scope_sha256','d'*64),('unit_ids',[1,2,3,4,True]),('speeds',[True])])
def test_missing_or_wrong_calibration_scope_keeps_real_history_unknown(field,value):
    calibration=contract();calibration[field]=value
    result=seal(trace(calibration=calibration))
    assert result['coverage']=='unknown' and result['not_fired_unit_ids']==[]


def test_pause_can_hide_a_new_ub_and_requires_its_own_tail_visibility_evidence():
    calibration=contract();calibration['pause_keeps_event_visible']=False
    result=seal(trace(calibration=calibration))
    assert 'pause_tail_unknown' in result['reasons'] and result['not_fired_unit_ids']==[]


def test_witnessed_ub_blocks_that_role_even_if_an_older_causal_proof_claims_it_safe():
    events={str(uid):'clear' for uid in UNITS};events['1']='ub_started'
    result=seal(trace(changes={1:{'events':events}}))
    assert result['fired_unit_ids']==[1] and result['not_fired_unit_ids']==[2,3,4,5]
    state=dict(opening_start_token='start',first_ub_observation=result)
    clan_battle.require_first_ub(state,[2])
    with pytest.raises(RuntimeError,match='first_ub_already_fired'):
        clan_battle.require_first_ub(state,[1])


def test_unknown_role_visibility_does_not_disqualify_other_visible_roles():
    events={str(uid):'clear' for uid in UNITS};events['1']='unknown'
    result=seal(trace(changes={1:{'events':events}}))
    assert result['not_fired_unit_ids']==[2,3,4,5] and result['unknown_unit_ids']==[1]
    with pytest.raises(RuntimeError,match='first_ub_unverified'):
        clan_battle.require_first_ub(dict(opening_start_token='start',first_ub_observation=result),[1])


def test_sealed_history_cannot_survive_owner_invalidation_or_a_different_start():
    observer=trace();assert seal(observer)['coverage']=='complete'
    observer.invalidate('owner_binding_invalidated')
    assert seal(observer)['not_fired_unit_ids']==[]
    observer=trace();assert seal(observer)['coverage']=='complete'
    assert observer.seal('another','2'*64,owner_source=OWNER,battle_speed=2)['not_fired_unit_ids']==[]


def test_callback_pixels_without_capture_metadata_never_get_fabricated_time_or_sequence():
    reader=object.__new__(ClanBattleCharacters);reader.team=UNITS
    reader.begin_opening('start',owner_source=OWNER)
    image=np.zeros((720,1280,3),np.uint8)
    reader.record_opening_frame(image,'before_start');reader.opening_started=True
    reader.record_opening_frame(image,'battle')
    frame_hash=hashlib.sha256(image.tobytes()).hexdigest()
    state=dict(unit_ids=UNITS,sets=[True]*5,auto=True,battle_speed=2,remaining_seconds=88,
        frame_pixel_sha256=frame_hash)
    result=reader.bind_paused_opening(state,'opening_read')
    report=result['first_ub_observation']
    assert report['coverage']=='unknown' and report['not_fired_unit_ids']==[]
    assert 'capture_interval_unknown' in report['reasons'] and 'first_ub_evidence' not in result
    assert report['max_gap_ns']==0


def png():
    return cv2.imencode('.png',np.zeros((720,1280,3),np.uint8))[1].tobytes()


@pytest.fixture
def adb(tmp_path):
    value=tmp_path/'synthetic-adb.exe';value.write_bytes(b'fixture-never-executed');return value


def test_fresh_command_stdout_is_bound_to_its_actual_call_interval_not_cached_image(adb,monkeypatch):
    data=png();commands=[]
    class Process:
        pid=4321;returncode=0
        def __init__(self,command,**options):commands.append((command,options))
        def communicate(self,timeout):return data,b''
    times=iter([100,300]);monkeypatch.setattr(observation.time,'monotonic_ns',lambda:next(times))
    monkeypatch.setattr(observation.subprocess,'Popen',Process)
    raw,packet=observation.capture_adb_png(adb,'127.0.0.1:1','0',timeout=1)
    assert raw==data and packet['capture_id']=='4321:100'
    assert packet['lower_ns']==100 and packet['upper_ns']==300
    assert commands[0][0]==[str(adb),'-s','127.0.0.1:1','exec-out','screencap','-p','-d','0']
    image,decoded=observation.decode_capture(raw,packet)
    assert decoded['frame_pixel_sha256']==hashlib.sha256(image.tobytes()).hexdigest()


@pytest.mark.parametrize('failure',['exit','crc','truncated','timeout'])
def test_capture_failure_does_not_retry_and_timeout_releases_its_child(adb,monkeypatch,failure):
    data=png();calls=[]
    class Process:
        pid=4321;returncode=1 if failure=='exit' else 0
        def __init__(self,*args,**options):calls.append('start')
        def communicate(self,timeout):
            if failure=='timeout' and 'kill' not in calls:raise subprocess.TimeoutExpired('fixture',timeout)
            value=data[:-3] if failure=='truncated' else data
            if failure=='crc':value=value[:45]+bytes([value[45]^1])+value[46:]
            return value,b''
        def kill(self):calls.append('kill')
    monkeypatch.setattr(observation.subprocess,'Popen',Process)
    with pytest.raises((RuntimeError,subprocess.TimeoutExpired)):
        observation.capture_adb_png(adb,'serial','0',timeout=1)
    assert calls.count('start')==1
    if failure=='timeout':assert calls==['start','kill']


def test_capture_only_worker_never_calls_owner_from_its_thread_and_joins(tmp_path,monkeypatch):
    main=threading.get_ident();threads=[];data=png()
    def capture(*args,**options):
        threads.append(threading.get_ident())
        return data,dict(source='adb_command_screencap',capture_id=str(len(threads)),
            lower_ns=0,upper_ns=1,png_sha256=hashlib.sha256(data).hexdigest(),physical_size=[1280,720])
    monkeypatch.setattr(observation,'capture_adb_png',capture)
    worker=observation.OpeningCaptureMaterials('unused','serial','0',tmp_path/'material',max_frames=2).start()
    worker.thread.join(timeout=2)
    result=worker.finish(source_valid=True,battle_start='sent')
    assert len(result['frames'])==2 and all(thread!=main for thread in threads)
    assert not worker.thread.is_alive() and not result['first_ub_verified']
    assert result['purpose']=='capture_only_not_ub_acceptance'
    assert (tmp_path/'material/capture.json').is_file()


def test_source_change_invalidates_the_whole_capture_and_failed_worker_is_reaped(tmp_path,monkeypatch):
    def failure(*args,**options):raise RuntimeError('fixture-capture-failed')
    monkeypatch.setattr(observation,'capture_adb_png',failure)
    worker=observation.OpeningCaptureMaterials('unused','serial','0',tmp_path/'material')
    with pytest.raises(RuntimeError,match='first_frame_missing'):
        worker.start()
    result=worker.finish(source_valid=False,battle_start='unknown')
    assert not worker.thread.is_alive() and result['status']=='unknown'
    assert result['source_bound'] is False and result['first_ub_verified'] is False


def test_capture_start_waits_for_the_first_frame_before_business_may_send_start(tmp_path,monkeypatch):
    capture_entered=threading.Event();allow_frame=threading.Event();data=png()
    def capture(*args,**options):
        capture_entered.set();assert allow_frame.wait(timeout=2)
        return data,dict(source='adb_command_screencap',capture_id='fixture',lower_ns=0,upper_ns=1,
            png_sha256=hashlib.sha256(data).hexdigest(),physical_size=[1280,720])
    monkeypatch.setattr(observation,'capture_adb_png',capture)
    worker=observation.OpeningCaptureMaterials('unused','serial','0',tmp_path/'material',max_frames=1)
    completed=[]
    starter=threading.Thread(target=lambda:completed.append(worker.start()))
    starter.start();assert capture_entered.wait(timeout=1)
    assert not completed
    allow_frame.set();starter.join(timeout=2)
    assert completed==[worker] and len(worker.rows)==1
    worker.finish(source_valid=True,battle_start='not_sent')
    assert not worker.thread.is_alive()


def test_owner_finishes_capture_before_the_final_source_check_and_cleanup_is_idempotent(monkeypatch,tmp_path):
    from pcr_maa.clan_battle_native import NativeClanBattle
    calls=[]
    class Capture:
        directory=Path('synthetic-material')
        def stop_and_join(self):calls.append('joined')
        def finish(self,**options):
            calls.append('finished');assert options['source_valid'] is False
            return dict(status='unknown',source_bound=False)
    port=object.__new__(NativeClanBattle)
    port._opening_capture=Capture();port._opening_capture_expected=object();port.battle_start='unknown';port.directory=tmp_path
    def source():
        calls.append('source_checked');assert calls[0]=='joined';return object()
    monkeypatch.setattr(port,'_opening_capture_source',source)
    assert port.finish_opening_capture()['status']=='unknown'
    assert port.finish_opening_capture() is None
    assert calls==['joined','source_checked','finished']


def test_job_json_cannot_enable_capture_when_trusted_process_switch_is_absent(monkeypatch):
    from pcr_maa.clan_battle_native import NativeClanBattle
    monkeypatch.delenv('PCR_CLAN_OPENING_CAPTURE_ONLY',raising=False)
    port=object.__new__(NativeClanBattle);port.plan={'opening_capture':True}
    monkeypatch.setattr(port,'_opening_capture_source',lambda:pytest.fail('unexpected capture source lookup'))
    assert port._start_opening_capture('token') is None


def test_start_failure_always_joins_capture_without_replaying_a_battle(monkeypatch):
    from pcr_maa.clan_battle_native import NativeClanBattle
    port=object.__new__(NativeClanBattle);calls=[]
    def run(plan):calls.append('start_once');raise RuntimeError('fixture unknown result')
    monkeypatch.setattr(port,'_start_and_read_bound',run)
    monkeypatch.setattr(port,'finish_opening_capture',lambda:calls.append('capture_finished'))
    with pytest.raises(RuntimeError,match='fixture unknown result'):
        port.start_and_read({})
    assert calls==['start_once','capture_finished']


def test_capture_material_markers_are_labelled_business_boundaries_not_capture_pts(tmp_path,monkeypatch):
    data=png()
    monkeypatch.setattr(observation,'capture_adb_png',lambda *args,**kwargs:(data,dict(
        source='adb_command_screencap',capture_id='fixture',lower_ns=10,upper_ns=20,
        png_sha256=hashlib.sha256(data).hexdigest(),physical_size=[1280,720])))
    worker=observation.OpeningCaptureMaterials('unused','serial','0',tmp_path/'material',max_frames=1).start()
    worker.mark('dispatch','CBStart','unknown')
    result=worker.finish(source_valid=True,battle_start='sent')
    assert result['markers'][0]['meaning']=='business_call_boundary_not_capture_pts'
    assert result['frames'][0]['lower_ns']==10 and result['frames'][0]['upper_ns']==20
    assert result['first_ub_verified'] is False


def test_opt_in_real_capture_class_reaches_native_start_markers_and_finally_joins(tmp_path,monkeypatch,adb):
    from pcr_maa import clan_battle_native as backend
    data=png();expected=(object(),str(adb),'serial')
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_ONLY','1')
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_DIR',str(tmp_path/'materials'))
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_DISPLAY_ID','0')
    monkeypatch.setattr(observation,'capture_adb_png',lambda *args,**kwargs:(data,dict(
        source='adb_command_screencap',capture_id='fixture-call',lower_ns=1,upper_ns=2,
        png_sha256=hashlib.sha256(data).hexdigest(),physical_size=[1280,720])))
    port=backend.NativeClanBattle(NS(resource=NS(override_pipeline=lambda value:True)),tmp_path/'run')
    port.directory.mkdir();port.plan={};port.characters=NS(begin_opening=lambda *args,**kwargs:None)
    monkeypatch.setattr(port,'freeze_opening_control_policy',lambda plan:None)
    monkeypatch.setattr(port,'_opening_capture_source',lambda:expected)
    monkeypatch.setattr(backend.NativeGuideTrial,'run',lambda *args,**kwargs:dict(native_ok=True,nodes=[]))
    port.arm_opening_start('fixture-token')
    capture=port._opening_capture
    assert capture.rows and port.started is False
    try:
        port.run('CBStart',click=True)
        assert port.started is True and port.battle_start=='sent'
    finally:
        result=port.finish_opening_capture()
    assert not capture.thread.is_alive() and result['first_ub_verified'] is False
    assert [(row['phase'],row['node']) for row in result['markers']]==[('dispatch','CBStart'),('returned','CBStart')]
    assert all(row['meaning']=='business_call_boundary_not_capture_pts' for row in result['markers'])
    assert (port.directory/'opening-capture.json').is_file()


def test_failed_capture_setup_has_explicit_pre_dispatch_not_sent_proof(monkeypatch,tmp_path):
    from pcr_maa import clan_battle_native as backend,native
    port=backend.NativeClanBattle(NS(resource=NS(override_pipeline=lambda value:True)),tmp_path)
    port.plan={};port.characters=NS(begin_opening=lambda *args,**kwargs:None)
    monkeypatch.setattr(port,'freeze_opening_control_policy',lambda plan:None)
    monkeypatch.setattr(port,'_start_opening_capture',lambda token:(_ for _ in ()).throw(RuntimeError('fixture first frame missing')))
    with pytest.raises(native.NativeInputNotSent) as caught:
        port.arm_opening_start('fixture-token')
    assert port.started is False and port.battle_start=='not_sent'
    assert caught.value.receipt['input_execution']=='not_sent'
    assert caught.value.receipt['clan_battle_phase']=='simulation_start'


@pytest.mark.parametrize('outcome',['success','not_sent','unknown'])
def test_formal_run_override_records_real_capture_markers_without_changing_start_facts(tmp_path,monkeypatch,outcome):
    from pcr_maa import clan_battle_daily_native as daily,native
    from pcr_maa.clan_battle_native import NativeClanBattle
    data=png()
    monkeypatch.setattr(observation,'capture_adb_png',lambda *args,**kwargs:(data,dict(
        source='adb_command_screencap',capture_id='fixture',lower_ns=1,upper_ns=2,
        png_sha256=hashlib.sha256(data).hexdigest(),physical_size=[1280,720])))
    capture=observation.OpeningCaptureMaterials('unused','serial','0',tmp_path/'material',max_frames=1).start()
    port=object.__new__(daily.NativeClanDailyAdapter)
    NativeClanBattle.__init__(port,NS(),tmp_path)
    pending={'intent_id':'fixture-intent'}
    port.pending=pending;port.action={'kind':'regular'};port.page=NS(expected=None)
    port.calibration={'daily':{'execution':{}}}
    port._opening_capture=capture
    proof={'input_execution':'not_sent'}
    def run(*args,**kwargs):
        if outcome=='not_sent':raise native.NativeInputNotSent(proof)
        if outcome=='unknown':raise RuntimeError('fixture SDK unknown')
        return dict(native_ok=True,nodes=[])
    monkeypatch.setattr(daily.NativeGuideTrial,'run',run)
    try:
        if outcome=='success':port.run('CBStart',click=True)
        else:
            with pytest.raises(RuntimeError):port.run('CBStart',click=True)
    finally:
        result=capture.finish(source_valid=outcome=='success',battle_start=port.battle_start)
    assert port.pending is pending and port.started is True
    assert port.battle_start=={'success':'sent','not_sent':'not_sent','unknown':'unknown'}[outcome]
    assert result['first_ub_verified'] is False and not capture.thread.is_alive()
    phases=[row['phase'] for row in result['markers']]
    assert phases==(['dispatch','returned'] if outcome=='success' else ['dispatch'])
    if outcome=='not_sent':assert proof['daily_intent_id']=='fixture-intent'


def test_capture_marker_failure_invalidates_material_without_failing_business(tmp_path,monkeypatch):
    worker=observation.OpeningCaptureMaterials('unused','serial','0',tmp_path/'unused')
    monkeypatch.setattr(observation.time,'monotonic_ns',lambda:(_ for _ in ()).throw(RuntimeError('fixture clock error')))
    worker.mark('dispatch','CBStart','unknown')
    assert worker.error=='capture_marker_failed:RuntimeError' and worker.markers==[]
