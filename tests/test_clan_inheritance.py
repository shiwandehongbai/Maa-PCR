"""Settings restoration survives a stopped Owner without replaying a battle."""
from copy import deepcopy
import time
from types import SimpleNamespace as NS

import pytest

from pcr_maa import native
from pcr_maa.clan_battle_calibration import load_calibration
from pcr_maa.clan_battle_inheritance import BattleInheritance, InheritancePage, inheritance_pipeline
from pcr_maa.clan_battle_native import ClanBattlePage, NativeClanBattle, calibration_pipeline, _detail
from pcr_maa.preset_execution import Journal
from test_clan_battle import Port, plan
from test_clan_opening import sampled_plan
from test_agent_callbacks import registry, FakeContext


class SettingsPort:
    account = '123456789'

    def __init__(self, state=True):
        self.state, self.actions, self.stopped = state, [], False

    def read_inheritance(self):
        if self.stopped:
            raise RuntimeError('stopped')
        return dict(inherit=self.state, same_frame=True, frame_pixel_sha256='a'*64, native_evidence={'read':True})

    def set_inheritance(self, previous, desired):
        self.read_inheritance()
        assert self.state is previous and previous is not desired
        self.actions.append((previous, desired))
        self.state = desired
        return self.read_inheritance()


def test_inheritance_callback_is_bound_and_revoked_by_the_actual_owner_registry(registry,monkeypatch):
    plan = sampled_plan()
    profile = load_calibration(plan)
    callback = InheritancePage(profile['auto_battle']['inheritance'],ClanBattlePage(plan,profile))
    context, calls = FakeContext(), []
    def analyze(ctx,argv):
        calls.append(argv.custom_recognition_name)
        return callback.AnalyzeResult(box=[0,0,1,1],detail={'inherit':True})
    monkeypatch.setattr(callback,'analyze',analyze)
    argv = NS(custom_recognition_name='ClanBattleInheritance',task_detail=NS(task_id=1))
    with registry.scope(context,deadline=time.monotonic()+10) as facade:
        assert facade.resource.register_custom_recognition('ClanBattleInheritance',callback)
        assert facade._callback_scope.bindings['ClanBattleInheritance'] is callback
        with facade._callback_scope.dispatch():
            assert registry.recognitions['ClanBattleInheritance'].analyze(context,argv) is not None
    assert calls == ['ClanBattleInheritance'] and not facade._callback_scope.bindings
    assert registry.recognitions['ClanBattleInheritance'].analyze(context,argv) is None


@pytest.mark.parametrize('original', [True, False])
def test_restores_original_without_changing_an_originally_disabled_setting(tmp_path, original):
    port = SettingsPort(original)
    manager = BattleInheritance(port, Journal(tmp_path))
    manager.prepare()
    assert port.state is False
    evidence = manager.restore()
    assert port.state is original and evidence['original'] is original and evidence['restored'] is True
    assert port.actions == ([(True, False), (False, True)] if original else [])
    events = manager.journal.events()
    assert len(events) == (2 if original else 0)
    if events:
        assert events[0]['intent_id'] == events[1]['intent_id'] and events[1]['restored'] is True


def test_stop_retains_pending_restore_and_new_owner_restores_it_first(tmp_path):
    port = SettingsPort()
    first = BattleInheritance(port, Journal(tmp_path))
    first.prepare()
    old = deepcopy(first.intent)
    port.stopped = True
    with pytest.raises(RuntimeError, match='stopped'):
        first.restore()
    assert first.journal.events() == [old] and port.actions == [(True,False)]
    port.stopped = False
    second = BattleInheritance(port, Journal(tmp_path))
    second.prepare()
    assert second.intent['intent_id'] != old['intent_id']
    assert port.actions == [(True,False),(False,True),(True,False)]
    second.restore()
    assert port.state is True and len(second.journal.events()) == 4


