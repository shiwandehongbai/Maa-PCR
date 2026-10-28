"""User-confirmed exit preserves unknown simulation history, never a successful result."""
from copy import deepcopy
import time

import pytest

from pcr_maa import clan_battle
from pcr_maa.preset_execution import Journal
from test_clan_battle import Port, bind, plan


@pytest.fixture
def pending(plan, tmp_path):
    journal = Journal(tmp_path / 'ledger')
    first = Port(plan, fail='unknown_damage')
    report = clan_battle.execute_simulation(first, journal, plan)
    assert report['outcome'] == 'stopped' and first.starts == 1
    intent, = journal.events()
    confirmation = dict(source='direct_user_message', exited=True, continue_remaining=True, text='已退出或放弃')
    evidence = dict(scene='CBPage_lobby', source='maa_observation', verified=True,
        intent_id=intent['intent_id'], start_sent=True, observed_at=time.time(),
        frame_sha256='a' * 64, receipt_sha256='b' * 64, start_receipt_sha256='c' * 64)
    return journal, intent, confirmation, evidence, report


def test_user_exit_preserves_original_and_allows_only_a_new_simulation(plan, pending):
    journal, intent, confirmation, evidence, report = pending
    before = journal.path.read_bytes()
    event = clan_battle.record_simulation_user_exit(journal, intent_id=intent['intent_id'],
        confirmation=confirmation, evidence=evidence)
    assert journal.path.read_bytes().startswith(before)
    assert event['state'] == 'historical_unknown' and event['outcome'] == 'user_exited'
    assert event['original_result_unknown'] and event['replay_forbidden']
    assert event['simulation_start_sent'] and event['damage'] is None
    assert event['damage_verified'] is event['settings_verified'] is event['result_verified'] is False
    assert report['outcome'] == 'stopped' and report['damage'] is None
    port = Port(plan)
    result = clan_battle.execute_simulation(port, journal, plan)
    assert result['outcome'] == 'simulated' and port.starts == 1
    events = journal.events()
    assert len(events) == 4 and events[0] == intent and events[1] == event
    assert events[2]['intent_id'] != intent['intent_id']


@pytest.mark.parametrize('case', [
    'no_confirmation', 'configuration_source', 'not_exited', 'no_continuation', 'no_text',
    'no_evidence', 'wrong_scene', 'not_verified', 'wrong_intent', 'start_unsent', 'old_observation',
    'missing_frame', 'missing_receipt', 'missing_start_receipt', 'invalid_hash',
])
def test_insufficient_exit_evidence_keeps_original_unknown(plan, pending, case):
    journal, intent, confirmation, evidence, _ = pending
    confirmation, evidence = deepcopy(confirmation), deepcopy(evidence)
    if case == 'no_confirmation': confirmation = None
    elif case == 'configuration_source': confirmation['source'] = 'task_json'
    elif case == 'not_exited': confirmation['exited'] = False
    elif case == 'no_continuation': confirmation['continue_remaining'] = False
    elif case == 'no_text': confirmation['text'] = ''
    elif case == 'no_evidence': evidence = None
    elif case == 'wrong_scene': evidence['scene'] = 'battle'
    elif case == 'not_verified': evidence['verified'] = False
    elif case == 'wrong_intent': evidence['intent_id'] = '0' * 32
    elif case == 'start_unsent': evidence['start_sent'] = False
    elif case == 'old_observation': evidence['observed_at'] = intent['at'] - 1
    elif case == 'missing_frame': evidence.pop('frame_sha256')
    elif case == 'missing_receipt': evidence.pop('receipt_sha256')
    elif case == 'missing_start_receipt': evidence.pop('start_receipt_sha256')
    elif case == 'invalid_hash': evidence['frame_sha256'] = 'x' * 64
    before = journal.path.read_bytes()
    with pytest.raises(ValueError, match='exit_evidence_invalid'):
        clan_battle.record_simulation_user_exit(journal, intent_id=intent['intent_id'],
            confirmation=confirmation, evidence=evidence)
    assert journal.path.read_bytes() == before and journal.events() == [intent]
    again = Port(plan)
    assert clan_battle.execute_simulation(again, journal, plan)['error'] == 'clan_battle_previous_result_unknown'
    assert again.prepared == again.starts == 0


def test_wrong_intent_or_duplicate_never_appends(pending):
    journal, intent, confirmation, evidence, _ = pending
    with pytest.raises(ValueError, match='exit_target_not_pending'):
        clan_battle.record_simulation_user_exit(journal, intent_id='0' * 32,
            confirmation=confirmation, evidence=evidence)
    clan_battle.record_simulation_user_exit(journal, intent_id=intent['intent_id'],
        confirmation=confirmation, evidence=evidence)
    before = journal.path.read_bytes()
    with pytest.raises(ValueError, match='exit_target_not_pending'):
        clan_battle.record_simulation_user_exit(journal, intent_id=intent['intent_id'],
            confirmation=confirmation, evidence=evidence)
    assert journal.path.read_bytes() == before


def test_tampered_exit_history_is_not_accepted_by_shared_journal(pending):
    journal, intent, confirmation, evidence, _ = pending
    event = clan_battle.record_simulation_user_exit(journal, intent_id=intent['intent_id'],
        confirmation=confirmation, evidence=evidence)
    for patch in (dict(intent_id='0' * 32), dict(original_result_unknown=False),
                  dict(replay_forbidden=False), dict(damage=0), dict(simulation_start_sent=False),
                  dict(settings_verified=True)):
        tampered = dict(event, **patch)
        assert not clan_battle.is_simulation_user_exit_history(tampered, intent)
        with pytest.raises(ValueError, match='Journal sequence invalid'):
            Journal._validate_event(tampered, 1, intent)


def test_task_json_cannot_carry_exit_confirmation(plan):
    plan['user_confirmed_exited'] = True
    with pytest.raises(ValueError):
        clan_battle.validate_plan(bind(plan))
