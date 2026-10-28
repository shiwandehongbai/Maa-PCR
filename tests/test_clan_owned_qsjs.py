"""All-owned qsjs uses calibrated own-unit execution without changing the job."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from pcr_maa import clan_battle as battle, clan_battle_native as simulation, clan_battle_daily_native as daily
from test_clan_battle_native import calibration, calibration_v2, own_only_profile
from test_clan_battle_daily_native import executable, profile


@pytest.fixture
def bound_plan(tmp_path):
    ids = [129701, 116701, 134701, 135501, 104701]
    image = tmp_path / 'synthetic-source.png'
    image.write_bytes(b'synthetic source; never a live-game frame')
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps(dict(schema_version=1, server='cn-bilibili', account_id='123456789',
        content_version='202610141431', observed_at=datetime.now(timezone.utc).isoformat(),
        ownership_complete=True, unknown_fields=[],
        units=[dict(unit_id=uid, battle_rarity=star) for uid, star in zip(ids, [5, 5, 5, 5, 4])])), encoding='utf-8')
    plan = dict(version=1, server='cn-bilibili', account_id='123456789', content_version='202610141431',
        period='2026-09', phase='BC', round_min=4, round_max=10, boss=3, boss_name='',
        package='synthetic', team_label='synthetic own qsjs', unit_ids=ids, stars=[5, 5, 5, 5, 4],
        auto=True, set_by_unit={str(uid): True for uid in ids}, switches=[], support_mode='qsjs',
        support_unit_id=None, reference_damage=None,
        source=dict(url='https://www.bilibili.com/video/BV1Rbh86XEsq/', bvid='BV1Rbh86XEsq',
            page=1, cid=42138340822, at_seconds=2, image_path=str(image),
            image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(), job_id='synthetic-own-qsjs'),
        snapshot=dict(path=str(snapshot), sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest()))
    plan['confirmed_for'] = battle.plan_hash(plan)
    return plan


def change_snapshot(plan, update):
    path = Path(plan['snapshot']['path'])
    value = json.loads(path.read_text(encoding='utf-8'))
    update(value)
    path.write_text(json.dumps(value), encoding='utf-8')
    plan['snapshot']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    plan['confirmed_for'] = battle.plan_hash(plan)


def check_gate(kind, plan, calibration_v2, executable):
    if kind == 'stars':
        return battle.account_battle_stars(plan)
    if kind == 'simulation':
        return simulation.calibration_pipeline(own_only_profile(calibration_v2), plan)
    return daily.require_daily_execution(own_only_profile(executable), plan)


@pytest.mark.parametrize('kind', ['stars', 'simulation', 'formal'])
@pytest.mark.parametrize('mode', ['none', 'qsjs'])
@pytest.mark.parametrize('complete', [True, False])
def test_all_explicitly_owned_modes_preserve_four_stars_and_plan(kind, mode, complete, bound_plan, calibration_v2, executable):
    bound_plan['support_mode'] = mode
    change_snapshot(bound_plan, lambda value: value.update(ownership_complete=complete))
    before = deepcopy(bound_plan)
    snapshot_bytes = Path(bound_plan['snapshot']['path']).read_bytes()
    assert battle.validate_plan(bound_plan) == bound_plan
    assert battle.support_requirement(bound_plan, battle.load_snapshot(bound_plan)) is None
    result = check_gate(kind, bound_plan, calibration_v2, executable)
    if kind == 'stars':
        assert result == dict(zip(bound_plan['unit_ids'], [5, 5, 5, 5, 4]))
    elif kind == 'simulation':
        assert not any(name in result for name in ('CBReadSupport', 'CBAddSupport', 'CBAction_open_support'))
    else:
        assert 'CBFormalOpeningSet0' in result
    assert bound_plan == before
    assert battle.plan_hash(bound_plan) == bound_plan['confirmed_for']
    assert Path(bound_plan['snapshot']['path']).read_bytes() == snapshot_bytes


@pytest.mark.parametrize('kind', ['stars', 'simulation', 'formal'])
@pytest.mark.parametrize('mode', ['qsjs', 'specified'])
def test_actual_support_is_still_uncalibrated(kind, mode, bound_plan, calibration_v2, executable):
    bound_plan.update(support_mode=mode, support_unit_id=bound_plan['unit_ids'][-1] if mode == 'specified' else None)
    if mode == 'qsjs':
        change_snapshot(bound_plan, lambda value: value['units'].pop())
    else:
        bound_plan['confirmed_for'] = battle.plan_hash(bound_plan)
    assert battle.support_requirement(bound_plan, battle.load_snapshot(bound_plan)) == bound_plan['unit_ids'][-1]
    with pytest.raises(RuntimeError, match='support_uncalibrated' if kind == 'stars' else 'support_mode_uncalibrated'):
        check_gate(kind, bound_plan, calibration_v2, executable)


@pytest.mark.parametrize('kind', ['stars', 'simulation', 'formal'])
@pytest.mark.parametrize('field', ['units.129701.ownership', 'units.129701', 'ownership'])
def test_unknown_owned_identity_cannot_enable_qsjs(kind, field, bound_plan, calibration_v2, executable):
    change_snapshot(bound_plan, lambda value: value.update(unknown_fields=[field]))
    with pytest.raises(ValueError, match='ownership_unknown'):
        check_gate(kind, bound_plan, calibration_v2, executable)


@pytest.mark.parametrize('kind', ['stars', 'simulation', 'formal'])
def test_partial_roster_missing_a_unit_remains_unknown(kind, bound_plan, calibration_v2, executable):
    change_snapshot(bound_plan, lambda value: (value.update(ownership_complete=False), value['units'].pop()))
    with pytest.raises(RuntimeError, match='ownership_unknown'):
        check_gate(kind, bound_plan, calibration_v2, executable)


@pytest.mark.parametrize('kind', ['stars', 'simulation', 'formal'])
def test_unvalidated_unknown_mode_cannot_use_own_only_gate(kind, bound_plan, calibration_v2, executable):
    bound_plan['support_mode'] = 'unknown'
    with pytest.raises(RuntimeError, match='support_uncalibrated' if kind == 'stars' else 'support_mode_uncalibrated'):
        check_gate(kind, bound_plan, calibration_v2, executable)