def test_ack_failure_after_disable_keeps_original_for_recovery(tmp_path):
    port = SettingsPort()
    original_set = port.set_inheritance
    def fail(previous, desired):
        original_set(previous, desired)
        raise RuntimeError('input_readback_unknown')
    port.set_inheritance = fail
    manager = BattleInheritance(port, Journal(tmp_path))
    with pytest.raises(RuntimeError, match='input_readback_unknown'):
        manager.prepare()
    assert port.state is False and len(manager.journal.events()) == 1
    port.set_inheritance = original_set
    manager.restore()
    assert port.state is True and len(manager.journal.events()) == 2


def test_other_account_cannot_replace_a_pending_global_restore(tmp_path):
    port = SettingsPort()
    manager = BattleInheritance(port, Journal(tmp_path))
    manager.prepare()
    port.account = '999'
    previous = manager.journal.path.read_bytes()
    with pytest.raises(RuntimeError, match='pending_other_account'):
        BattleInheritance(port, Journal(tmp_path)).prepare()
    assert manager.journal.path.read_bytes() == previous and port.actions == [(True,False)]


def test_completed_settings_history_does_not_block_a_different_account(tmp_path):
    port = SettingsPort()
    manager = BattleInheritance(port, Journal(tmp_path))
    manager.prepare()
    manager.restore()
    port.account = '999'
    manager = BattleInheritance(port, Journal(tmp_path))
    manager.prepare()
    manager.restore()
    assert len(manager.journal.events()) == 4


def test_restore_failure_never_loses_a_known_damage_or_resends_the_battle(plan, tmp_path):
    from pcr_maa.clan_battle import execute_simulation
    port = Port(plan)
    def unknown():
        raise RuntimeError('restore_readback_unknown')
    port.finish_inheritance = unknown
    journal = Journal(tmp_path)
    result = execute_simulation(port,journal,plan)
    assert result['damage'] == 12345678 and result['damage_verified'] and result['settlement_verified']
    assert result['error'] == 'clan_battle_inheritance_restore_pending'
    assert result['inheritance']['restored'] is False
    assert len(journal.events()) == 2 and port.starts == 1


def test_owner_stop_does_not_hide_original_error_or_create_a_battle_intent(plan,tmp_path):
    from pcr_maa.clan_battle import execute_simulation
    port = Port(plan,fail='stop')
    called = []
    def stopped():
        called.append('check_restore')
        raise RuntimeError('stopped')
    port.finish_inheritance = stopped
    journal = Journal(tmp_path)
    result = execute_simulation(port,journal,plan)
    assert result['error'] == 'stopped' and called == ['check_restore']
    assert not result['simulation_started'] and not journal.events() and port.starts == 0


@pytest.mark.parametrize('initial',['CBInheritanceSettingsObserved','CBInheritanceFormationObserved'])
def test_new_start_can_reach_menu_to_recover_after_a_prestart_stop(initial,tmp_path,monkeypatch):
    profile=load_calibration(sampled_plan())
    port=NativeClanBattle(NS(),tmp_path,calibration=profile)
    calls=[]
    states=iter([initial]+(['LabyClientMainMenuObserved'] if initial=='CBInheritanceSettingsObserved' else
        ['CBObserve_boss_detail','CBObserve_lobby','LabyClientAdventureObserved','LabyClientMainMenuObserved']))
    monkeypatch.setattr(port,'wait',lambda *args,**kwargs:next(states))
    monkeypatch.setattr(port,'run',lambda name,**kwargs:calls.append(name) or {})
    port.page=NS(reset_context=lambda:calls.append('reset_context'))
    port._main_menu()
    assert 'CBStart' not in calls
    assert calls[0]==('CBInheritanceClose' if initial=='CBInheritanceSettingsObserved' else 'CBInheritanceFormationCancel')


