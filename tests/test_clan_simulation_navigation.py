"""Guarded simulation navigation; fake images never count as live acceptance."""
from copy import deepcopy
from types import SimpleNamespace as NS

import numpy as np
import pytest

from pcr_maa import clan_battle_native as module
from test_clan_battle_native import calibration, calibration_v2, plan, v2_context


def detail_receipt(value):
    return {'nodes': [{'recognition': {'best': {'detail': deepcopy(value)}}}]}


def page_context(calibration_v2, plan):
    nodes, texts, guards, context = v2_context(calibration_v2, plan)
    frame = np.zeros((720, 1280, 3), np.uint8)
    page = module.ClanBattlePage(plan, calibration_v2)
    page.read(context, frame, 'lobby')
    page.advance('open_boss')
    return page, frame, nodes, texts, guards, context


@pytest.mark.parametrize('dead,round_present', [(True, True), (False, False)])
def test_boss_slot_requires_exclusive_live_or_defeated_evidence(calibration_v2, plan, dead, round_present):
    _, texts, guards, context = v2_context(calibration_v2, plan)
    if dead:
        guards.add('CBNavDefeated3')
    if not round_present:
        texts.pop('CBNavRound3')
    page = module.ClanBattlePage(plan, calibration_v2)
    with pytest.raises(RuntimeError, match='boss_state_unknown'):
        page.read(context, np.zeros((720, 1280, 3), np.uint8), 'lobby')


def test_dead_slot_never_uses_old_open_boss_static_click(calibration_v2, plan):
    _, texts, guards, context = v2_context(calibration_v2, plan)
    texts.pop('CBNavRound3')
    guards.add('CBNavDefeated3')
    page = module.ClanBattlePage(plan, calibration_v2)
    frame = np.zeros((720, 1280, 3), np.uint8)
    assert page.read(context, frame, 'lobby')['bosses'][2] == dict(boss=3, round=None, available=False)
    with pytest.raises(RuntimeError, match='boss_unavailable'):
        page.action(context, frame, 'open_boss')


@pytest.mark.parametrize('change', ['dead', 'round'])
def test_open_entry_rechecks_the_live_slot_in_same_frame(calibration_v2, plan, change):
    page, frame, _, texts, guards, context = page_context(calibration_v2, plan)
    params = dict(navigation='open_boss', expected=dict(boss=1, round=5))
    if change == 'dead':
        texts.pop('CBNavRound1')
        guards.add('CBNavDefeated1')
    else:
        texts['CBNavRound1'] = '第6轮'
    with pytest.raises(RuntimeError, match='boss_changed'):
        page.navigation_action(context, frame, params)


def test_navigation_reads_actual_boss_without_mutating_plan_or_bound(calibration_v2, plan):
    page, frame, _, texts, _, context = page_context(calibration_v2, plan)
    before_plan, before_bound = deepcopy(plan), deepcopy(page.bound)
    texts['CBNavBoss'] = '合成魔物1'
    value = page.read_navigation(context, frame)
    assert value == dict(page='boss_detail', period=plan['period'], boss=1, phase='B', simulation=True)
    assert plan == before_plan and page.bound == before_bound
    assert 'boss' not in page.bound and page.current_page == 'lobby'


@pytest.mark.parametrize('change', ['formal', 'wrong_boss', 'missing_arrow', 'popup', 'unbound'])
def test_arrow_input_rejects_unknown_or_changed_detail(calibration_v2, plan, change):
    page, frame, _, texts, guards, context = page_context(calibration_v2, plan)
    guards.add('CBNavArrow_next')
    value = page.read_navigation(context, frame)
    params = dict(navigation='next', expected={key: value[key] for key in ('period', 'boss', 'phase', 'simulation')})
    if change == 'formal':
        texts.pop('CBNavSimulation')
    elif change == 'wrong_boss':
        texts['CBNavBoss'] = '合成魔物4'
    elif change == 'missing_arrow':
        guards.remove('CBNavArrow_next')
    elif change == 'popup':
        guards.remove('CBLabel_boss_detail_0')
    else:
        page.reset_context()
    with pytest.raises(RuntimeError):
        page.navigation_action(context, frame, params)


def test_arrow_input_requires_selected_simulation_even_if_expected_false(calibration_v2, plan):
    page, frame, _, texts, guards, context = page_context(calibration_v2, plan)
    texts.pop('CBNavSimulation')
    guards.add('CBNavArrow_next')
    value = page.read_navigation(context, frame)
    with pytest.raises(RuntimeError, match='navigation_forbidden'):
        page.navigation_action(context, frame, dict(navigation='next',
            expected={key: value[key] for key in ('period', 'boss', 'phase', 'simulation')}))


class NavigationHarness:
    def __init__(self, calibration, plan, tmp_path, monkeypatch, *, alive=(1,), simulation=False, wrong_step=False):
        self.nodes, self.calls = {}, []
        self.state = dict(page='boss_detail', period=plan['period'], phase='B', boss=1, simulation=simulation)
        self.alive, self.wrong_step = alive, wrong_step
        self.port = module.NativeClanBattle(NS(resource=NS(override_pipeline=self.override)), tmp_path, calibration=calibration)
        self.port.page = NS(reset_context=lambda: self.calls.append('reset_context'))
        monkeypatch.setattr(self.port, 'run', self.run)
        monkeypatch.setattr(self.port, 'read_page', self.read)
        monkeypatch.setattr(self.port, '_wait_simulation_page', self.wait)
        monkeypatch.setattr(self.port, 'checkpoint', lambda: None)
        self.plan = plan

    def override(self, nodes):
        self.nodes.update(nodes)
        return True

    def run(self, node, **kwargs):
        self.calls.append(node)
        if node == 'CBNavOpenBoss':
            self.state['boss'] = self.nodes[node]['custom_recognition_param']['expected']['boss']
        elif node == 'CBNavSelectSimulation':
            self.state['simulation'] = True
        elif node == 'CBNavStep':
            mode = self.nodes[node]['custom_recognition_param']['navigation']
            self.state['boss'] += 2 if self.wrong_step else (1 if mode == 'next' else -1)
        elif node == 'CBAction_confirm_difficulty':
            self.state['phase'] = self.plan['phase']
        return detail_receipt(self.state)

    def read(self, page):
        self.calls.append('read_'+page)
        if page == 'lobby':
            value = dict(period=self.plan['period'], bosses=[
                dict(boss=boss, round=5 if boss in self.alive else None, available=boss in self.alive) for boss in range(1, 6)])
        else:
            value = dict(self.state, live_state='absent', round=None, round_source='selected_difficulty',
                         selected_difficulty={key: self.state[key] for key in ('phase', 'boss', 'simulation')})
        return value, detail_receipt(value)

    def wait(self, names, **kwargs):
        if 'CBPage_lobby' in names:
            return 'CBPage_lobby'
        self.port.last_receipt = detail_receipt(self.state)
        return names[0]


