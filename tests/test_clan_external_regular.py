"""Synthetic external-use facts never substitute for program settlement or input authority."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from pcr_maa import alpha_owner, clan_battle_batch as batch
from pcr_maa.clan_battle_daily_native import NativeClanDailyAdapter
from pcr_maa.preset_execution import Journal
from test_clan_battle_batch import daily_request, run, ledgers, Port
from test_clan_consumption_consent import ConsentPort, approval, transport, preview_value
from test_clan_regular_empty_inventory import (live_observation, strict_regular, regular_only, plan,
    executable, calibration, calibration_v2, profile, bundle, v2_bundle, daily_bundle, daily_execution_bundle)


class ExternalPort(ConsentPort):
    def __init__(self, request):
        super().__init__(request)
        self.remaining = 2
        self.external_fault = None
        self.baselines = {}

    def observe(self, binding):
        row = super().observe(binding)
        row['reset_evidence']['inventory'] = {'source': 'synthetic_actual_empty_inventory'}
        return row

    def bind_history(self, binding, regular, *, external=None):
        self.baselines = deepcopy(external or {})
        self.used = [uid for row in self.baselines.values() for uid in row['unit_ids']]
        self.used += [uid for row in regular.values() for uid in row['intent']['ready']['unit_ids']]

    def request_consumption_consent(self, plan_hash, preview):
        value = super().request_consumption_consent(plan_hash, preview)
        if preview['kind'] == 'external_regular_baseline' and value:
            value['selected_slot'] = 3
            if self.external_fault == 'deny': return None
            if self.external_fault == 'wrong_slot': value['selected_slot'] = 4
            if self.external_fault == 'boolean_slot': value['selected_slot'] = True
            if self.external_fault == 'changed_count': self.remaining = 1
            if self.external_fault == 'changed_cp': self.cp = 600
            if self.external_fault == 'new_stock': self.inventory = [0]
            if self.external_fault == 'changed_target': self.rounds[1] += 1
            if self.external_fault == 'changed_source': self.fingerprint['source_sha256'] = 'e'*64
            if self.external_fault == 'changed_owner': self.fingerprint['authorization_generation'] = 'f'*32
            if self.external_fault == 'changed_day': self.clock += timedelta(days=1)
        return value


def test_external_fact_then_two_program_attacks_are_separately_counted_and_replayable(tmp_path, daily_request):
    port = ExternalPort(daily_request)
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete', result
    assert result['regular_settled'] == 2 and result['external_regular_observed'] == 1
    assert [row['action']['slot'] for row in port.executions] == [1, 2]
    assert [row['kind'] for row in port.confirmations] == ['external_regular_baseline', 'regular']
    assert [row['slot'] for row in port.confirmations[1]['actions']] == [1, 2]
    events = Journal(ledgers(tmp_path)[0].parent).events()
    assert [row['state'] for row in events] == ['observed_external', 'in_flight', 'settled', 'in_flight', 'settled']
    fact = events[0]
    assert fact['operation'] == 'clan_battle_external_regular' and fact['slot'] == 3
    assert not {'intent_id', 'ready', 'damage', 'result', 'grant'} & fact.keys()
    assert fact['unit_ids'] == daily_request['slot_plans']['3']['unit_ids']
    again, _ = run(tmp_path, daily_request, port)
    assert again['outcome'] == 'complete' and again['regular_settled'] == 2
    assert again['external_regular_observed'] == 1 and len(port.executions) == 2


@pytest.mark.parametrize('fault', ['deny', 'wrong_slot', 'boolean_slot', 'changed_count', 'changed_cp',
    'new_stock', 'changed_target', 'changed_source', 'changed_owner', 'changed_day'])
def test_unconfirmed_or_changed_baseline_writes_no_history_and_sends_nothing(tmp_path, daily_request, fault):
    port = ExternalPort(daily_request)
    port.external_fault = fault
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'stopped' and result.get('error'), result
    assert not result['consumption_sent'] and not port.executions
    assert not ledgers(tmp_path) and not list(tmp_path.rglob('batch.json'))


@pytest.mark.parametrize('remaining,stocks', [(1, []), (2, [0]), (0, [])])
def test_only_one_initial_external_use_with_empty_inventory_is_eligible(tmp_path, daily_request, remaining, stocks):
    port = ExternalPort(daily_request)
    port.remaining, port.inventory = remaining, stocks
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'stopped' and not port.confirmations
    assert not port.executions and not ledgers(tmp_path)


def test_no_owner_callback_still_rejects_external_consumption(tmp_path, daily_request):
    port = Port(daily_request)
    port.remaining = 2
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_remaining_count_history_mismatch'
    assert not port.executions and not ledgers(tmp_path)


def test_unknown_program_intent_after_baseline_never_repeats_or_reconfirms(tmp_path, daily_request):
    port = ExternalPort(daily_request)
    port.fail = 'unknown_after_start'
    first, _ = run(tmp_path, daily_request, port)
    assert first['error'] == 'unknown_after_start' and first['external_regular_observed'] == 1
    assert first['regular_settled'] == 0 and len(port.executions) == 1
    events = Journal(ledgers(tmp_path)[0].parent).events()
    assert [row['state'] for row in events] == ['observed_external', 'in_flight']
    second, _ = run(tmp_path, daily_request, port)
    assert second['error'] == 'daily_previous_result_unknown'
    assert len(port.executions) == 1 and len(port.confirmations) == 2


def test_baseline_fact_is_not_a_normal_consumption_permit(tmp_path, daily_request):
    port = ExternalPort(daily_request)
    original = port.request_consumption_consent
    def consent(plan_hash, preview):
        if preview['kind'] == 'regular': return None
        return original(plan_hash, preview)
    port.request_consumption_consent = consent
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_consumption_not_authorized'
    assert result['external_regular_observed'] == 1 and result['regular_settled'] == 0
    assert not port.executions
    assert [row['state'] for row in Journal(ledgers(tmp_path)[0].parent).events()] == ['observed_external']


def test_journal_leading_fact_does_not_relax_other_operation_or_pair_sequences(tmp_path, daily_request):
    port = ExternalPort(daily_request)
    original = port.request_consumption_consent
    port.request_consumption_consent = lambda plan_hash, preview: original(plan_hash, preview) if preview['kind'] == 'external_regular_baseline' else None
    run(tmp_path, daily_request, port)
    journal = Journal(ledgers(tmp_path)[0].parent)
    fact = journal.events()[0]
    bad_reset = deepcopy(fact)
    bad_reset['after']['reset_verified'] = False
    with pytest.raises(ValueError, match='baseline_invalid'):
        batch.validate_external_regular_event(bad_reset, daily_request)
    with journal.locked():
        with pytest.raises(ValueError, match='sequence'):
            journal.append(fact)
        with pytest.raises(ValueError, match='sequence'):
            journal.append(dict(state='settled', operation='synthetic'))
        intent = dict(state='in_flight', operation='synthetic')
        journal.append(intent)
        with pytest.raises(ValueError, match='sequence'):
            journal.append(dict(state='in_flight', operation='synthetic'))
        journal.append(dict(intent, state='settled'))
    assert [row['state'] for row in journal.events()] == ['observed_external', 'in_flight', 'settled']
    other = Journal(tmp_path / 'other')
    with other.locked():
        with pytest.raises(ValueError, match='sequence'):
            other.append(dict(fact, operation='preset_external'))
        with pytest.raises(ValueError):
            other.append(dict(fact, confirmation={'ok': True}))


def test_native_default_observation_still_rejects_gap_but_owner_fact_path_requires_real_empty_proof(live_observation):
    port, binding, _, _, _, lobby, _ = live_observation
    lobby['remaining_count'] = 2
    with pytest.raises(RuntimeError, match='count_or_inventory_changed'):
        port.observe(binding)
    port.consumption_consent, port.owner_core_sha256 = lambda *_: None, 'a'*64
    port.consumption_binding = lambda: {'synthetic_owner': True}
    observed = port.observe_external_baseline(binding)
    assert observed['remaining_count'] == 2 and observed['used_unit'] == []
    assert observed['reset_evidence']['consumption_history']['external_regular_unconfirmed'] is True
    assert port._external_baseline_observing is False
    with pytest.raises(RuntimeError, match='count_or_inventory_changed'):
        port.observe(binding)
    port.bind_history(binding, {}, external={3: dict(unit_ids=[11,12,13,14,15])})
    observed = port.observe(binding)
    assert observed['used_unit'] == [11,12,13,14,15]
    assert observed['reset_evidence']['consumption_history']['settled_regular_intents'] == []
    assert observed['reset_evidence']['consumption_history']['external_regular_observed_slots'] == [3]


@pytest.mark.parametrize('fault', ['owner', 'pending', 'inventory', 'unproven'])
def test_native_fact_path_does_not_accept_unknown_owner_pending_or_stock(live_observation, fault):
    port, binding, _, _, _, lobby, empty = live_observation
    lobby['remaining_count'] = 2
    port.consumption_consent, port.owner_core_sha256 = lambda *_: None, 'a'*64
    port.consumption_binding = lambda: {'synthetic_owner': True}
    if fault == 'owner': port.consumption_consent = None
    elif fault == 'pending': port.pending = {'intent_id': 'unknown'}
    elif fault == 'inventory': lobby['carry_count'] = 1
    elif fault == 'unproven': empty['evidence']['frame_pixel_sha256'] = 'invalid'
    with pytest.raises((RuntimeError, ValueError)):
        port.observe_external_baseline(binding)


def test_external_preview_is_three_regular_choices_not_three_consumption_permissions():
    preview = preview_value()
    preview.update(kind='external_regular_baseline', remaining_count=2, max_count=1)
    preview['actions'] = [dict(preview['actions'][0], slot=slot) for slot in (1,2,3)]
    assert alpha_owner.validate_clan_preview(preview)
    preview['actions'][0]['kind'] = 'external_regular_baseline'
    with pytest.raises(ValueError): alpha_owner.validate_clan_preview(preview)


@pytest.mark.parametrize('selected', [3, None, True, 0, 4])
def test_live_owner_external_reply_requires_one_exact_selected_slot(selected):
    provider, grant, context, incoming, sent = transport(selected_slot=selected)
    try:
        preview = preview_value()
        preview.update(kind='external_regular_baseline', remaining_count=2, max_count=1)
        preview['actions'] = [dict(preview['actions'][0], slot=slot) for slot in (1,2,3)]
        if selected == 3:
            value = grant.clan_consumption_consent('f'*64, preview)
            assert value['selected_slot'] == 3 and value['ok'] is True
            assert grant.observation_source(context) is True
        else:
            with pytest.raises(RuntimeError): grant.clan_consumption_consent('f'*64, preview)
        assert sum(line.startswith(alpha_owner.CLAN_PREFIX) for line in sent) == 1
    finally:
        incoming.put('')


def test_regular_permission_cannot_carry_external_slot_choice():
    provider, grant, _, incoming, _ = transport(selected_slot=3)
    try:
        with pytest.raises(RuntimeError, match='reply_binding_mismatch'):
            grant.clan_consumption_consent('f'*64, preview_value())
    finally:
        incoming.put('')