@pytest.mark.parametrize('mode_readable',[True,False])
def test_formation_recovery_only_cancels_a_verified_simulation_page(mode_readable):
    from pcr_maa.replay import SavedImages
    plan=sampled_plan()
    profile=load_calibration(plan)
    path=native.ROOT/'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples/formation.png'
    image=native.read_image(path)
    if not mode_readable:
        image[650:675,810:945]=0
    resource=native.load_resource()
    assert resource.override_pipeline(calibration_pipeline(profile,plan))
    controller=SavedImages([[image]])
    tasker=native.bind(resource,controller)
    tasker._resource_holder=resource
    try:
        receipt=native.run(tasker,'CBInheritanceFormationCancel',reset_hits=('CBInheritanceFormationCancel',))
        assert receipt['native_ok'] is mode_readable
        assert len(controller.clicks)==int(mode_readable) and not controller.swipes
        if mode_readable:
            x,y=controller.clicks[0]
            assert 855<=x<=1045 and 560<=y<=635
    finally:
        stopped=native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


@pytest.mark.parametrize('sample,expected', [('inherit-on',True),('inherit-off',False)])
def test_real_settings_page_reads_both_radios_on_the_same_frame(sample, expected):
    from pcr_maa.replay import SavedImages
    plan = sampled_plan()
    profile = load_calibration(plan)
    spec = profile['auto_battle']['inheritance']
    resource = native.load_resource()
    callback = InheritancePage(spec, ClanBattlePage(plan, profile))
    assert resource.register_custom_recognition('ClanBattleInheritance', callback)
    assert resource.override_pipeline(inheritance_pipeline(spec))
    path = native.ROOT/'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples'/f'{sample}.png'
    controller = SavedImages([[native.read_image(path)]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    try:
        receipt = native.run(tasker,'CBInheritanceRead')
        assert receipt['native_ok']
        state = _detail(receipt)
        assert state['inherit'] is expected and state['same_frame'] is True
        assert not controller.clicks and not controller.swipes
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


@pytest.mark.parametrize('sample,node', [
    ('inherit-menu','CBInheritanceOpen'), ('inherit-settings-system','CBInheritanceBattle'),
    ('inherit-settings-battle-index','CBInheritanceExpand'), ('inherit-on','CBInheritanceDisable'),
    ('inherit-off','CBInheritanceEnable')])
def test_navigation_and_setting_input_are_guarded_by_the_actual_page(sample, node):
    from pcr_maa.replay import SavedImages
    from pcr_maa.labyrinth.game_session import game_session_pipeline
    plan = sampled_plan()
    profile = load_calibration(plan)
    spec = profile['auto_battle']['inheritance']
    resource = native.load_resource()
    callback = InheritancePage(spec, ClanBattlePage(plan, profile))
    assert resource.register_custom_recognition('ClanBattleInheritance',callback)
    assert resource.override_pipeline(game_session_pipeline(int(plan['account_id'])))
    assert resource.override_pipeline(inheritance_pipeline(spec))
    path = native.ROOT/'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples'/f'{sample}.png'
    controller = SavedImages([[native.read_image(path)]])
    tasker = native.bind(resource,controller)
    tasker._resource_holder = resource
    try:
        receipt = native.run(tasker,node,reset_hits=(node,))
        native.require_native_input(receipt,(node,))
        assert len(controller.clicks) == 1 and not controller.swipes
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


@pytest.mark.parametrize('node', ['CBInheritanceRead','CBInheritanceDisable','CBInheritanceEnable'])
def test_unknown_settings_page_never_changes_any_setting(node):
    import numpy as np
    from pcr_maa.replay import SavedImages
    plan = sampled_plan()
    profile = load_calibration(plan)
    spec = profile['auto_battle']['inheritance']
    resource = native.load_resource()
    callback = InheritancePage(spec,ClanBattlePage(plan,profile))
    assert resource.register_custom_recognition('ClanBattleInheritance',callback)
    assert resource.override_pipeline(inheritance_pipeline(spec))
    controller = SavedImages([[np.zeros((720,1280,3),np.uint8)]])
    tasker = native.bind(resource,controller)
    tasker._resource_holder = resource
    try:
        receipt = native.run(tasker,node,reset_hits=(node,))
        assert not receipt['native_ok'] and not controller.clicks and not controller.swipes
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']