@pytest.mark.parametrize('target,alive,steps', [(3, (3,), 0), (3, (1,), 2), (1, (5,), 4), (5, (1,), 4)])
def test_live_entry_and_bounded_steps_reach_original_plan(calibration_v2, plan, tmp_path, monkeypatch, target, alive, steps):
    plan['boss'] = target
    before = deepcopy(plan)
    test = NavigationHarness(calibration_v2, plan, tmp_path, monkeypatch, alive=alive)
    target_value, evidence = test.port._open_simulation_target(plan)
    assert target_value['boss'] == target and target_value['simulation']
    assert test.calls.count('CBNavOpenBoss') == 1
    assert test.calls.count('CBNavSelectSimulation') == 1
    assert test.calls.count('CBNavStep') == steps
    assert all(name not in test.calls for name in ('CBStart', 'CBAction_open_formation'))
    assert plan == before and evidence['navigation'][-1]['boss'] == target


def test_matching_detail_never_clicks_simulation_tab(calibration_v2, plan, tmp_path, monkeypatch):
    test = NavigationHarness(calibration_v2, plan, tmp_path, monkeypatch, alive=(3,), simulation=True)
    test.port._open_simulation_target(plan)
    assert 'CBNavSelectSimulation' not in test.calls and 'CBNavStep' not in test.calls


def test_no_alive_entry_stops_without_any_input(calibration_v2, plan, tmp_path, monkeypatch):
    test = NavigationHarness(calibration_v2, plan, tmp_path, monkeypatch, alive=())
    with pytest.raises(RuntimeError, match='boss_unavailable'):
        test.port._open_simulation_target(plan)
    assert test.calls == ['read_lobby']


def test_wrong_arrow_destination_stops_without_repeating_input(calibration_v2, plan, tmp_path, monkeypatch):
    test = NavigationHarness(calibration_v2, plan, tmp_path, monkeypatch, wrong_step=True)
    with pytest.raises(RuntimeError, match='navigation_changed'):
        test.port._open_simulation_target(plan)
    assert test.calls.count('CBNavStep') == 1
    assert 'read_target' not in test.calls


def test_phase_change_uses_existing_difficulty_selection(calibration_v2, plan, tmp_path, monkeypatch):
    plan['phase'] = 'C'
    test = NavigationHarness(calibration_v2, plan, tmp_path, monkeypatch)
    value, _ = test.port._open_simulation_target(plan)
    assert value['phase'] == 'C'
    assert test.calls.count('CBAction_confirm_difficulty') == 1


def test_owner_loss_never_continues_the_old_navigation(calibration_v2, plan, tmp_path, monkeypatch):
    test = NavigationHarness(calibration_v2, plan, tmp_path, monkeypatch)
    def stopped(*args, **kwargs):
        raise RuntimeError('owner_source_changed')
    monkeypatch.setattr(test.port, '_navigation_input', stopped)
    with pytest.raises(RuntimeError, match='owner_source_changed'):
        test.port._open_simulation_target(plan)
    assert test.calls == ['read_lobby']


def test_navigation_reward_is_bounded_to_five_acknowledgements(calibration_v2, plan, tmp_path, monkeypatch):
    test = NavigationHarness(calibration_v2, plan, tmp_path, monkeypatch)
    count = [0]
    def popup(*args, **kwargs):
        count[0] += 1
        raise module._SimulationReward(dict(content_sha256=str(count[0])))
    monkeypatch.setattr(test.port, '_wait_simulation_page', popup)
    with pytest.raises(RuntimeError, match='reward_limit'):
        test.port._open_simulation_target(plan)
    assert test.calls.count('CBNavRewardConfirm') == 5 and test.calls.count('reset_context') == 5
    assert 'CBNavOpenBoss' not in test.calls


def test_same_acknowledged_reward_is_not_clicked_again(calibration_v2, plan, tmp_path, monkeypatch):
    calibration_v2['pages']['lobby']['navigation_reward'] = {}
    port = module.NativeClanBattle(NS(), tmp_path, calibration=calibration_v2)
    observed = iter([('CBNavRewardObserved', 'old'), ('CBNavRewardObserved', 'old'),
                     ('CBNavRewardObserved', 'new'), ('CBNavRewardObserved', 'new')])
    def choose(names, **kwargs):
        node, signature = next(observed)
        port.last_receipt = detail_receipt(dict(content_sha256=signature))
        return node
    monkeypatch.setattr(port, 'choose', choose)
    monkeypatch.setattr(port, 'checkpoint', lambda: None)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    with pytest.raises(module._SimulationReward) as exc:
        port._wait_simulation_page(['CBPage_lobby'], deadline=module.time.monotonic()+10, acknowledged={'old'})
    assert exc.value.reading['content_sha256'] == 'new'


def test_reward_confirm_binds_same_frame_content(calibration_v2, plan):
    marker = deepcopy(calibration_v2['pages']['lobby']['labels'][0])
    calibration_v2['pages']['lobby']['navigation_reward'] = dict(
        labels=[deepcopy(marker), deepcopy(marker)], confirm=marker, content_roi=[200, 200, 100, 50])
    page, frame, _, _, guards, context = page_context(calibration_v2, plan)
    guards.update({'CBNavRewardLabel0', 'CBNavRewardLabel1', 'CBNavRewardConfirmLabel'})
    observed, _ = page.navigation_action(context, frame, dict(navigation='reward_read'))
    frame[210, 210] = 255
    with pytest.raises(RuntimeError, match='reward_changed'):
        page.navigation_action(context, frame, dict(navigation='reward_confirm',
            expected_sha256=observed['content_sha256']))


def test_any_previously_acknowledged_reward_is_not_reconfirmed(calibration_v2, plan, tmp_path, monkeypatch):
    calibration_v2['pages']['lobby']['navigation_reward'] = {}
    port = module.NativeClanBattle(NS(), tmp_path, calibration=calibration_v2)
    observed = iter([('CBNavRewardObserved', 'A'), ('CBNavRewardObserved', 'A'),
                     ('CBNavRewardObserved', 'B'), ('CBNavRewardObserved', 'B'),
                     ('CBPage_lobby', None)])
    def choose(names, **kwargs):
        node, signature = next(observed)
        port.last_receipt = detail_receipt(dict(content_sha256=signature))
        return node
    monkeypatch.setattr(port, 'choose', choose)
    monkeypatch.setattr(port, 'checkpoint', lambda: None)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    assert port._wait_simulation_page(['CBPage_lobby'],
        deadline=module.time.monotonic()+10, acknowledged={'A', 'B'}) == 'CBPage_lobby'

