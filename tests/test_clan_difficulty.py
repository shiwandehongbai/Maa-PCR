"""Saved C/B menu frames prove selection only; normal Owner acceptance is separate."""
import hashlib
from pathlib import Path
import pytest
from pcr_maa import native
from pcr_maa.clan_battle_calibration import load_calibration
from pcr_maa.clan_battle_native import ClanBattlePage, calibration_pipeline, _detail
from pcr_maa.labyrinth.game_session import game_session_pipeline
from pcr_maa.replay import SavedImages
from test_clan_opening import sampled_plan

SAMPLES = {
    'difficulty-c-selected.png':'bc6d224ae2e6c8489ee6bc0ec3bc20efdbb03a620356f01d12d32eb972c07f1b',
    'difficulty-b-selected-c-menu.png':'47288d18daa867fcb2b7bce4cc2152f1ebacba4509a6568de0460526429a42ce',
    'boss-detail-c3.png':'1ad1a089c3b785c73d81d88df57481b5a592bffbc11b307ba1b185c416a92e3c',
    'target-c3.png':'8f4514c857660f30511cadda79e1446dbdbba28b091aa398f058eb81368ce4ac',
}


def read_saved(name,phase,node,*,page='difficulty'):
    plan=dict(sampled_plan(),phase=phase,boss=3)
    profile=load_calibration(plan)
    path=native.ROOT/'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples'/name
    if name in SAMPLES:
        assert hashlib.sha256(path.read_bytes()).hexdigest()==SAMPLES[name]
    callback=ClanBattlePage(plan,profile)
    callback.bound=dict(period=plan['period'],boss=3)
    callback.current_page='lobby' if page=='boss_detail' else 'boss_detail'
    callback.expected_page=page
    errors=[]
    read=callback.read
    def diagnosed(context,image,location,**kwargs):
        try:return read(context,image,location,**kwargs)
        except Exception as exc:
            errors.append(str(exc));raise
    callback.read=diagnosed
    resource=native.load_resource()
    assert resource.register_custom_recognition('ClanBattlePage',callback)
    assert resource.override_pipeline(game_session_pipeline(int(plan['account_id'])))
    assert resource.override_pipeline(calibration_pipeline(profile,plan))
    controller=SavedImages([[native.read_image(path)]])
    tasker=native.bind(resource,controller)
    tasker._resource_holder=resource
    try:
        if page=='difficulty':
            label=native.run(tasker,'CBActionLabel_select_difficulty')
            assert label['native_ok']
        receipt=native.run(tasker,node)
        assert not controller.clicks and not controller.swipes
        return receipt,errors
    finally:
        stopped=native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


@pytest.mark.parametrize('name,phase,selected',[
    ('difficulty.png','B',True),
    ('difficulty-b-selected-c-menu.png','B',True),
    ('difficulty-c-selected.png','B',False),
    ('difficulty-c-selected.png','C',True),
    ('difficulty-b-selected-c-menu.png','C',False),
])
def test_actual_menu_keeps_old_b_and_distinguishes_current_c_radio(name,phase,selected):
    receipt,errors=read_saved(name,phase,'CBDifficultyChosen')
    assert receipt['native_ok'] is selected,errors


def test_actual_c3_detail_reads_phase_without_granting_formal_execution():
    receipt,errors=read_saved('boss-detail-c3.png','C','CBPage_boss_detail',page='boss_detail')
    assert receipt['native_ok'],errors
    reading=_detail(receipt)
    assert reading['phase']=='C' and reading['boss']==3


def test_actual_c3_simulation_target_binds_phase_and_full_simulation():
    receipt,errors=read_saved('target-c3.png','C','CBPage_target',page='target')
    assert receipt['native_ok'],errors
    reading=_detail(receipt)
    assert reading['phase']=='C' and reading['boss']==3 and reading['simulation'] is True
    assert reading['live_state']=='off'
    assert reading['round'] is None and reading['round_source']=='selected_difficulty'
    assert reading['selected_difficulty']==dict(phase='C',boss=3,simulation=True)


def test_actual_c3_target_cannot_satisfy_a_b_request():
    receipt,errors=read_saved('target-c3.png','B','CBPage_target',page='target')
    assert not receipt['native_ok']
    assert errors and any('target_mismatch' in error for error in errors)


def test_actual_formal_c3_detail_cannot_be_a_simulation_target():
    receipt,errors=read_saved('boss-detail-c3.png','C','CBPage_target',page='target')
    assert not receipt['native_ok'] and errors


@pytest.mark.parametrize('capability',['formal','carryover'])
def test_c_menu_samples_grant_no_formal_or_carryover(capability):
    with pytest.raises(RuntimeError,match='capability_missing:'+capability):
        load_calibration(dict(sampled_plan(),phase='C',boss=3),capability=capability)


@pytest.mark.parametrize('phase,expected', [('B',[665,307,1,1]),('C',[373,402,1,1])])
def test_difficulty_clicks_existing_selected_radio_center_instead_of_OCR_text(phase,expected):
    plan=dict(sampled_plan(),phase=phase,boss=3)
    callback=ClanBattlePage(plan,load_calibration(plan))
    callback.read=lambda *args,**kwargs:{}
    callback.hit=lambda *args,**kwargs:True
    assert callback.action(None,None,'select_difficulty')==expected


def test_radio_target_still_requires_current_menu_label_guard():
    plan=dict(sampled_plan(),phase='C',boss=3)
    callback=ClanBattlePage(plan,load_calibration(plan))
    callback.read=lambda *args,**kwargs:{}
    callback.hit=lambda *args,**kwargs:False
    with pytest.raises(RuntimeError,match='action_label_unknown'):
        callback.action(None,None,'select_difficulty')
