"""Relic-converted defeat advances once without recording a failed attempt."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from pcr_maa.labyrinth import scene, autoplay
from pcr_maa.labyrinth.batch import BatchStopped
from test_labyrinth_autoplay import make, route_for, sequence, receipt


def failure_frame(*, ordinary=False, dim=False):
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    image[26:114, 493:794] = (230, 180, 80)
    image[36:67, 1066:1214] = (245, 245, 245)
    image[643:681, 986:1161] = (230, 180, 80)
    if ordinary:
        image[643:681, 673:848] = (245, 245, 245)
    return image // 2 if dim else image


def observe_failure(*, ordinary=False, dim=False, missing=None, guard=None):
    keys = {'DEFEAT': '战斗失败', 'DAMAGE_REPORT': '伤害报告', 'REVIVE_NEXT': '下一步'}
    if ordinary:
        keys.update(RETRY='重新挑战', END='结束')
    keys.pop(missing, None)
    def recognize(name, image):
        key = name.removeprefix('LabySceneWord_')
        if not name.startswith('LabySceneWord_') or key not in keys:
            return None
        box = scene._OCR[key][1]
        return NS(hit=True, reco_id=1, raw_detail={'filtered': [dict(text=keys[key], box=box, score=.99)]})
    return scene.SceneRecognition().analyze(NS(run_recognition=recognize),
        NS(image=failure_frame(ordinary=ordinary, dim=dim), custom_recognition_param=guard or {}))


def test_two_button_failure_requires_same_frame_layout_and_words():
    result = observe_failure()
    assert result.detail['kind'] == 'battle_defeat'
    assert result.detail['payload']['battle_failure_layout'] == 'two_button'


@pytest.mark.parametrize('change', [dict(ordinary=True), dict(dim=True),
    dict(missing='DAMAGE_REPORT'), dict(missing='REVIVE_NEXT'), dict(missing='DEFEAT')])
def test_normal_failure_overlay_and_missing_identity_never_qualify_for_advance(change):
    result = observe_failure(**change)
    assert result.detail['payload'].get('battle_failure_layout') != 'two_button'


def test_fresh_action_guard_rejects_layout_change_even_on_same_failure_page():
    guard = dict(kind='battle_defeat', battle_failure_layout='two_button')
    assert observe_failure(guard=guard).box is not None
    assert observe_failure(ordinary=True, guard=guard).box is None


def test_owned_revived_battle_completes_effect_and_map_without_retry_or_defeat(tmp_path):
    route = route_for([1, 2, 8])
    pages = sequence([1, 2, 8])
    pages[3:4] = [('battle_defeat', dict(battle_failure_layout='two_button')),
        ('item_reward', dict(effect='revive'))]
    runner, batch, runtime, _, actions = make(tmp_path, route, pages)
    original_start = actions.start_battle
    def start(route, node):
        if node.block_type == 8:
            return original_start(route, node)
        runtime.perform(runtime.observe(), 'start_battle')
        actions.battle_result_scene = runtime.observe()
        return dict(outcome='revived', native=receipt())
    actions.start_battle = start
    actions.record_defeat = lambda *args: pytest.fail('a revived battle is not a defeat')
    result = runner.play(route, batch, roles=('a', 'b', 'c'))
    assert result['outcome'] == 'cleared'
    assert runtime.inputs.count('advance_revived_battle') == 1
    assert 'retry_battle' not in runtime.inputs and not runner.battle_retries
    assert runtime.inputs.index('advance_revived_battle') < runtime.inputs.index('close_reward')


def test_unowned_two_button_page_never_sends_next(tmp_path):
    route = route_for([1, 2])
    runner, batch, runtime, _, _ = make(tmp_path, route,
        [('battle_defeat', dict(battle_failure_layout='two_button'))])
    result = runner.play(route, batch)
    assert result['outcome'] != 'cleared' and not runtime.inputs


def test_revived_reader_is_preserved_as_distinct_authenticated_outcome():
    owner = NS(stopping=False)
    observed = scene.Scene('battle_defeat', receipt(), dict(battle_failure_layout='two_button'))
    actions = autoplay.MaaRunActions.__new__(autoplay.MaaRunActions)
    actions.tasker = owner
    actions.batch = NS(checkpoint=lambda: None)
    actions.runtime = NS(observe=lambda **kwargs: observed)
    actions.team = NS(start_once=lambda batch, enter, block, read: read(enter, block))
    result = actions.start_battle(NS(enter_id=1), NS(block_id=10201))
    assert result['outcome'] == 'revived' and actions.battle_result_scene is observed


@pytest.mark.parametrize('outcome,enter,accepted', [('revived', 1, True), ('revived', 2, False), ('unknown', 1, False)])
def test_team_result_contract_accepts_revive_but_preserves_identity(outcome, enter, accepted):
    from pcr_maa.labyrinth.team_execution import MaaTeam
    team = MaaTeam.__new__(MaaTeam)
    team.pages = {(1, 10201): 'battle'}
    team.prepared, team.started, team.roster = {(1, 10201): ['a']}, set(), ('a',)
    team._characters = NS(read=lambda *a, **k: ({'selected':['a']}, receipt()),
        run=lambda *a, **k: ({}, receipt(action=True)))
    team.stamp = lambda **fields: fields
    batch = NS(checkpoint=lambda: None, transaction=lambda op, fields, send: send('owned'),
        observation=lambda value, **fields: value)
    read = lambda *args: dict(verified=True, outcome=outcome, enter_id=enter, block_id=10201)
    if accepted:
        assert team.start_once(batch, 1, 10201, read)['outcome'] == 'revived'
    else:
        with pytest.raises(BatchStopped, match='^battle_result_unknown$'):
            team.start_once(batch, 1, 10201, read)