def timed_reward_wait(calibration, tmp_path, monkeypatch, observations):
    calibration['pages']['lobby']['navigation_reward'] = {}
    port = module.NativeClanBattle(NS(), tmp_path, calibration=calibration)
    clock, calls = NS(value=0.0), []
    def sleep(seconds):
        clock.value += seconds
    monkeypatch.setattr(module, 'time', NS(monotonic=lambda: clock.value, sleep=sleep))
    remaining = iter(observations)
    def choose(names, **kwargs):
        at, node, signature = next(remaining)
        clock.value = at
        calls.append(node)
        port.last_receipt = detail_receipt(dict(content_sha256=signature))
        return node
    monkeypatch.setattr(port, 'choose', choose)
    monkeypatch.setattr(port, 'checkpoint', lambda: None)
    return port, clock, calls


def test_late_first_reward_gets_one_stable_read_window(calibration_v2, tmp_path, monkeypatch):
    observations = [(1.492, 'GTUnknown', None), (3.859, 'GTUnknown', None),
        (6.697, 'GTUnknown', None), (9.524, 'GTUnknown', None),
        (10.918, 'CBNavRewardObserved', 'new'), (13.2, 'CBNavRewardObserved', 'new')]
    port, clock, calls = timed_reward_wait(calibration_v2, tmp_path, monkeypatch, observations)
    with pytest.raises(module._SimulationReward) as exc:
        port._wait_simulation_page(['CBPage_lobby'], deadline=90)
    assert exc.value.reading['content_sha256'] == 'new'
    assert len(calls) == 6 and clock.value == 13.2


def test_unstable_reward_signatures_do_not_renew_stability_window(calibration_v2, tmp_path, monkeypatch):
    observations = [(9.9, 'CBNavRewardObserved', 'A'), (12, 'CBNavRewardObserved', 'B'),
        (15, 'GTUnknown', None), (18, 'CBNavRewardObserved', 'C'), (20, 'CBNavRewardObserved', 'D')]
    port, clock, calls = timed_reward_wait(calibration_v2, tmp_path, monkeypatch, observations)
    with pytest.raises(RuntimeError, match='navigation_timeout'):
        port._wait_simulation_page(['CBPage_lobby'], deadline=90)
    assert len(calls) == 5 and clock.value < 21


def test_acknowledged_reward_does_not_extend_local_deadline(calibration_v2, tmp_path, monkeypatch):
    port, clock, calls = timed_reward_wait(calibration_v2, tmp_path, monkeypatch,
        [(9.9, 'CBNavRewardObserved', 'old')])
    with pytest.raises(RuntimeError, match='navigation_timeout'):
        port._wait_simulation_page(['CBPage_lobby'], deadline=90, acknowledged={'old'})
    assert calls == ['CBNavRewardObserved'] and clock.value == 10


@pytest.mark.parametrize('last_node', ['CBNavRewardObserved', 'CBPage_lobby'])
def test_returning_read_cannot_cross_total_navigation_deadline(calibration_v2, tmp_path, monkeypatch, last_node):
    port, clock, calls = timed_reward_wait(calibration_v2, tmp_path, monkeypatch,
        [(9.9, 'CBNavRewardObserved', 'new'), (12.01, last_node, 'new')])
    with pytest.raises(RuntimeError, match='navigation_timeout'):
        port._wait_simulation_page(['CBPage_lobby'], deadline=12)
    assert len(calls) == 2 and clock.value == 12.01

def test_initial_reward_page_returns_to_normal_account_navigation(calibration_v2, plan, tmp_path, monkeypatch):
    calibration_v2['pages']['lobby']['navigation_reward'] = {}
    calls, observed = [], [False]
    port = module.NativeClanBattle(NS(resource=NS(override_pipeline=lambda _: True)), tmp_path, calibration=calibration_v2)
    port.page = NS(reset_context=lambda: calls.append('reset_context'))
    def initial(names, **kwargs):
        if not observed[0]:
            observed[0] = True
            raise module._SimulationReward(dict(content_sha256='initial-reward'))
        assert kwargs['acknowledged'] == {'initial-reward'}
        return 'CBObserve_lobby' if 'CBObserve_lobby' in names else names[0]
    monkeypatch.setattr(port, '_wait_simulation_page', initial)
    monkeypatch.setattr(port, 'run', lambda node, **kwargs: calls.append(node) or {})
    monkeypatch.setattr(port, '_verify_account', lambda _: calls.append('verify_account'))
    monkeypatch.setattr(port, '_open_simulation_target', lambda _: calls.append('select_simulation') or ({}, {}))
    monkeypatch.setattr(port, 'form_team', lambda: calls.append('form_team'))
    port._prepare_from_current_page(plan)
    assert calls[:4] == ['CBNavRewardConfirm', 'reset_context', 'CBAction_back_to_adventure', 'reset_context']
    assert calls.index('CBNavRewardConfirm') < calls.index('verify_account') < calls.index('select_simulation')
    assert 'CBStart' not in calls and port.battle_start == 'not_sent' and not port.started


def test_initial_reward_page_uses_shared_five_popup_limit(calibration_v2, tmp_path, monkeypatch):
    calibration_v2['pages']['lobby']['navigation_reward'] = {}
    calls, count = [], [0]
    port = module.NativeClanBattle(NS(resource=NS(override_pipeline=lambda _: True)), tmp_path, calibration=calibration_v2)
    port.page = NS(reset_context=lambda: None)
    def popup(*args, **kwargs):
        count[0] += 1
        raise module._SimulationReward(dict(content_sha256=str(count[0])))
    monkeypatch.setattr(port, '_wait_simulation_page', popup)
    monkeypatch.setattr(port, 'run', lambda node, **kwargs: calls.append(node) or {})
    with pytest.raises(RuntimeError, match='reward_limit'):
        port._prepare_from_current_page({})
    assert calls == ['CBNavRewardConfirm']*5


@pytest.mark.parametrize('reason', ['deadline', 'acknowledged'])
def test_confirm_helper_rechecks_budget_and_previous_signature(calibration_v2, tmp_path, monkeypatch, reason):
    port = module.NativeClanBattle(NS(), tmp_path, calibration=calibration_v2)
    calls = []
    monkeypatch.setattr(port, '_navigation_input', lambda *args, **kwargs: calls.append(args))
    reading = dict(content_sha256='seen')
    with pytest.raises(RuntimeError, match='reward_limit'):
        port._confirm_navigation_popup(reading, deadline=0 if reason == 'deadline' else module.time.monotonic()+10,
            acknowledged={'seen'} if reason == 'acknowledged' else set(), rewards=[])
    assert not calls

