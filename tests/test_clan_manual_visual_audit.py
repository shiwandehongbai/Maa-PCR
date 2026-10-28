"""Synthetic visual-audit gate tests; no device or real ledger access."""
from copy import deepcopy

import pytest

from pcr_maa.clan_battle import execute_simulation
from pcr_maa.preset_execution import Journal
from test_clan_battle import plan, Port, reading


def visual_pair(plan):
    target = {key: deepcopy(plan[key]) for key in ('server', 'period', 'phase', 'round_min', 'round_max', 'boss')}
    intent = dict(state='in_flight', operation='clan_battle_simulation', intent_id='f'*32,
        account_id=plan['account_id'], confirmed_for=plan['confirmed_for'], at=1.0,
        target=target, team_evidence=reading(plan))
    evidence = dict(intent_id=intent['intent_id'], damage_verified=True, normal_owner_acceptance=False,
        settings_verified=False, first_ub_verified=False, normal_return_verified=False, simulation=True, damage=123456,
        native_result_verified=False, native_readback='unknown', manual_battle_intervention=True,
        manual_return_observed=True, continuity_review_required=False,
        continuity_review=dict(reviewer='root', intent_id=intent['intent_id']),
        damage_verification_source='manual_visual_result_and_hp_difference',
        result_frame_sha256='a'*64, returned_frame_sha256='b'*64, result_return_manifest_sha256='c'*64,
        manual_result=dict(value=123456, hp_current=876544, hp_maximum=1000000,
            boss=plan['boss'], phase=intent['team_evidence']['phase']))
    event = dict(deepcopy(intent), state='settled', outcome='manual_calibration_result', simulation=True,
        damage=123456, damage_verified=True, settings_verified=False, normal_owner_acceptance=False,
        first_ub_verified=False, normal_return_verified=False, resolution_source='manual_visual_result',
        result_evidence=evidence)
    return intent, event


def invoke(plan, tmp_path, intent, event):
    journal = Journal(tmp_path / 'visual-ledger')
    with journal.locked():
        journal.append(intent)
        journal.append(event)
    port = Port(plan, fail='stop')
    result = execute_simulation(port, journal, plan)
    return result, port


@pytest.mark.parametrize('intervention', [True, False])
def test_reviewed_visual_result_and_hp_difference_allow_a_fresh_request(plan, tmp_path, intervention):
    intent, event = visual_pair(plan)
    event['result_evidence']['manual_battle_intervention'] = intervention
    result, port = invoke(plan, tmp_path, intent, event)
    assert result['error'] == 'stopped' and port.checks == 1 and port.starts == 0


@pytest.mark.parametrize('fault', [
    'unknown_source', 'unknown_outcome', 'unreviewed', 'wrong_reviewer', 'wrong_review_intent',
    'wrong_boss', 'wrong_phase', 'wrong_team', 'wrong_team_account', 'wrong_target', 'wrong_timestamp',
    'missing_team', 'wrong_damage', 'wrong_hp', 'bool_hp', 'missing_frame', 'missing_return_frame',
    'missing_manifest', 'missing_intervention', 'integer_intervention', 'unknown_intervention', 'no_return', 'native_claimed', 'native_not_unknown',
    'normal_owner_claimed', 'settings_claimed', 'first_ub_claimed', 'normal_return_claimed',
    'unverified_damage', 'wrong_evidence_damage', 'not_simulation', 'wrong_intent'])
def test_visual_audit_never_unblocks_unknown_or_mismatched_evidence(plan, tmp_path, fault):
    intent, event = visual_pair(plan)
    evidence = event['result_evidence']
    visual = evidence['manual_result']
    if fault == 'unknown_source': event['resolution_source'] = 'unknown'
    elif fault == 'unknown_outcome': event['outcome'] = 'unknown'
    elif fault == 'unreviewed': evidence['continuity_review_required'] = True
    elif fault == 'wrong_reviewer': evidence['continuity_review']['reviewer'] = 'other'
    elif fault == 'wrong_review_intent': evidence['continuity_review']['intent_id'] = 'other'
    elif fault == 'wrong_boss': visual['boss'] = plan['boss'] % 5 + 1
    elif fault == 'wrong_phase': visual['phase'] = 'D'
    elif fault == 'wrong_team': event['team_evidence']['unit_ids'][0] += 1
    elif fault == 'wrong_team_account': intent['team_evidence']['account_id'] = event['team_evidence']['account_id'] = '99999'
    elif fault == 'wrong_target': event['target']['boss'] = plan['boss'] % 5 + 1
    elif fault == 'wrong_timestamp': event['at'] += 1
    elif fault == 'missing_team': intent['team_evidence'] = event['team_evidence'] = None
    elif fault == 'wrong_damage': visual['value'] += 1
    elif fault == 'wrong_hp': visual['hp_current'] += 1
    elif fault == 'bool_hp': visual['hp_current'] = True
    elif fault == 'missing_frame': evidence.pop('result_frame_sha256')
    elif fault == 'missing_return_frame': evidence.pop('returned_frame_sha256')
    elif fault == 'missing_manifest': evidence['result_return_manifest_sha256'] = 'unknown'
    elif fault == 'missing_intervention': evidence.pop('manual_battle_intervention')
    elif fault == 'integer_intervention': evidence['manual_battle_intervention'] = 0
    elif fault == 'unknown_intervention': evidence['manual_battle_intervention'] = None
    elif fault == 'no_return': evidence['manual_return_observed'] = False
    elif fault == 'native_claimed': evidence['native_result_verified'] = True
    elif fault == 'native_not_unknown': evidence['native_readback'] = 'verified'
    elif fault == 'normal_owner_claimed': event['normal_owner_acceptance'] = True
    elif fault == 'settings_claimed': event['settings_verified'] = True
    elif fault == 'first_ub_claimed': evidence['first_ub_verified'] = True
    elif fault == 'normal_return_claimed': event['normal_return_verified'] = True
    elif fault == 'unverified_damage': evidence['damage_verified'] = False
    elif fault == 'wrong_evidence_damage': evidence['damage'] += 1
    elif fault == 'not_simulation': evidence['simulation'] = False
    elif fault == 'wrong_intent': evidence['intent_id'] = 'other'
    result, port = invoke(plan, tmp_path, intent, event)
    assert result['error'] == 'clan_battle_previous_result_unknown'
    assert port.checks == port.starts == 0


@pytest.mark.parametrize('field', ['account_id', 'confirmed_for', 'intent_id'])
def test_visual_audit_mismatched_identity_is_invalid_before_any_start(plan, tmp_path, field):
    intent, event = visual_pair(plan)
    event[field] = 'different'
    journal = Journal(tmp_path / 'visual-identity')
    with journal.locked():
        journal.append(intent)
        journal.append(event)
    port = Port(plan)
    with pytest.raises(ValueError, match='clan_battle_ledger_binding_invalid'):
        execute_simulation(port, journal, plan)
    assert port.checks == port.starts == 0


@pytest.mark.parametrize('where', ['team', 'visual'])
def test_boolean_boss_cannot_match_boss_one_in_visual_audit(plan, tmp_path, where):
    from test_clan_battle import bind
    plan['boss'] = 1
    bind(plan)
    intent, event = visual_pair(plan)
    if where == 'team':
        intent['team_evidence']['boss'] = event['team_evidence']['boss'] = True
    else:
        event['result_evidence']['manual_result']['boss'] = True
    result, port = invoke(plan, tmp_path, intent, event)
    assert result['error'] == 'clan_battle_previous_result_unknown'
    assert port.checks == port.starts == 0
