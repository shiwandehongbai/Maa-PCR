"""Caller regressions use synthetic IO; native frame guards are checked separately."""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from pcr_maa import clan_battle, clan_battle_native as native, clan_battle_daily_native as daily
from test_clan_battle_native import plan, Registry, receipt, result_page_receipt, identity
from test_clan_opening import paused_port


@pytest.fixture(params=['simulation', 'formal'])
def opening_caller(request, plan, tmp_path, monkeypatch):
    formal = request.param == 'formal'
    port = (daily.NativeClanDailyAdapter.__new__(daily.NativeClanDailyAdapter) if formal
            else native.NativeClanBattle.__new__(native.NativeClanBattle))
    resource = Registry()
    native.NativeClanBattle.__init__(port, NS(resource=resource), tmp_path, battle_speed=2)
    port.calibration = {'version': 2, 'daily': {'execution': {'opening': {'battle_speed': 2}}}} if formal else {
        'version': 2, 'auto_battle': {'speeds': [1, 2, 4]}}
    port.formal_opening, port.opening_paused = formal, formal
    state = paused_port(plan).state
    state.update(battle_speed=2, remaining_seconds=84)
    initial = receipt(deepcopy(state))
    calls, reads, toggles = [], [], []
    monkeypatch.setattr(port, 'verify_team', lambda _: {'native_evidence': {}})
    def arm(token, *, start_seconds, action_kind):
        assert not formal and type(start_seconds) is int and start_seconds == 90
        assert action_kind == 'simulation'
    monkeypatch.setattr(port, 'arm_opening_start', arm)
    monkeypatch.setattr(port, 'checkpoint', lambda: None)

    def read(value=None):
        assert value is not None, 'caller must reuse verified observation without an unguarded second read'
        reads.append(value)
        return deepcopy(value['nodes'][-1]['recognition']['best']['detail'])
    monkeypatch.setattr(port, 'read_battle_settings', read)

    def toggle_set(slot, previous, desired, **kwargs):
        assert state['sets'][slot] is previous
        state['sets'][slot] = desired
        toggles.append(('set', state['unit_ids'][slot]))
        return deepcopy(state)
    def toggle_auto(previous, desired, **kwargs):
        assert state['auto'] is previous
        state['auto'] = desired
        toggles.append(('auto',))
        return deepcopy(state)
    monkeypatch.setattr(port, 'toggle_set', toggle_set)
    monkeypatch.setattr(port, 'toggle_auto', toggle_auto)

    def run(node, *, click=False):
        calls.append(node)
        assert click
        if node == 'CBStart':
            return initial
        expected = resource.nodes[node]['custom_recognition_param']
        assert all(expected[key] == state[key] for key in ('auto', 'sets', 'unit_ids', 'battle_speed', 'remaining_seconds'))
        if node == 'CBOpeningSpeed':
            state['battle_speed'] = 2
            return receipt(deepcopy(state))
        assert node == 'CBResumeOpening'
        return receipt(deepcopy(state))
    monkeypatch.setattr(port, 'run', run)

    def wait(names, *, seconds):
        assert names in (['CBSettingsConfirmed'], ['CBPage_result'])
        port.last_receipt = (result_page_receipt(dict(identity(plan), damage=123)) if names == ['CBPage_result']
                             else receipt(dict(deepcopy(state), paused=False)))
        return names[0]
    monkeypatch.setattr(port, 'wait', wait)
    monkeypatch.setattr(port, 'read_page', lambda *_: pytest.fail('must not recapture result'))

    def invoke():
        initial['nodes'][-1]['recognition']['best']['detail'] = deepcopy(state)
        return (port.configure_opening(plan, initial) if formal else port._start_and_read_bound(plan)['settings_evidence'])
    return NS(port=port, plan=plan, state=state, calls=calls, reads=reads, toggles=toggles,
              invoke=invoke, formal=formal, resource=resource)


@pytest.mark.parametrize('difference', ['none', 'set', 'auto', 'speed'])
def test_opening_caller_reuses_verified_initial_or_last_toggle_readback(opening_caller, difference):
    h = opening_caller
    if difference == 'set':
        h.plan['set_by_unit'][str(h.plan['unit_ids'][0])] = False
    elif difference == 'auto':
        h.state['auto'] = False
    elif difference == 'speed':
        h.state['battle_speed'] = 1
    result = h.invoke()
    assert result['settings_verified'] is True
    assert result['observation']['paused'] is False and result['battle_speed'] == 2
    assert h.calls == ([] if h.formal else ['CBStart']) + (
        ['CBOpeningSpeed'] if difference == 'speed' else []) + ['CBResumeOpening']
    assert len(h.reads) == (3 if difference == 'speed' else 2)
    assert h.toggles == ([('set', h.plan['unit_ids'][0])] if difference == 'set' else
                         [('auto',)] if difference == 'auto' else [])
    assert len(result['actions']) == (0 if difference == 'none' else 1)
    expected = h.resource.nodes['CBResumeOpening']['custom_recognition_param']
    assert expected['battle_speed'] == 2
    assert expected['auto'] is True
    assert expected['sets'] == h.state['sets']


@pytest.mark.parametrize('field,value,code', [
    ('paused', False, 'opening_not_verified'), ('same_frame', False, 'opening_not_verified'),
    ('remaining_seconds', 83, 'opening_not_verified'), ('opening_start_token', 'different-owner', 'opening_not_verified'),
    ('first_ub_evidence', {}, 'opening_not_verified'), ('first_ub_observation', {}, 'opening_not_verified'),
    ('auto', False, 'switch_state_changed'), ('sets', [False]*5, 'switch_state_changed'),
    ('unit_ids', [1, 2, 3, 4, 5], 'switch_state_changed'), ('battle_speed', 1, 'switch_state_changed'),
])
def test_opening_caller_rejects_changed_returned_observation(opening_caller, monkeypatch, field, value, code):
    h = opening_caller
    original = clan_battle.execute_switches
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        result['observation'][field] = deepcopy(value)
        return result
    monkeypatch.setattr(clan_battle, 'execute_switches', changed)
    with pytest.raises(RuntimeError, match=code):
        h.invoke()
    assert h.calls == ([] if h.formal else ['CBStart'])
    assert not h.toggles