class TransitionHarness:
    def __init__(self, calibration, plan, tmp_path, monkeypatch, interruptions):
        from contextlib import nullcontext
        calibration['pages']['lobby']['navigation_reward'] = {}
        calibration['auto_battle'] = {'inheritance': {'enabled': True}}
        self.port = module.NativeClanBattle(NS(resource=NS(override_pipeline=lambda _: True)), tmp_path, calibration=calibration)
        self.state, self.calls, self.verified = 'formation', [], False
        self.interruptions, self.pending_reward, self.deadlines = list(interruptions), None, []
        self.port.page = NS(reset_context=lambda: None)
        self.port.inheritance_managers = [NS(journal=NS(locked=lambda: nullcontext()), _pending=lambda: None)]
        monkeypatch.setattr(self.port, 'run', self.run)
        monkeypatch.setattr(self.port, '_wait_simulation_page', self.wait)
        monkeypatch.setattr(self.port, '_verify_account', self.verify)
        monkeypatch.setattr(self.port, '_open_simulation_target', self.target)
        monkeypatch.setattr(self.port, 'form_team', self.form)
        self.plan = plan

    def run(self, node, **kwargs):
        self.calls.append(node)
        if node == 'CBNavRewardConfirm':
            self.state = self.pending_reward
            self.pending_reward = None
        else:
            destinations = {'CBInheritanceFormationCancel': 'detail', 'CBAction_back_to_lobby': 'lobby',
                'CBAction_back_to_adventure': 'adventure', 'LabyClientHomeToAdventure': 'adventure',
                'LabyClientOpenMainMenuOnce': 'menu', 'LabyClientOpenProfileOnce': 'profile',
                'LabyClientProfileBackOnce': 'menu', 'LabyClientMenuToAdventureOnce': 'adventure',
                'CBAction_open_clan': 'lobby', 'CBAction_open_formation': 'formation'}
            self.state = destinations.get(node, self.state)
        return {}

    def wait(self, names, **kwargs):
        self.deadlines.append(kwargs['deadline'])
        if self.interruptions and self.calls and self.calls[-1] == self.interruptions[0][0]:
            _, landing, signature = self.interruptions.pop(0)
            self.pending_reward = landing
            raise module._SimulationReward(dict(content_sha256=signature))
        options = {'formation': ['CBInheritanceFormationObserved', 'CBPage_formation'],
            'detail': ['CBObserve_boss_detail', 'CBPage_boss_detail'], 'lobby': ['CBObserve_lobby', 'CBPage_lobby'],
            'adventure': ['LabyClientAdventureObserved'], 'menu': ['LabyClientMainMenuObserved'],
            'profile': [module.PROFILE_NODE]}
        return next(name for name in options[self.state] if name in names)

    def verify(self, plan):
        if self.verified:
            return
        self.run('LabyClientOpenProfileOnce', click=True)
        self.port.wait([module.PROFILE_NODE], seconds=10)
        self.calls.append('account_read')
        self.run('LabyClientProfileBackOnce', click=True)
        self.port.wait(['LabyClientMainMenuObserved'], seconds=10)
        self.verified = True

    def target(self, plan):
        self.port.wait(['CBPage_lobby'], seconds=10)
        self.state = 'detail'
        return {}, {}

    def form(self):
        assert self.port._simulation_navigation_budget is None
        assert self.state == 'formation'
        self.calls.append('form_team')


@pytest.mark.parametrize('action,landing', [
    ('CBInheritanceFormationCancel', 'lobby'),
    ('CBAction_back_to_lobby', 'lobby'),
    ('CBAction_back_to_adventure', 'lobby'),
    ('LabyClientOpenMainMenuOnce', 'adventure'),
    ('LabyClientOpenProfileOnce', 'profile'),
    ('LabyClientProfileBackOnce', 'menu'),
    ('LabyClientMenuToAdventureOnce', 'menu'),
    ('CBAction_open_formation', 'detail'),
])
def test_late_reward_at_each_prebattle_transition_reobserves_actual_page(calibration_v2, plan, tmp_path, monkeypatch, action, landing):
    test = TransitionHarness(calibration_v2, plan, tmp_path, monkeypatch, [(action, landing, 'late')])
    test.port._prepare_from_current_page(plan)
    assert test.calls.count('CBNavRewardConfirm') == 1
    assert test.calls[-1] == 'form_team' and test.verified
    assert test.deadlines == sorted(test.deadlines) and len(set(test.deadlines)) <= 2
    assert test.port._simulation_navigation_budget is None
    assert not test.port.started and test.port.battle_start == 'not_sent' and 'CBStart' not in test.calls


def test_late_reward_does_not_replay_old_transition_when_already_at_destination(calibration_v2, plan, tmp_path, monkeypatch):
    test = TransitionHarness(calibration_v2, plan, tmp_path, monkeypatch,
        [('CBAction_back_to_adventure', 'adventure', 'late')])
    test.port._prepare_from_current_page(plan)
    assert test.calls.count('CBAction_back_to_adventure') == 1
    confirm = test.calls.index('CBNavRewardConfirm')
    assert test.calls[confirm+1] == 'LabyClientOpenMainMenuOnce'


def test_reward_limit_is_shared_across_all_prepare_transitions(calibration_v2, plan, tmp_path, monkeypatch):
    triggers = [('CBInheritanceFormationCancel', 'lobby', '1'),
        ('CBAction_back_to_adventure', 'adventure', '2'),
        ('LabyClientOpenMainMenuOnce', 'menu', '3'),
        ('LabyClientOpenProfileOnce', 'profile', '4'),
        ('LabyClientProfileBackOnce', 'menu', '5'),
        ('LabyClientMenuToAdventureOnce', 'menu', '6')]
    test = TransitionHarness(calibration_v2, plan, tmp_path, monkeypatch, triggers)
    with pytest.raises(RuntimeError, match='reward_limit'):
        test.port._prepare_from_current_page(plan)
    assert test.calls.count('CBNavRewardConfirm') == 5
    assert test.deadlines == sorted(test.deadlines) and len(set(test.deadlines)) <= 6
    assert test.port._simulation_navigation_budget is None and 'form_team' not in test.calls


def test_unknown_input_is_not_caught_as_reward_or_retried(calibration_v2, plan, tmp_path, monkeypatch):
    test = TransitionHarness(calibration_v2, plan, tmp_path, monkeypatch, [])
    old = test.run
    def run(node, **kwargs):
        if node == 'CBAction_back_to_adventure':
            test.calls.append(node)
            raise RuntimeError('Native input outcome unknown; no retry')
        return old(node, **kwargs)
    monkeypatch.setattr(test.port, 'run', run)
    with pytest.raises(RuntimeError, match='outcome unknown'):
        test.port._prepare_from_current_page(plan)
    assert test.calls.count('CBAction_back_to_adventure') == 1
    assert 'CBNavRewardConfirm' not in test.calls and test.port._simulation_navigation_budget is None


