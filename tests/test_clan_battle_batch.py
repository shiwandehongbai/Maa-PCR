"""Synthetic batch ports only: no formal battle, device, account or live-rule proof."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess

import pytest

from pcr_maa import clan_battle_batch as batch
from pcr_maa.clan_battle import plan_hash
from pcr_maa.clan_battle_daily import POLICY
from pcr_maa.preset_execution import Journal

NOW = datetime.now(timezone.utc)


def bind(value):
    value['confirmed_for'] = batch.request_hash(value)
    return value


@pytest.fixture
def daily_request(tmp_path):
    image = tmp_path / 'synthetic-source.png'
    image.write_bytes(b'SYNTHETIC ONLY - never a game frame')
    snapshot = tmp_path / 'synthetic-snapshot.json'
    snapshot.write_text(json.dumps(dict(schema_version=1, server='cn-bilibili', account_id='123456789',
        content_version='202610141431', observed_at=datetime.now(timezone.utc).isoformat(),
        ownership_complete=True, units=[dict(unit_id=slot * 100 + offset, battle_rarity=5)
            for slot in (1, 2, 3) for offset in range(1, 6)], unknown_fields=[])), encoding='utf-8')
    plans, materials = {}, {}
    for slot in (1, 2, 3):
        ids = [slot * 100 + offset for offset in range(1, 6)]
        job = dict(version=1, server='cn-bilibili', account_id='123456789', content_version='202610141431',
            period='2026-10', phase='BC', round_min=1, round_max=10, boss=slot, boss_name='',
            package='合成套餐', team_label=f'合成队伍{slot}', unit_ids=ids, stars=[5]*5,
            auto=True, set_by_unit={str(uid): True for uid in ids}, switches=[], support_mode='none',
            support_unit_id=None, reference_damage=None,
            source=dict(url='https://www.bilibili.com/video/BV1Rbh86XEsq/', bvid='BV1Rbh86XEsq',
                page=1, cid=42138340822, at_seconds=slot, image_path=str(image),
                image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(), job_id=f'synthetic-{slot}'),
            snapshot=dict(path=str(snapshot), sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest()))
        job['confirmed_for'] = plan_hash(job)
        plans[str(slot)] = job
        materials[str(slot)] = {key: deepcopy(job[key]) for key in batch.MATERIAL_KEYS}
    bundle = tmp_path / 'synthetic-bundle.json'
    bundle.write_text(json.dumps(dict(jobs=list(materials.values()))), encoding='utf-8')
    refs = [dict(slot=slot, bundle_path=str(bundle), bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        job_id=plans[str(slot)]['source']['job_id'], material_sha256=batch._hash(materials[str(slot)]),
        confirmed_for=plans[str(slot)]['confirmed_for']) for slot in (1, 2, 3)]
    return bind(dict(version=1, daily_plan=dict(version=1, policy=POLICY, slots=refs),
        slot_plans=plans, slot_materials=materials))


def rebind(value):
    """Update synthetic reviewed material after a deliberate fixture-only edit."""
    for ref in value['daily_plan']['slots']:
        key = str(ref['slot'])
        job = value['slot_plans'][key]
        job['confirmed_for'] = plan_hash(job)
        value['slot_materials'][key] = {field: deepcopy(job[field]) for field in batch.MATERIAL_KEYS}
        ref.update(confirmed_for=job['confirmed_for'], material_sha256=batch._hash(value['slot_materials'][key]))
    return bind(value)


class Port:
    timing_capability = dict(validated=True, max_observation_interval_ms=25, max_action_latency_ms=30,
        source='synthetic-only', battle_speed=1, min_game_second_ms=1000)

    def __init__(self, request, *, now=NOW):
        self.request = request
        self.clock = now
        self.executions, self.prepared, self.observations = [], [], []
        self.remaining, self.used, self.inventory = 3, [], []
        self.cp = 900
        self.available = {slot: True for slot in (1, 2, 3)}
        self.rounds = {slot: 4 for slot in (1, 2, 3)}
        self.instances = {slot: (f'synthetic-instance-{slot}-original', now - timedelta(seconds=30)) for slot in (1, 2, 3)}
        self.issue = set()
        self.grant_time = 34
        self.refresh = True
        self.fail = None
        self.result_change = None
        self.prepare_change = None
        self.reset_verified = True
        self.checks = 0

    def now(self):
        return self.clock

    def checkpoint(self):
        self.checks += 1
        if self.fail == 'stop_before':
            raise RuntimeError('stopped')

    def observe(self, binding):
        self.binding = binding
        row = dict(deepcopy(binding), observed_at=self.clock.isoformat(), remaining_count=self.remaining,
            cp=self.cp, cp_semantics='daily_earned', cp_evidence={'source': 'synthetic_cp_readback'},
            used_unit=list(self.used), carry_stock_indices=list(self.inventory), reset_verified=self.reset_verified,
            reset_evidence={'source': 'synthetic_reset_readback'} if self.reset_verified else {},
            bosses=[dict(boss=slot, round=self.rounds[slot], phase='B', available=self.available[slot],
                instance_id=self.instances[slot][0], appeared_at=self.instances[slot][1].isoformat(),
                evidence={'source': 'synthetic_boss_page'}) for slot in (1, 2, 3)])
        self.observations.append(deepcopy(row))
        return row

    def prepare(self, action, job, binding):
        self.prepared.append(deepcopy(action))
        support = batch.simulation.support_requirement(job, batch.simulation.load_snapshot(job))
        ready = dict(deepcopy(binding), **action, slot_confirmed_for=job['confirmed_for'], verified=True,
            unit_ids=deepcopy(job['unit_ids']), stars=deepcopy(job['stars']), support_unit=support,
            support_owner_viewer_id='987654321' if support else None, evidence={'source': 'synthetic_team_readback'})
        if self.prepare_change:
            self.prepare_change(ready, self)
        return ready

    def execute(self, intent, job):
        self.executions.append(deepcopy(intent))
        if self.fail:
            raise RuntimeError(self.fail)
        action = intent['action']
        self.clock += timedelta(seconds=1)
        grant = None
        if action['kind'] == 'regular':
            self.remaining -= 1
            self.used.extend(uid for uid in job['unit_ids'] if uid != intent['ready']['support_unit'])
            if action['slot'] in self.issue:
                stock = action['slot'] - 1
                self.inventory.append(stock)
                grant = dict(deepcopy(self.binding), slot_confirmed_for=job['confirmed_for'], source_slot=action['slot'],
                    source_intent_id=intent['intent_id'], stock_index=stock, time=self.grant_time,
                    using_unit=deepcopy(job['unit_ids']), support_unit=intent['ready']['support_unit'],
                    support_owner_viewer_id=intent['ready']['support_owner_viewer_id'], source_instance_id=action['instance_id'],
                    issued_at=self.clock.isoformat(), evidence={'source': 'synthetic_actual_stock_page'})
                if self.refresh:
                    self.clock += timedelta(seconds=1)
                    self.rounds[action['boss']] += 1
                    self.instances[action['boss']] = (f"synthetic-instance-{action['boss']}-new", self.clock)
        else:
            self.inventory.remove(action['stock_index'])
        state = dict(auto=job['auto'], unit_ids=deepcopy(job['unit_ids']),
            sets=[job['set_by_unit'][str(uid)] for uid in job['unit_ids']], battle_speed=1)
        settings = dict(settings_verified=True, auto=job['auto'], set_by_unit=deepcopy(job['set_by_unit']),
            opening_evidence=deepcopy(state), observation=deepcopy(state), actions=[], switch_evidence=[])
        for switch in job['switches']:
            before = deepcopy(state)
            state['auto'] = state['auto'] if switch['auto'] is None else switch['auto']
            values = dict(zip(state['unit_ids'], state['sets']))
            values.update({int(key): item for key, item in switch['set_by_unit'].items()})
            state['sets'] = [values[uid] for uid in state['unit_ids']]
            settings['switch_evidence'].append(dict(remaining_seconds=switch['remaining_seconds'],
                confirmed_remaining_seconds=switch['remaining_seconds'], before=before, after=deepcopy(state)))
        settings.update(auto=state['auto'], set_by_unit={str(uid): setting for uid, setting in zip(state['unit_ids'], state['sets'])}, observation=state)
        result = dict(ready=deepcopy(intent['ready']), result_verified=True, consumption_verified=True,
            damage=12345678, settings_verified=True, settings_evidence=settings, result_evidence={'source': 'synthetic_formal_result'},
            grant=grant, after=self.observe(deepcopy(self.binding)), return_verified=True)
        if self.result_change:
            self.result_change(result, self)
        return result


def run(tmp_path, request, port=None, *, now=NOW):
    port = port or Port(request, now=now)
    result = batch.execute_batch(port, Journal(tmp_path / 'daily-ledger'), request, now=now, explicit_start=True)
    return result, port


def ledgers(tmp_path):
    return list((tmp_path / 'daily-ledger').rglob('transactions.jsonl'))


def test_strict_request_reuses_review_and_raw_material(daily_request):
    original = deepcopy(daily_request)
    assert batch.validate_daily_request(daily_request) == original
    assert daily_request == original


@pytest.mark.parametrize('field', ['enabled', 'port', 'calibration_path', 'remaining_count'])
def test_request_has_no_json_execution_or_calibration_override(daily_request, field):
    daily_request[field] = True
    with pytest.raises(ValueError, match='daily_request_invalid'):
        batch.validate_daily_request(bind(daily_request))


@pytest.mark.parametrize('field', ['bundle_sha256', 'job_id', 'material_sha256', 'confirmed_for'])
def test_original_reference_changes_are_rejected(daily_request, field):
    daily_request['daily_plan']['slots'][0][field] = 'b' * 64
    with pytest.raises(ValueError):
        batch.validate_daily_request(bind(daily_request))


def test_raw_unknown_star_resolves_only_from_known_bound_snapshot(daily_request):
    daily_request['slot_materials']['1']['stars'][0] = None
    daily_request['daily_plan']['slots'][0]['material_sha256'] = batch._hash(daily_request['slot_materials']['1'])
    assert batch.validate_daily_request(bind(daily_request))
    daily_request['slot_materials']['1']['stars'][0] = True
    daily_request['daily_plan']['slots'][0]['material_sha256'] = batch._hash(daily_request['slot_materials']['1'])
    with pytest.raises(ValueError, match='material_stars_invalid'):
        batch.validate_daily_request(bind(daily_request))


def test_original_source_not_present_in_bound_bundle_is_rejected(daily_request):
    daily_request['slot_plans']['1']['source']['at_seconds'] = 999
    rebind(daily_request)
    with pytest.raises(ValueError, match='bundle_source_changed'):
        batch.validate_daily_request(daily_request)


def test_shared_owned_characters_cannot_form_a_formal_three_target_plan(daily_request):
    job = daily_request['slot_plans']['2']
    job['unit_ids'][0] = daily_request['slot_plans']['1']['unit_ids'][0]
    job['set_by_unit'] = {str(uid): True for uid in job['unit_ids']}
    with pytest.raises(ValueError, match='self_units_repeated'):
        batch.validate_daily_request(rebind(daily_request))


def test_one_start_settles_three_once_with_real_journal(tmp_path, daily_request):
    result, port = run(tmp_path, daily_request)
    assert result['outcome'] == 'complete' and result['regular_settled'] == 3
    assert result['remaining_count'] == 0
    assert [(row['action']['kind'], row['action']['slot']) for row in port.executions] == [('regular', 1), ('regular', 2), ('regular', 3)]
    rows = Journal(ledgers(tmp_path)[0].parent).events()
    assert len(rows) == 6 and all(row['daily_plan_hash'] == daily_request['confirmed_for'] for row in rows)
    assert all(row['slot_confirmed_for'] == daily_request['slot_plans'][str(row['action']['slot'])]['confirmed_for'] for row in rows)
    before = ledgers(tmp_path)[0].read_bytes()
    again, _ = run(tmp_path, daily_request, port)
    assert again['outcome'] == 'complete' and len(port.executions) == 3
    assert ledgers(tmp_path)[0].read_bytes() == before


def test_unavailable_first_regular_is_held_and_other_targets_scanned(tmp_path, daily_request):
    port = Port(daily_request)
    port.available[1] = False
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'no_available_target' and result['regular_settled'] == 2
    assert result['remaining_count'] == 1
    assert result['held_regular'] == [dict(slot=1, boss=1, reason='regular_target_not_available_for_job')]
    assert [row['action']['slot'] for row in port.executions] == [2, 3]
    port.available[1] = True
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete'
    assert [row['action']['slot'] for row in port.executions] == [2, 3, 1]


def test_all_unavailable_ends_without_wait_intent_or_frozen_plan(tmp_path, daily_request):
    port = Port(daily_request)
    port.available = {slot: False for slot in (1, 2, 3)}
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'no_available_target' and not port.executions
    assert result['remaining_count'] == 3
    assert not ledgers(tmp_path) and not list((tmp_path / 'daily-ledger').rglob('batch.json'))
    assert len(port.observations) == 1


def test_missing_target_observation_is_unknown_not_unavailable(tmp_path, daily_request):
    port = Port(daily_request)
    original = port.observe
    def missing(binding):
        row = original(binding)
        row['bosses'].pop(0)
        return row
    port.observe = missing
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_target_observation_unknown' and not port.executions


def test_carryover_uses_actual_new_instance_before_next_regular(tmp_path, daily_request):
    port = Port(daily_request)
    port.issue = {1}
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete' and result['carryover_settled'] == 1
    assert [(row['action']['kind'], row['action']['slot']) for row in port.executions] == [('regular', 1), ('carryover', 1), ('regular', 2), ('regular', 3)]
    assert port.executions[1]['action']['start_seconds'] == 34
    assert port.executions[1]['action']['round'] > port.executions[0]['action']['round']


@pytest.mark.parametrize('fault', ['same_instance', 'appeared_before_issue', 'unavailable'])
def test_round_increase_alone_never_proves_new_carry_target(tmp_path, daily_request, fault):
    port = Port(daily_request)
    port.issue = {1}
    original = port.observe
    def changed(binding):
        row = original(binding)
        if port.remaining < 3:
            row['bosses'][0]['round'] = 5
            if fault == 'same_instance':
                row['bosses'][0]['instance_id'] = 'synthetic-instance-1-original'
            elif fault == 'appeared_before_issue':
                row['bosses'][0]['appeared_at'] = (NOW - timedelta(seconds=10)).isoformat()
            else:
                row['bosses'][0]['available'] = False
        return row
    port.observe = changed
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'remind_carryover' and result['regular_settled'] == 3
    assert result['remaining_count'] == 0
    assert [row['action']['kind'] for row in port.executions] == ['regular'] * 3
    assert result['held_carryovers'][0]['reason'] == 'same_boss_new_appearance_not_observed'


def test_held_carry_can_resume_after_main_targets_complete(tmp_path, daily_request):
    port = Port(daily_request)
    port.issue, port.refresh = {1}, False
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'remind_carryover'
    assert result['remaining_count'] == 0
    port.clock += timedelta(seconds=1)
    port.instances[1] = ('synthetic-later-instance', port.clock)
    port.rounds[1] += 1
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete' and len(port.executions) == 4


def test_carry_priority_is_source_slot_then_stock_not_grant_insertion(tmp_path, daily_request):
    port = Port(daily_request)
    port.available[1] = False
    port.issue, port.refresh = {1, 2}, False
    run(tmp_path, daily_request, port)
    port.available[1] = True
    run(tmp_path, daily_request, port)
    port.clock += timedelta(seconds=1)
    port.instances[1] = ('later-one', port.clock)
    port.instances[2] = ('later-two', port.clock)
    port.rounds[1] += 1
    port.rounds[2] += 1
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete'
    assert [row['action']['slot'] for row in port.executions if row['action']['kind'] == 'carryover'] == [1, 2]


@pytest.mark.parametrize('fail', ['stopped', '6002', 'result_unknown'])
def test_unknown_intent_never_replays_same_day(tmp_path, daily_request, fail):
    port = Port(daily_request)
    port.fail = fail
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == fail and len(port.executions) == 1
    before = ledgers(tmp_path)[0].read_bytes()
    again, _ = run(tmp_path, daily_request, Port(daily_request))
    assert again['error'] == 'daily_previous_result_unknown'
    assert ledgers(tmp_path)[0].read_bytes() == before


def test_same_day_frozen_plan_cannot_be_replaced_after_consumption(tmp_path, daily_request):
    run(tmp_path, daily_request)
    daily_request['slot_plans']['1']['team_label'] = 'new synthetic label'
    rebind(daily_request)
    port = Port(daily_request)
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_consumed_batch_plan_immutable'
    assert not port.observations and not port.prepared and not port.executions


def test_new_game_day_requires_explicit_start_and_actual_reset_preserves_old_unknown(tmp_path, daily_request):
    old = Port(daily_request)
    old.fail = '6002'
    run(tmp_path, daily_request, old)
    old_path = ledgers(tmp_path)[0]
    old_bytes = old_path.read_bytes()
    tomorrow = NOW + timedelta(days=1)
    new = Port(daily_request, now=tomorrow)
    result = batch.execute_batch(new, Journal(tmp_path / 'daily-ledger'), daily_request, now=tomorrow)
    assert result['error'] == 'daily_explicit_start_required' and not new.observations
    new.reset_verified = False
    result, _ = run(tmp_path, daily_request, new, now=tomorrow)
    assert result['error'] == 'daily_actual_reset_readback_required'
    new.reset_verified = True
    result, _ = run(tmp_path, daily_request, new, now=tomorrow)
    assert result['outcome'] == 'complete'
    assert old_path.read_bytes() == old_bytes and len(ledgers(tmp_path)) == 2


@pytest.mark.parametrize('fault', ['remaining', 'used', 'stock'])
def test_external_manual_consumption_or_inventory_is_not_adopted(tmp_path, daily_request, fault):
    port = Port(daily_request)
    if fault == 'remaining':
        port.remaining = 2
    elif fault == 'used':
        port.used = [999]
    else:
        port.inventory = [99]
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'stopped' and 'history_mismatch' in result['error']
    assert not port.prepared and not port.executions and not ledgers(tmp_path)


@pytest.mark.parametrize('fault', ['batch_id', 'daily_plan_hash', 'account_id', 'period', 'game_day', 'slot_confirmations'])
def test_every_observation_requires_full_batch_binding(tmp_path, daily_request, fault):
    port = Port(daily_request)
    original = port.observe
    def bad(binding):
        row = original(binding)
        row[fault] = 'unbound'
        return row
    port.observe = bad
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_batch_binding_mismatch' and not port.executions


@pytest.mark.parametrize('fault', ['consumption', 'settings', 'damage', 'result_evidence', 'after_count', 'stock_inventory', 'grant_binding', 'recursive_grant'])
def test_bad_result_never_settles_and_blocks_future_start(tmp_path, daily_request, fault):
    port = Port(daily_request)
    if fault in ('stock_inventory', 'grant_binding', 'recursive_grant'):
        port.issue = {1}
    def bad(result, owner):
        if fault == 'consumption':
            result['consumption_verified'] = False
        elif fault == 'settings':
            result['settings_evidence']['opening_evidence']['sets'][0] = False
        elif fault == 'damage':
            result['damage'] = True
        elif fault == 'result_evidence':
            result['result_evidence'] = {}
        elif fault == 'after_count':
            result['after']['remaining_count'] = 0
        elif fault == 'stock_inventory':
            result['after']['carry_stock_indices'] = []
        elif fault == 'grant_binding':
            result['grant']['daily_plan_hash'] = '0' * 64
        elif result['ready']['kind'] == 'carryover':
            result['grant'] = deepcopy(owner.previous_grant)
        else:
            owner.previous_grant = deepcopy(result['grant'])
    port.result_change = bad
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'stopped'
    assert result['remaining_count'] == (2 if fault == 'recursive_grant' else 3)
    assert len(Journal(ledgers(tmp_path)[0].parent).events()) % 2 == 1
    again, _ = run(tmp_path, daily_request)
    assert again['error'] == 'daily_previous_result_unknown'


def test_known_damage_return_failure_is_settled_but_stops_batch(tmp_path, daily_request):
    port = Port(daily_request)
    port.result_change = lambda result, owner: result.update(return_verified=False)
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_return_unverified' and result['regular_settled'] == 1
    assert result['remaining_count'] == 2
    assert result['attempts'][0]['damage'] == 12345678
    assert len(Journal(ledgers(tmp_path)[0].parent).events()) == 2
    port.result_change = None
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete'
    assert [row['action']['slot'] for row in port.executions] == [1, 2, 3]
    assert [row['slot'] for row in port.prepared] == [1, 1, 2, 2, 3, 3]


@pytest.mark.parametrize('boundary,error', [('checkpoint', 'stopped'), ('observe', 'synthetic_observation_failure')])
def test_interruption_after_settlement_preserves_verified_remaining_count(tmp_path, daily_request, boundary, error):
    port = Port(daily_request)
    def interrupt_after_result(result, owner):
        def fail(*args):
            raise RuntimeError(error)
        setattr(owner, boundary, fail)
    port.result_change = interrupt_after_result
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == error and result['regular_settled'] == 1
    assert result['remaining_count'] == 2
    assert len(result['attempts']) == 1 and result['attempts'][0]['damage'] == 12345678
    assert len(port.executions) == 1 and len(Journal(ledgers(tmp_path)[0].parent).events()) == 2


@pytest.mark.parametrize('seconds', [-100, -1, 1, 100])
def test_result_observation_cannot_be_stale_backwards_or_future(tmp_path, daily_request, seconds):
    port = Port(daily_request)
    port.result_change = lambda result, owner: result['after'].update(observed_at=(NOW + timedelta(seconds=seconds)).isoformat())
    if seconds == 1:
        # The port advanced one second: move the result another second into its future.
        port.result_change = lambda result, owner: result['after'].update(observed_at=(owner.clock + timedelta(seconds=1)).isoformat())
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'stopped' and len(port.executions) == 1
    assert len(Journal(ledgers(tmp_path)[0].parent).events()) == 1


@pytest.mark.parametrize('field', ['intent_id', 'slot_confirmed_for', 'daily_plan_hash', 'operation'])
def test_tampered_history_cannot_authorize_resumption(tmp_path, daily_request, field):
    run(tmp_path, daily_request)
    path = ledgers(tmp_path)[0]
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    for row in rows[:2]:
        row[field] = '0' * 64
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    port = Port(daily_request)
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'stopped'
    assert 'ledger' in result['error'] or result['error'] == 'daily_batch_binding_mismatch'
    assert not port.observations and not port.executions


def test_actual_team_order_is_identity_mapped_not_position_assumed(tmp_path, daily_request):
    port = Port(daily_request)
    def reverse(ready, owner):
        ready['unit_ids'].reverse()
        ready['stars'].reverse()
    port.prepare_change = reverse
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete'


def test_carry_history_keeps_the_actual_source_support_owner(tmp_path, daily_request):
    job = daily_request['slot_plans']['1']
    job.update(support_mode='specified', support_unit_id=job['unit_ids'][0])
    rebind(daily_request)
    port = Port(daily_request)
    port.issue = {1}
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete'
    path = ledgers(tmp_path)[0]
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    for row in rows[2:4]:
        row['ready']['support_owner_viewer_id'] = '888888888'
        if 'result' in row:
            row['result']['ready']['support_owner_viewer_id'] = '888888888'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    result, _ = run(tmp_path, daily_request, Port(daily_request))
    assert result['error'] == 'daily_carryover_actual_team_mismatch'


def test_recovery_requires_initial_actual_reset_proof_not_just_state_pairs(tmp_path, daily_request):
    run(tmp_path, daily_request)
    path = ledgers(tmp_path)[0]
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    for row in rows[:2]:
        row['before']['reset_verified'] = False
        row['before']['reset_evidence'] = {}
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    port = Port(daily_request)
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_actual_reset_readback_required'
    assert not port.observations and not port.executions


def test_stop_before_prepare_creates_no_intent(tmp_path, daily_request):
    port = Port(daily_request)
    port.fail = 'stop_before'
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'stopped' and not port.executions and not ledgers(tmp_path)


def test_target_changed_after_prepare_stops_before_intent(tmp_path, daily_request):
    port = Port(daily_request)
    port.prepare_change = lambda ready, owner: owner.instances.update({1: ('changed-before-input', owner.clock)})
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_target_changed_before_input' and not port.executions and not ledgers(tmp_path)


def test_final_inventory_observation_is_followed_by_real_formation_reentry(tmp_path, daily_request):
    port = Port(daily_request)
    observe, prepare, execute = port.observe, port.prepare, port.execute
    def observed(binding):
        port.on_formation = False
        return observe(binding)
    def prepared(*args):
        value = prepare(*args)
        port.on_formation = True
        value['evidence']['reading'] = len(port.prepared)
        return value
    def executed(*args):
        assert port.on_formation is True
        assert args[0]['ready']['evidence']['reading'] == len(port.prepared)
        return execute(*args)
    port.observe, port.prepare, port.execute = observed, prepared, executed
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete'
    assert len(port.prepared) == 6


@pytest.mark.parametrize('change,error', [
    ('team', 'daily_team_or_target_unknown'),
    ('order', 'daily_team_changed_before_input'),
    ('stale', 'daily_fresh_observation_required'),
    ('day', 'daily_fresh_observation_required'),
])
def test_final_formation_recheck_stops_before_intent(tmp_path, daily_request, change, error):
    port = Port(daily_request)
    def changed(ready, owner):
        if len(owner.prepared) != 2:
            return
        if change == 'team':
            ready['unit_ids'][0] = 999901
        elif change == 'order':
            ready['unit_ids'].reverse()
            ready['stars'].reverse()
        else:
            owner.clock += timedelta(seconds=61 if change == 'stale' else 86400)
    port.prepare_change = changed
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == error and not port.executions and not ledgers(tmp_path)


@pytest.mark.parametrize('field', ['slot', 'boss', 'round', 'start_seconds'])
def test_boolean_action_readbacks_cannot_alias_integer_fields(tmp_path, daily_request, field):
    port = Port(daily_request)
    port.prepare_change = lambda ready, owner: ready.update({field: True})
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_action_invalid' and not port.executions and not ledgers(tmp_path)


def test_source_changed_during_prepare_stops_before_intent(tmp_path, daily_request):
    port = Port(daily_request)
    port.prepare_change = lambda ready, owner: Path(daily_request['slot_plans']['1']['source']['image_path']).write_bytes(b'changed synthetic source')
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'clan_battle_source_changed' and not port.executions and not ledgers(tmp_path)


@pytest.mark.parametrize('target', ['image', 'snapshot'])
def test_source_changed_during_final_observation_stops_before_intent(tmp_path, daily_request, target):
    port = Port(daily_request)
    original = port.observe
    def changed(binding):
        row = original(binding)
        if len(port.observations) == 2:
            path = daily_request['slot_plans']['1']['source']['image_path'] if target == 'image' else daily_request['slot_plans']['1']['snapshot']['path']
            Path(path).write_bytes(b'changed synthetic bound file')
        return row
    port.observe = changed
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'clan_battle_source_changed' and not port.executions and not ledgers(tmp_path)


def test_uncalibrated_axis_does_not_create_intent(tmp_path, daily_request):
    daily_request['slot_plans']['1']['switches'] = [dict(remaining_seconds=60, auto=False, set_by_unit={})]
    rebind(daily_request)
    port = Port(daily_request)
    port.timing_capability = {}
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'clan_battle_timing_not_calibrated' and not port.executions and not ledgers(tmp_path)


def test_carry_axis_not_shifted_to_shorter_actual_start_time(tmp_path, daily_request):
    daily_request['slot_plans']['1']['switches'] = [dict(remaining_seconds=60, auto=False, set_by_unit={})]
    rebind(daily_request)
    port = Port(daily_request)
    port.issue = {1}
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'remind_carryover'
    assert result['held_carryovers'][0]['reason'] == 'carryover_axis_start_time_incompatible'
    assert len(port.executions) == 3


def test_no_simulation_or_guide_ledger_is_read_or_written(tmp_path, daily_request):
    original = tmp_path / 'old-guide-ledger' / 'transactions.jsonl'
    original.parent.mkdir()
    original.write_bytes(b'old unknown is preserved byte for byte\n')
    before = original.read_bytes()
    run(tmp_path, daily_request)
    assert original.read_bytes() == before


def test_production_run_owned_always_zero_input_zero_intent(tmp_path, daily_request, monkeypatch):
    class Tasker:
        def __getattr__(self, name):
            raise AssertionError('Production formal path must not use a tasker without real page qualification')
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline test attempted external process/network')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    report = batch.run_owned(Tasker(), {'clan_battle_daily': daily_request}, tmp_path / 'report')
    result = report['steps'][0]['result']
    assert result['error'] == 'clan_battle_calibration_missing'
    assert result['consumption_sent'] is False and result['intents_created'] == 0 and result['daily_execution_available'] is False
    assert 'remaining_count' not in result and not list(tmp_path.rglob('transactions.jsonl'))
    assert json.loads((tmp_path / 'report/report.json').read_text(encoding='utf-8')) == report


@pytest.mark.parametrize('missing', ['formal', 'timed', None])
def test_production_checks_bundled_capabilities_for_every_slot_without_accepting_request_authority(
        tmp_path, daily_request, monkeypatch, missing):
    from pcr_maa import clan_battle_calibration
    checked = []
    def load(job, *, capability):
        checked.append((job['confirmed_for'], capability))
        if missing:
            raise RuntimeError('clan_battle_calibration_capability_missing:' + missing)
        return {'daily': {'read_only': True}}
    monkeypatch.setattr(clan_battle_calibration, 'load_calibration', load)
    result = batch.run_owned(object(), {'clan_battle_daily': daily_request}, tmp_path / 'report')['steps'][0]['result']
    assert checked == [(daily_request['slot_plans']['1']['confirmed_for'], 'formal')]
    assert result['error'] == ('clan_battle_calibration_capability_missing:' + missing if missing else
        'clan_battle_daily_calibration_invalid')
    assert result['consumption_sent'] is False and result['intents_created'] == 0
    assert not list(tmp_path.rglob('transactions.jsonl'))


def test_production_entry_dispatches_reviewed_adapter_through_existing_batch_and_journal(
        tmp_path, daily_request, monkeypatch):
    from pcr_maa import clan_battle_calibration, clan_battle_daily_native, native
    port, checked = Port(daily_request), []
    monkeypatch.setattr(native, 'ROOT', tmp_path)
    def loaded(job, *, capability):
        checked.append((job['confirmed_for'], capability))
        return {'calibration_id': 'synthetic-full-profile'}
    monkeypatch.setattr(clan_battle_calibration, 'load_calibration', loaded)
    monkeypatch.setattr(clan_battle_daily_native, 'require_daily_execution', lambda *args: {})
    def adapter(tasker, directory, *, request, calibrations):
        assert request == daily_request and len(calibrations) == 3
        return port
    monkeypatch.setattr(clan_battle_daily_native, 'NativeClanDailyAdapter', adapter)
    report = batch.run_owned(object(), {'clan_battle_daily': daily_request}, tmp_path / 'report')
    result = report['steps'][0]['result']
    assert report['outcome'] == result['outcome'] == 'complete'
    assert result['daily_execution_available'] is True and result['regular_settled'] == 3
    assert len(checked) == 3 and all(capability == 'formal' for _, capability in checked)
    assert len(list((tmp_path / '.runtime/clan-battle-daily-work').rglob('transactions.jsonl'))) == 1


def test_insufficient_cp_keeps_all_targets_without_creating_an_intent(tmp_path, daily_request):
    port = Port(daily_request)
    port.cp, port.remaining = 299, 0
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'insufficient_cp' and result['cp'] == 299
    assert [row['slot'] for row in result['held_regular']] == [1, 2, 3]
    assert not port.prepared and not port.executions and not ledgers(tmp_path)


def test_rescanning_same_boss_round_cannot_manufacture_a_new_carry_target(tmp_path, daily_request):
    port = Port(daily_request)
    port.issue, port.refresh = {1}, False
    first, _ = run(tmp_path, daily_request, port)
    assert first['outcome'] == 'remind_carryover'
    port.clock += timedelta(seconds=1)
    port.instances[1] = ('invented-from-new-scan-time', port.clock)
    second, _ = run(tmp_path, daily_request, port)
    assert second['outcome'] == 'remind_carryover'
    assert second['held_carryovers'][0]['reason'] == 'same_boss_new_appearance_not_observed'
    assert len(port.executions) == 3 and port.inventory == [0]


@pytest.mark.parametrize('cp,earned', [(0, 0), (299, 0), (300, 1), (599, 1), (600, 2), (900, 3)])
def test_only_earned_challenges_are_executed_without_assuming_three(tmp_path, daily_request, cp, earned):
    port = Port(daily_request)
    port.cp, port.remaining = cp, earned
    result, _ = run(tmp_path, daily_request, port)
    assert result['regular_settled'] == earned and len(port.executions) == earned
    assert result['remaining_count'] == 0 and result['cp'] == cp
    assert result['outcome'] == ('complete' if earned == 3 else 'insufficient_cp')


def test_newly_earned_challenges_resume_remaining_slots_only(tmp_path, daily_request):
    port = Port(daily_request)
    for cp, count in ((300, 1), (600, 2), (900, 3)):
        port.cp, port.remaining = cp, 1
        result, _ = run(tmp_path, daily_request, port)
        assert result['regular_settled'] == count and len(port.executions) == count
    assert result['outcome'] == 'complete'


def test_cp_threshold_change_during_result_is_accounted_separately_from_consumption(tmp_path, daily_request):
    port = Port(daily_request)
    port.cp, port.remaining = 599, 1
    def earn(result, owner):
        if owner.cp == 599:
            owner.cp, owner.remaining = 600, owner.remaining + 1
            result['after'].update(cp=owner.cp, remaining_count=owner.remaining)
    port.result_change = earn
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'insufficient_cp' and result['regular_settled'] == 2


def test_earned_count_does_not_hide_external_manual_consumption(tmp_path, daily_request):
    port = Port(daily_request)
    port.cp, port.remaining = 600, 1
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'daily_remaining_count_history_mismatch' and not port.executions


@pytest.mark.parametrize('fault', ['unknown', 'boolean', 'balance_semantics', 'decreased'])
def test_cp_requires_actual_cumulative_observation(tmp_path, daily_request, fault):
    port = Port(daily_request)
    if fault == 'boolean':
        port.cp = True
    elif fault in ('unknown', 'balance_semantics'):
        observe = port.observe
        def missing(binding):
            row = observe(binding)
            if fault == 'unknown':
                row['cp_evidence'] = {}
            else:
                row['cp_semantics'] = 'available_balance'
            return row
        port.observe = missing
    else:
        port.result_change = lambda result, owner: result['after'].update(cp=899)
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == ('daily_cp_observation_unknown' if fault != 'decreased'
        else 'daily_consumption_readback_mismatch')
    assert result['regular_settled'] == 0
    if fault != 'decreased':
        assert not port.executions and not ledgers(tmp_path)
    else:
        assert len(Journal(ledgers(tmp_path)[0].parent).events()) == 1


def test_carry_does_not_spend_cp_and_can_run_with_zero_regular_challenges(tmp_path, daily_request):
    port = Port(daily_request)
    port.issue, port.refresh = {1}, False
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'remind_carryover' and result['cp'] == 900 and result['remaining_count'] == 0
    port.clock += timedelta(seconds=1)
    port.instances[1] = ('synthetic-later-instance', port.clock)
    port.rounds[1] += 1
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete' and result['cp'] == 900


def test_defeated_boss_does_not_require_invented_round_or_instance(tmp_path, daily_request):
    port = Port(daily_request)
    observe = port.observe
    def defeated(binding):
        row = observe(binding)
        row['bosses'][0].update(available=False, round=None, phase=None, instance_id=None, appeared_at=None)
        return row
    port.observe = defeated
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'no_available_target' and result['regular_settled'] == 2
    assert result['held_regular'][0]['slot'] == 1


def test_first_seen_instance_has_no_invented_appearance_time_for_carry(tmp_path, daily_request):
    port = Port(daily_request)
    port.issue = {1}
    observe = port.observe
    def first_seen(binding):
        row = observe(binding)
        for boss in row['bosses']:
            boss['appeared_at'] = None
        return row
    port.observe = first_seen
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'remind_carryover' and result['regular_settled'] == 3
    assert result['carryover_settled'] == 0


def test_current_start_not_sent_closes_only_this_attempt_and_requires_new_start(tmp_path, daily_request):
    from pcr_maa.native import NativeInputNotSent
    port = Port(daily_request)
    calls = []
    execute = port.execute
    def rejected(intent, job):
        calls.append(intent)
        port.battle_start = 'not_sent'
        raise NativeInputNotSent(dict(input_execution='not_sent', clan_battle_phase='regular_start',
            daily_intent_id=intent['intent_id'], source='synthetic_rejected_dispatch'))
    port.execute = rejected
    result, _ = run(tmp_path, daily_request, port)
    assert result['error'] == 'native_input_not_sent' and result['consumption_sent'] is False
    rows = Journal(ledgers(tmp_path)[0].parent).events()
    assert len(calls) == 1 and len(rows) == 2 and rows[-1]['input_execution'] == 'not_sent'
    port.execute = execute
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete' and len(port.executions) == 3
    assert len(Journal(ledgers(tmp_path)[0].parent).events()) == 8


@pytest.mark.parametrize('fault', ['settings', 'wrong_intent', 'sent', 'unknown'])
def test_other_not_sent_or_unknown_does_not_clear_the_reserved_start(tmp_path, daily_request, fault):
    from pcr_maa.native import NativeInputNotSent
    port = Port(daily_request)
    def rejected(intent, job):
        port.battle_start = 'sent' if fault == 'sent' else 'not_sent'
        raise NativeInputNotSent(dict(input_execution='unknown' if fault == 'unknown' else 'not_sent',
            clan_battle_phase='settings' if fault == 'settings' else 'regular_start',
            daily_intent_id='wrong' if fault == 'wrong_intent' else intent['intent_id']))
    port.execute = rejected
    run(tmp_path, daily_request, port)
    assert len(Journal(ledgers(tmp_path)[0].parent).events()) == 1
    result, fresh = run(tmp_path, daily_request, Port(daily_request))
    assert result['error'] == 'daily_previous_result_unknown' and not fresh.executions


def test_not_sent_resumption_does_not_claim_old_history_as_new_consumption(tmp_path, daily_request):
    from pcr_maa.native import NativeInputNotSent
    port = Port(daily_request)
    port.available = {1: True, 2: False, 3: False}
    result, _ = run(tmp_path, daily_request, port)
    assert result['regular_settled'] == 1
    port.available[2] = True
    def rejected(intent, job):
        port.battle_start = 'not_sent'
        raise NativeInputNotSent(dict(input_execution='not_sent', clan_battle_phase='regular_start',
            daily_intent_id=intent['intent_id']))
    port.execute = rejected
    result, _ = run(tmp_path, daily_request, port)
    assert result['consumption_sent'] is False and result['regular_settled'] == 1


def test_carry_start_not_sent_preserves_grant_for_explicit_resumption(tmp_path, daily_request):
    from pcr_maa.native import NativeInputNotSent
    port = Port(daily_request)
    port.issue = {1}
    execute = port.execute
    def rejected(intent, job):
        if intent['action']['kind'] == 'regular':
            return execute(intent, job)
        port.battle_start = 'not_sent'
        raise NativeInputNotSent(dict(input_execution='not_sent', clan_battle_phase='carryover_start',
            daily_intent_id=intent['intent_id']))
    port.execute = rejected
    result, _ = run(tmp_path, daily_request, port)
    assert result['consumption_sent'] is True and result['regular_settled'] == 1
    assert result['carryover_settled'] == 0 and port.inventory == [0]
    port.execute = execute
    result, _ = run(tmp_path, daily_request, port)
    assert result['outcome'] == 'complete' and result['carryover_settled'] == 1
    assert len([row for row in port.executions if row['action']['kind'] == 'regular']) == 3
