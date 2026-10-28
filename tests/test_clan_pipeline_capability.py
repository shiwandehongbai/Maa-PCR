"""Capability routing fixtures do not grant real formal or carryover calibration."""
from copy import deepcopy
from types import SimpleNamespace as NS
import re

import pytest

from pcr_maa import clan_battle, clan_battle_calibration as loader
from pcr_maa import clan_battle_daily_native as daily, clan_battle_native as native
from test_clan_battle_native import calibration, calibration_v2, plan, Registry
from test_clan_battle_daily_native import profile, executable
from test_clan_battle_calibration import bundle, v2_bundle


@pytest.mark.parametrize('capability', ['formal', 'carryover'])
def test_d_formal_common_nodes_do_not_require_a_simulated_difficulty(calibration_v2, plan, capability):
    plan = dict(plan, phase='D', round_min=23, round_max=None)
    before = deepcopy(calibration_v2)
    nodes = native.calibration_pipeline(calibration_v2, plan, capability=capability)
    assert {'CBReadTeam', 'CBEditTeam', 'CBStart', 'CBEmpty0', 'CBSupport4', 'CBStar0_5',
        'CBList_formation_top', 'CBAction_characters_next'} <= nodes.keys()
    assert not any(name.startswith(('CBNav', 'CBPage_', 'CBObserve_', 'CBDifficulty')) for name in nodes)
    assert not {'CBFullSimulationReady', 'CBPauseOpening', 'CBAction_open_simulation',
        'CBAction_open_difficulty', 'CBAction_disable_live_state', 'CBAction_return_to_target'} & nodes.keys()
    assert calibration_v2 == before
    with pytest.raises(RuntimeError, match='difficulty_unavailable'):
        native.calibration_pipeline(calibration_v2, plan)


@pytest.mark.parametrize('phase', ['B', 'C', 'BC'])
def test_explicit_simulation_preserves_default_pipeline(calibration_v2, plan, phase):
    calibration_v2['actions']['select_difficulty']['choices']['C'] = deepcopy(
        calibration_v2['actions']['select_difficulty']['choices']['B'])
    plan = dict(plan, phase=phase)
    assert native.calibration_pipeline(calibration_v2, plan, capability='simulation') == native.calibration_pipeline(calibration_v2, plan)


@pytest.mark.parametrize('capability', ['simulation', 'formal', 'carryover'])
def test_legacy_pipeline_is_unchanged(calibration, plan, capability):
    assert native.calibration_pipeline(calibration, plan, capability=capability) == native.calibration_pipeline(calibration, plan)


@pytest.mark.parametrize('capability', [None, '', 'timed', 'formal_single'])
def test_pipeline_rejects_unknown_internal_capability(calibration_v2, plan, capability):
    with pytest.raises(ValueError, match='pipeline_capability_invalid'):
        native.calibration_pipeline(calibration_v2, plan, capability=capability)


@pytest.mark.parametrize('capability', ['simulation', 'formal', 'carryover'])
def test_loader_forwards_only_the_requested_capability(v2_bundle, plan, monkeypatch, capability):
    root, _, entry, _, save = v2_bundle
    entry['capabilities'] = ['formal', 'carryover'] if capability == 'carryover' else [capability]
    save()
    calls = []
    monkeypatch.setattr(native, 'calibration_pipeline', lambda *args, **kwargs: calls.append(kwargs) or {})
    monkeypatch.setattr(daily, 'require_daily_execution', lambda *args, **kwargs: {})
    loader.load_calibration(plan, root=root, capability=capability)
    assert calls == [{'capability': capability}]


@pytest.mark.parametrize('capability', ['formal', 'carryover'])
def test_d_slot_uses_formal_opening_and_the_shared_gesture(executable, plan, tmp_path, monkeypatch, capability):
    plan = dict(plan, phase='D', round_min=23, round_max=None, confirmed_for='synthetic-reviewed-plan')
    executable['actions']['characters_next'].update(begin=[1100, 465, 1, 1], end=[1100, 335, 1, 1],
        duration=700, end_hold=250, post_delay=350)
    executable['daily']['execution']['actions']['characters_next'].update(begin=[1100, 465, 1, 1], end=[1100, 335, 1, 1])
    original = deepcopy(executable)
    monkeypatch.setattr(clan_battle, 'load_snapshot', lambda _: dict(ownership_complete=True,
        units=[dict(unit_id=uid) for uid in plan['unit_ids']]))
    loaded = []
    def carry_calibration(job, *, capability):
        loaded.append(capability)
        return executable
    monkeypatch.setattr(loader, 'load_calibration', carry_calibration)
    resource = Registry()
    port = daily.NativeClanDailyAdapter(NS(resource=resource), tmp_path,
        request={'slot_plans': {'1': plan}}, calibrations={plan['confirmed_for']: executable})
    port.configure_slot(plan, capability=capability)
    assert resource.nodes['CBStart']['next'] == ['CBDailyPauseOpening']
    assert not any(name.startswith(('CBNav', 'CBDifficulty')) for name in resource.nodes)
    assert not {'CBPauseOpening', 'CBFullSimulationReady', 'CBPage_difficulty',
        'CBAction_open_simulation', 'CBAction_select_difficulty'} & resource.nodes.keys()
    scroll = resource.nodes['CBAction_characters_next']
    assert {key: scroll[key] for key in ('duration', 'end_hold', 'post_delay')} == {
        'duration': 700, 'end_hold': 250, 'post_delay': 350}
    assert scroll['custom_recognition'] == 'ClanBattleDailyPage'
    assert scroll['custom_recognition_param'] == dict(page='formation', action='characters_next')
    assert scroll['max_hit'] == 1
    assert executable == original and port.calibrations[plan['confirmed_for']] == original
    assert loaded == (['carryover'] if capability == 'carryover' else [])


def test_d_formal_common_builder_does_not_bypass_formation_validation(calibration_v2, plan):
    calibration_v2['formation'].pop('borrowed')
    with pytest.raises(ValueError, match='formation_invalid'):
        native.calibration_pipeline(calibration_v2, dict(plan, phase='D'), capability='formal')


@pytest.mark.parametrize('text,accepted', [
    ('10/25 05:00 ~ 10/29:23:59', True),
    ('10/25：05:00 ~ 10/29：23:59', True),
    ('10/25 05:00 ~ 10/29::23:59', False),
    ('10/25 05:00 ~ 10/29:2:59', False),
    ('10/25 05:00 ~ 10/29:23:69', False)])
def test_formal_period_filter_accepts_only_one_boundary_colon(text, accepted):
    assert (re.fullmatch(daily.PATTERNS['period'], text) is not None) is accepted