@pytest.mark.parametrize('state,started,pending', [('unknown', False, None), ('sent', True, None),
    ('previously_sent', False, None), ('not_sent', False, {'intent': 'unsettled'})])
def test_prebattle_scope_never_handles_reward_during_unknown_or_active_start(calibration_v2, plan, tmp_path, monkeypatch, state, started, pending):
    test = TransitionHarness(calibration_v2, plan, tmp_path, monkeypatch, [])
    test.port.battle_start, test.port.started, test.port.pending = state, started, pending
    with pytest.raises(RuntimeError, match='scope_invalid'):
        test.port._prepare_from_current_page(plan)
    assert not test.calls


def test_wait_outside_simulation_scope_keeps_original_formal_behavior(calibration_v2, tmp_path, monkeypatch):
    from pcr_maa.guide_trial_native import NativeGuideTrial
    port = module.NativeClanBattle(NS(), tmp_path, calibration=calibration_v2)
    calls = []
    monkeypatch.setattr(NativeGuideTrial, 'wait', lambda self, names, **kwargs: calls.append(names) or names[0])
    monkeypatch.setattr(port, '_wait_simulation_page', lambda *args, **kwargs: pytest.fail('formal wait entered simulation scope'))
    port.battle_start, port.started = 'sent', True
    assert port.wait(['CBPage_result'], seconds=180) == 'CBPage_result'
    assert calls == [['CBPage_result']]


def test_existing_inheritance_recovery_evidence_survives_reward_reobservation(calibration_v2, plan, tmp_path, monkeypatch):
    from contextlib import nullcontext
    test = TransitionHarness(calibration_v2, plan, tmp_path, monkeypatch, [])
    pending, attempts = [True], []
    manager = NS(journal=NS(locked=lambda: nullcontext()), _pending=lambda: {} if pending[0] else None, evidence=None)
    def recover():
        pending[0] = False
        manager.evidence = {'restored': True, 'actual_readback': 'known'}
        attempts.append('recover')
    manager.recover = recover
    test.port.inheritance_managers = [manager]
    monkeypatch.setattr(test.port, 'open_inheritance', lambda: None)
    # Interrupt through the shared wait so confirmation and restart use product logic.
    real_wait = test.wait
    def wait(names, **kwargs):
        if pending[0] is False and manager.evidence is not None and not attempts.count('reward'):
            attempts.append('reward')
            test.pending_reward = 'menu'
            raise module._SimulationReward(dict(content_sha256='after-real-recovery'))
        return real_wait(names, **kwargs)
    monkeypatch.setattr(test.port, '_wait_simulation_page', wait)
    monkeypatch.setattr(test.port, 'close_inheritance', lambda: test.port.wait(['LabyClientMainMenuObserved'], seconds=10))
    test.port._prepare_from_current_page(plan)
    assert test.port.inheritance_managers == [manager] and attempts.count('recover') == 1
    assert test.port.finish_inheritance() == manager.evidence


@pytest.mark.parametrize('unrelated_state', ['unknown', 'conflict', 'dead', 'round_changed'])
def test_open_entry_ignores_unrelated_slot_animation_without_fresh_full_snapshot(
        calibration_v2, plan, unrelated_state):
    page, frame, _, texts, guards, context = page_context(calibration_v2, plan)
    before = deepcopy(page.bound)
    if unrelated_state in ('unknown', 'dead'):
        texts.pop('CBNavRound2')
    if unrelated_state in ('conflict', 'dead'):
        guards.add('CBNavDefeated2')
    if unrelated_state == 'round_changed':
        texts['CBNavRound2'] = '第6轮'
    reading, target = page.navigation_action(context, frame,
        dict(navigation='open_boss', expected=dict(boss=3, round=5)))
    assert reading == dict(page='lobby', period=plan['period'],
        selected_entry=dict(boss=3, round=5, available=True))
    x, y, w, h = calibration_v2['boss_targets']['3']['target']
    assert target == [x + w // 2, y + h // 2, 1, 1]
    assert page.bound == before and 'bosses' not in reading


@pytest.mark.parametrize('boss', [1, 2, 3, 4, 5])
def test_open_entry_clicks_the_center_of_the_bound_boss_target(calibration_v2, plan, boss):
    page, frame, _, _, _, context = page_context(calibration_v2, plan)
    before = deepcopy(page.bound)
    reading, target = page.navigation_action(context, frame,
        dict(navigation='open_boss', expected=dict(boss=boss, round=5)))
    x, y, w, h = calibration_v2['boss_targets'][str(boss)]['target']
    assert target == [x + w // 2, y + h // 2, 1, 1]
    assert reading['selected_entry'] == dict(boss=boss, round=5, available=True)
    assert page.bound == before


@pytest.mark.parametrize('dead,round_present', [(True, True), (False, False)])
def test_open_entry_still_rejects_selected_slot_unknown_or_conflicting(
        calibration_v2, plan, dead, round_present):
    page, frame, _, texts, guards, context = page_context(calibration_v2, plan)
    if dead:
        guards.add('CBNavDefeated3')
    if not round_present:
        texts.pop('CBNavRound3')
    with pytest.raises(RuntimeError, match='boss_state_unknown'):
        page.navigation_action(context, frame,
            dict(navigation='open_boss', expected=dict(boss=3, round=5)))


@pytest.mark.parametrize('change', ['period', 'popup', 'disconnect', 'stopping', 'shape'])
def test_open_entry_keeps_same_frame_page_and_source_guards(calibration_v2, plan, change):
    page, frame, _, texts, guards, context = page_context(calibration_v2, plan)
    if change == 'period':
        texts['CBField_lobby_period'] = '2025-01'
    elif change == 'popup':
        guards.remove('CBLabel_lobby_0')
    elif change == 'disconnect':
        guards.add('LabyDisconnect6002Observed')
    elif change == 'stopping':
        context.tasker.stopping = True
    else:
        frame = frame[:700]
    with pytest.raises(RuntimeError):
        page.navigation_action(context, frame,
            dict(navigation='open_boss', expected=dict(boss=3, round=5)))


@pytest.mark.parametrize('change', ['reset', 'period', 'page', 'missing_snapshot', 'different_entry'])
def test_open_entry_requires_original_verified_lobby_binding(calibration_v2, plan, change):
    page, frame, _, _, _, context = page_context(calibration_v2, plan)
    if change == 'reset':
        page.reset_context()
    elif change == 'period':
        page.bound['period'] = '2025-01'
    elif change == 'page':
        page.current_page = 'formation'
    elif change == 'missing_snapshot':
        page.bound.pop('bosses')
    else:
        page.bound['bosses'][2]['round'] = 4
    with pytest.raises(RuntimeError, match='navigation_unbound'):
        page.navigation_action(context, frame,
            dict(navigation='open_boss', expected=dict(boss=3, round=5)))


@pytest.mark.parametrize('expected', [None, {}, {'boss': 3}, {'boss': 3, 'round': 5, 'extra': 0},
    {'boss': True, 'round': 5}, {'boss': 6, 'round': 5}, {'boss': 3, 'round': True},
    {'boss': 3, 'round': 0}])
def test_open_entry_rejects_malformed_expected_instance(calibration_v2, plan, expected):
    page, frame, _, _, _, context = page_context(calibration_v2, plan)
    with pytest.raises(RuntimeError, match='boss_changed'):
        page.navigation_action(context, frame, dict(navigation='open_boss', expected=expected))


def test_open_entry_keeps_native_input_once_and_original_recognition_timeout(calibration_v2, plan):
    node = module.calibration_pipeline(calibration_v2, plan)['CBNavOpenBoss']
    assert node['action'] == 'Click' and node['max_hit'] == 1
    assert node['timeout'] == 1000 and node['next'] == []



def difficulty_notice_context(calibration_v2, plan):
    page, frame, nodes, texts, guards, context = page_context(calibration_v2, plan)
    for index, text in enumerate(('首领魔物等级', '（难度）', '提升', '魔物的等级（难度）提升了。', '进行模拟战。')):
        texts['CBNavDifficultyNoticeLabel'+str(index)] = text
    texts['CBNavDifficultyNoticeCloseLabel'] = '关闭'
    return page, frame, nodes, texts, guards, context


def test_difficulty_notice_requires_all_markers_and_same_frame_content(calibration_v2, plan):
    page, frame, nodes, _, _, context = difficulty_notice_context(calibration_v2, plan)
    observed, _ = page.navigation_action(context, frame, dict(navigation='difficulty_notice_read'))
    assert observed['page'] == 'difficulty_notice'
    params = dict(navigation='difficulty_notice_close', expected_sha256=observed['content_sha256'])
    confirmed, target = page.navigation_action(context, frame, params)
    assert confirmed == observed and target == nodes['CBNavDifficultyNoticeCloseLabel']['roi']
    assert nodes['CBNavDifficultyNoticeClose']['max_hit'] == 1
    assert nodes['CBNavDifficultyNoticeClose']['next'] == []
    frame[300, 500] = 255
    with pytest.raises(RuntimeError, match='difficulty_notice_changed'):
        page.navigation_action(context, frame, params)


@pytest.mark.parametrize('missing', ['CBNavDifficultyNoticeLabel'+str(i) for i in range(5)]+['CBNavDifficultyNoticeCloseLabel'])
def test_difficulty_notice_rejects_any_missing_specific_marker(calibration_v2, plan, missing):
    page, frame, _, texts, _, context = difficulty_notice_context(calibration_v2, plan)
    texts.pop(missing)
    with pytest.raises(RuntimeError, match='page_field_unknown'):
        page.navigation_action(context, frame, dict(navigation='difficulty_notice_read'))


@pytest.mark.parametrize('change', ['disconnect', 'stopping', 'shape', 'legacy'])
def test_difficulty_notice_keeps_source_and_frame_guards(calibration_v2, plan, change):
    page, frame, _, _, guards, context = difficulty_notice_context(calibration_v2, plan)
    if change == 'disconnect':
        guards.add('LabyDisconnect6002Observed')
    elif change == 'stopping':
        context.tasker.stopping = True
    elif change == 'shape':
        frame = frame[:700]
    else:
        page.modern = False
    with pytest.raises(RuntimeError, match='difficulty_notice_unknown'):
        page.navigation_action(context, frame, dict(navigation='difficulty_notice_read'))


def timed_difficulty_wait(calibration_v2, tmp_path, monkeypatch, observations):
    port, clock, calls = timed_reward_wait(calibration_v2, tmp_path, monkeypatch, observations)
    choose = port.choose
    def with_kind(names, **kwargs):
        node = choose(names, **kwargs)
        if node == 'CBNavDifficultyNoticeObserved':
            value = module._detail(port.last_receipt)
            port.last_receipt = detail_receipt(dict(value, page='difficulty_notice'))
        return node
    monkeypatch.setattr(port, 'choose', with_kind)
    return port, clock, calls


def test_late_difficulty_notice_gets_one_stability_window(calibration_v2, tmp_path, monkeypatch):
    port, clock, calls = timed_difficulty_wait(calibration_v2, tmp_path, monkeypatch,
        [(9.8, 'GTUnknown', None), (10.2, 'CBNavDifficultyNoticeObserved', 'new'),
         (12, 'CBNavDifficultyNoticeObserved', 'new')])
    with pytest.raises(module._SimulationDifficultyNotice):
        port._wait_simulation_page(['CBPage_lobby'], deadline=90)
    assert calls[-2:] == ['CBNavDifficultyNoticeObserved']*2 and clock.value == 12


@pytest.mark.parametrize('cause', ['unstable', 'total_deadline', 'already_closed'])
def test_difficulty_notice_never_extends_total_or_repeated_read_budget(calibration_v2, tmp_path, monkeypatch, cause):
    rows = ([(9.9, 'CBNavDifficultyNoticeObserved', 'A'), (12, 'CBNavDifficultyNoticeObserved', 'B'),
             (20, 'CBNavDifficultyNoticeObserved', 'C')] if cause == 'unstable' else
            [(9.9, 'CBNavDifficultyNoticeObserved', 'A'), (12.1, 'CBNavDifficultyNoticeObserved', 'A')]
            if cause == 'total_deadline' else [(9.9, 'CBNavDifficultyNoticeObserved', 'A')])
    port, _, _ = timed_difficulty_wait(calibration_v2, tmp_path, monkeypatch, rows)
    with pytest.raises(RuntimeError, match='navigation_timeout'):
        port._wait_simulation_page(['CBPage_lobby'], deadline=12 if cause == 'total_deadline' else 90,
            acknowledged={'difficulty_notice:A'} if cause == 'already_closed' else set())


def test_reward_read_cannot_complete_difficulty_notice_second_read(calibration_v2, tmp_path, monkeypatch):
    port, _, calls = timed_difficulty_wait(calibration_v2, tmp_path, monkeypatch,
        [(1, 'CBNavRewardObserved', 'same'), (2, 'CBNavDifficultyNoticeObserved', 'same'),
         (3, 'CBNavDifficultyNoticeObserved', 'same')])
    with pytest.raises(module._SimulationDifficultyNotice):
        port._wait_simulation_page(['CBPage_lobby'], deadline=90)
    assert len(calls) == 3


def test_closed_notice_waits_for_actual_landing_and_is_not_clicked_twice(calibration_v2, tmp_path, monkeypatch):
    port, _, calls = timed_difficulty_wait(calibration_v2, tmp_path, monkeypatch,
        [(1, 'CBNavDifficultyNoticeObserved', 'old'), (2, 'CBNavDifficultyNoticeObserved', 'old'),
         (3, 'CBPage_lobby', None)])
    assert port._wait_simulation_page(['CBPage_lobby'], deadline=90,
        acknowledged={'difficulty_notice:old'}) == 'CBPage_lobby'
    assert calls == ['CBNavDifficultyNoticeObserved']*2+['CBPage_lobby']


def test_initial_notice_closes_once_then_reobserves_account_route(calibration_v2, plan, tmp_path, monkeypatch):
    calls, seen = [], [False]
    port = module.NativeClanBattle(NS(resource=NS(override_pipeline=lambda _: True)), tmp_path, calibration=calibration_v2)
    port.page = NS(reset_context=lambda: calls.append('reset_context'))
    def initial(names, **kwargs):
        if not seen[0]:
            seen[0] = True
            raise module._SimulationDifficultyNotice(dict(page='difficulty_notice', content_sha256='first'))
        assert kwargs['acknowledged'] == {'difficulty_notice:first'}
        return 'CBObserve_lobby' if 'CBObserve_lobby' in names else names[0]
    monkeypatch.setattr(port, '_wait_simulation_page', initial)
    monkeypatch.setattr(port, 'run', lambda node, **kwargs: calls.append(node) or {})
    monkeypatch.setattr(port, '_verify_account', lambda _: calls.append('verify_account'))
    monkeypatch.setattr(port, '_open_simulation_target', lambda _: ({}, {}))
    monkeypatch.setattr(port, 'form_team', lambda: calls.append('form_team'))
    port._prepare_from_current_page(plan)
    assert calls[:4] == ['CBNavDifficultyNoticeClose', 'reset_context', 'CBAction_back_to_adventure', 'reset_context']
    assert calls.count('CBNavDifficultyNoticeClose') == 1 and calls[-1] == 'form_team'
    assert 'CBNavRewardConfirm' not in calls and 'CBStart' not in calls


class DifficultyTransitionHarness(TransitionHarness):
    def run(self, node, **kwargs):
        if node == 'CBNavDifficultyNoticeClose':
            self.calls.append(node)
            self.state, self.pending_reward = self.pending_reward, None
            return {}
        return super().run(node, **kwargs)

    def wait(self, names, **kwargs):
        try:
            return super().wait(names, **kwargs)
        except module._SimulationReward as exc:
            raise module._SimulationDifficultyNotice(dict(exc.reading, page='difficulty_notice'))


@pytest.mark.parametrize('action,landing', [('CBAction_back_to_adventure', 'lobby'),
    ('CBAction_back_to_adventure', 'adventure'), ('CBAction_open_formation', 'detail')])
def test_late_notice_reobserves_actual_transition_landing(calibration_v2, plan, tmp_path, monkeypatch, action, landing):
    test = DifficultyTransitionHarness(calibration_v2, plan, tmp_path, monkeypatch, [(action, landing, 'late')])
    test.port._prepare_from_current_page(plan)
    assert test.calls.count('CBNavDifficultyNoticeClose') == 1 and test.calls[-1] == 'form_team'
    assert not test.interruptions and test.pending_reward is None and len(set(test.deadlines)) == 1
    if landing == 'adventure':
        assert test.calls.count('CBAction_back_to_adventure') == 1
    assert 'CBNavRewardConfirm' not in test.calls and 'CBStart' not in test.calls


@pytest.mark.parametrize('state,started,pending,settled', [('unknown', False, None, False),
    ('sent', True, None, False), ('not_sent', False, {'intent': 'unresolved'}, False)])
def test_notice_close_rejects_unresolved_battle_or_consumption(calibration_v2, tmp_path, monkeypatch, state, started, pending, settled):
    port = module.NativeClanBattle(NS(), tmp_path, calibration=calibration_v2)
    port.battle_start, port.started, port.pending = state, started, pending
    port._simulation_result_return_verified = settled
    monkeypatch.setattr(port, '_navigation_input', lambda *a, **k: pytest.fail('unexpected input'))
    with pytest.raises(RuntimeError, match='scope_invalid'):
        port._confirm_navigation_popup(dict(page='difficulty_notice', content_sha256='new'),
            deadline=module.time.monotonic()+10, acknowledged=set(), rewards=[])


def test_notice_on_verified_result_return_reaches_original_simulation(calibration_v2, plan, tmp_path, monkeypatch):
    test = NavigationHarness(calibration_v2, plan, tmp_path, monkeypatch)
    test.port.started, test.port.battle_start = True, 'sent'
    test.port._simulation_result_return_verified = True
    seen = [False]
    original = test.wait
    def wait(*args, **kwargs):
        if not seen[0]:
            seen[0] = True
            raise module._SimulationDifficultyNotice(dict(page='difficulty_notice', content_sha256='after-result'))
        return original(*args, **kwargs)
    monkeypatch.setattr(test.port, '_wait_simulation_page', wait)
    value, evidence = test.port._open_simulation_target(plan)
    assert value['boss'] == plan['boss'] and value['simulation'] is True
    assert test.calls.count('CBNavDifficultyNoticeClose') == 1 and 'CBStart' not in test.calls
    assert evidence['rewards'][0]['observed']['page'] == 'difficulty_notice'


def test_unknown_notice_close_outcome_is_not_retried(calibration_v2, plan, tmp_path, monkeypatch):
    test = DifficultyTransitionHarness(calibration_v2, plan, tmp_path, monkeypatch,
        [('CBAction_back_to_adventure', 'lobby', 'late')])
    original = test.run
    def run(node, **kwargs):
        if node == 'CBNavDifficultyNoticeClose':
            test.calls.append(node)
            raise RuntimeError('Native input outcome unknown; no retry')
        return original(node, **kwargs)
    monkeypatch.setattr(test.port, 'run', run)
    with pytest.raises(RuntimeError, match='outcome unknown'):
        test.port._prepare_from_current_page(plan)
    assert test.calls.count('CBNavDifficultyNoticeClose') == 1
    assert 'form_team' not in test.calls and test.port._simulation_navigation_budget is None



def test_period_only_rec_is_limited_to_modern_lobby_same_roi_and_pattern(calibration_v2, calibration, plan):
    modern = module.calibration_pipeline(calibration_v2, plan)
    legacy = module.calibration_pipeline(calibration, plan)
    node = modern['CBField_lobby_period']
    assert node['only_rec'] is True
    assert node['roi'] == calibration_v2['pages']['lobby']['fields']['period']
    assert node['threshold'] == .9
    assert 'only_rec' not in legacy['CBField_lobby_period']
    assert {name for name, spec in modern.items() if spec.get('only_rec')} == {'CBField_lobby_period', 'CBField_result_damage_verify'}
    import re
    assert re.fullmatch(node['expected'], '10/25 05:00 ~ 10/29 23:59')
    assert re.fullmatch(node['expected'], '10/2505:00~110/2923:59') is None
    assert re.fullmatch(node['expected'], '10/2505:00~1') is None
    assert re.fullmatch(node['expected'], '10/2923:59') is None



def frozen_period_clock(monkeypatch):
    from datetime import datetime, timezone, timedelta
    class Clock:
        @staticmethod
        def now(zone):
            return datetime(2026, 10, 27, 2, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(module, 'datetime', Clock)


@pytest.mark.parametrize('left', ['', ':', '：'])
@pytest.mark.parametrize('right', ['', ':', '：'])
def test_period_accepts_one_ocr_colon_only_between_date_and_time(calibration_v2, plan, monkeypatch, left, right):
    import re
    frozen_period_clock(monkeypatch)
    text = f'10/25 {left} 05:00 ~ 10/29 {right} 23:59'
    node = module.calibration_pipeline(calibration_v2, plan)['CBField_lobby_period']
    assert re.fullmatch(node['expected'], text)
    assert module._current_period(text) == '2026-10'
    assert node['threshold'] == .9 and node['only_rec'] is True


@pytest.mark.parametrize('text', [
    '10/25 05:00 ~ 10/29::23:59',
    '10/25 05:00 ~ 10/29：:23:59',
    '10/:25 05:00 ~ 10/29 23:59',
    '10/25,05:00 ~ 10/29 23:59',
    '10/2505:00~110/2923:59',
    '10/25 05:00 ~ 10/29 23:59x',
    '10/25:25:00 ~ 10/29 23:59',
    '10/32:05:00 ~ 10/39 23:59',
    '09/25:05:00 ~ 09/29:23:59',
])
def test_period_separator_tolerance_keeps_digits_date_and_current_window_strict(monkeypatch, text):
    frozen_period_clock(monkeypatch)
    with pytest.raises(RuntimeError, match='period_'):
        module._current_period(text)


@pytest.mark.parametrize('kind,owner_deadline,confirm_at,completes', [
    ('reward', 200, 41, True), ('reward', 200, 95, True),
    ('reward', 100, 41, False), ('difficulty', 200, 41, False)])
def test_reward_restart_renews_only_its_navigation_segment(calibration_v2, plan, tmp_path, monkeypatch, kind, owner_deadline, confirm_at, completes):
    calibration_v2['pages']['lobby']['navigation_reward'] = {}
    clock, attempts, actions, acknowledgements = NS(value=0), [], [], []
    fake_time = NS(monotonic=lambda: clock.value, sleep=lambda seconds: setattr(clock, 'value', clock.value + seconds))
    monkeypatch.setattr(module, 'time', fake_time)
    port = module.NativeClanBattle(NS(resource=NS(override_pipeline=lambda _: True)), tmp_path, calibration=calibration_v2)
    port.page = NS(reset_context=lambda: None)
    popup_node = 'CBNavRewardObserved' if kind == 'reward' else 'CBNavDifficultyNoticeObserved'
    popup = dict(content_sha256='new')
    if kind == 'difficulty': popup['page'] = 'difficulty_notice'
    observations = iter([(40, popup_node), (41, popup_node), (110, 'CBPage_boss_detail')])
    def choose(names, **kwargs):
        at, name = next(observations)
        clock.value = at
        assert name in names
        port.last_receipt = detail_receipt(popup)
        return name
    monkeypatch.setattr(port, 'choose', choose)
    monkeypatch.setattr(port, 'checkpoint', lambda: None)
    def run(node, **kwargs):
        actions.append(node)
        clock.value = confirm_at
        return {}
    monkeypatch.setattr(port, 'run', run)
    def prepare(_):
        attempts.append(port._simulation_navigation_budget['deadline'])
        acknowledgements.append(set(port._simulation_navigation_budget['acknowledged']))
        if len(attempts) == 1:
            port.wait(['CBPage_lobby'])
        else:
            clock.value = 110
            port.wait(['CBPage_boss_detail'])
    monkeypatch.setattr(port, '_prepare_navigation', prepare)
    monkeypatch.setattr(port, 'form_team', lambda: actions.append('form_team'))
    token = module.native.RUN_DEADLINE.set(owner_deadline)
    try:
        if completes:
            port._prepare_from_current_page(plan)
        else:
            with pytest.raises(RuntimeError, match='navigation_timeout'):
                port._prepare_from_current_page(plan)
    finally:
        module.native.RUN_DEADLINE.reset(token)
    assert attempts[0] == 90
    assert attempts[1] == (min(confirm_at + 90, owner_deadline) if kind == 'reward' else 90)
    assert acknowledgements[1] == ({'new'} if kind == 'reward' else {'difficulty_notice:new'})
    assert actions == (['CBNavRewardConfirm', 'form_team'] if completes else
        ['CBNavRewardConfirm'] if kind == 'reward' else ['CBNavDifficultyNoticeClose'])
    assert port._simulation_navigation_budget is None
    assert not port.started and port.battle_start == 'not_sent'


def test_settled_return_reward_restart_renews_its_navigation_segment(calibration_v2, plan, tmp_path, monkeypatch):
    test = NavigationHarness(calibration_v2, plan, tmp_path, monkeypatch, alive=(3,), simulation=True)
    test.port.started, test.port.battle_start, test.port._simulation_result_return_verified = True, 'sent', True
    clock, waits = NS(value=0), []
    monkeypatch.setattr(module, 'time', NS(monotonic=lambda: clock.value, sleep=lambda _: None))
    original = test.wait
    def wait(names, *, deadline, **kwargs):
        waits.append(deadline)
        if len(waits) == 1:
            clock.value = 40
            raise module._SimulationReward(dict(content_sha256='return-reward'))
        clock.value = 110
        if clock.value >= deadline:
            raise RuntimeError('clan_battle_simulation_navigation_timeout')
        return original(names, deadline=deadline, **kwargs)
    monkeypatch.setattr(test.port, '_wait_simulation_page', wait)
    token = module.native.RUN_DEADLINE.set(200)
    try:
        value, evidence = test.port._open_simulation_target(plan)
    finally:
        module.native.RUN_DEADLINE.reset(token)
    assert waits[0] == 90 and all(deadline == 130 for deadline in waits[1:])
    assert value['boss'] == 3 and value['simulation'] is True
    assert test.calls.count('CBNavRewardConfirm') == 1 and 'CBStart' not in test.calls
    assert evidence['rewards'][0]['observed']['content_sha256'] == 'return-reward'
