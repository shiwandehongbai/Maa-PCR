import cv2
import numpy as np

from pcr_maa import native
from pcr_maa.clan_battle_calibration import load_calibration
from pcr_maa.clan_battle_native import calibration_pipeline
from pcr_maa.clan_battle_daily_native import require_daily_execution
from pcr_maa.replay import SavedImages
from test_clan_opening import sampled_plan


def test_adventure_title_on_brown_background_keeps_ocr_threshold_and_rejects_missing_title():
    plan = sampled_plan()
    profile = load_calibration(plan, capability='formal')
    frame = np.zeros((720,1280,3), dtype=np.uint8)
    frame[:80,60:190] = cv2.imread(str(native.ROOT/'tests/data/clan-adventure-title.png'))
    nodes = calibration_pipeline(profile, plan)
    nodes.update(require_daily_execution(profile, plan))
    nodes['HeaderMissing'] = dict(recognition='DirectHit',action='DoNothing',next=[],pre_delay=0,post_delay=0)
    names = ('CBLabel_adventure_0','CBDaily_adventure_labels_0')
    for name in names:
        assert nodes[name]['threshold'] == .8
        nodes['Check'+name] = dict(recognition='DirectHit',action='DoNothing',next=[name,'HeaderMissing'],pre_delay=0,post_delay=0)
    assert nodes['CBDaily_lobby_cp']['threshold'] == .9
    assert nodes['CBDaily_result_damage']['threshold'] == .9
    resource = native.load_resource()
    assert resource.override_pipeline(nodes)
    controller = SavedImages([[frame]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    try:
        for name in names:
            assert native.run(tasker,'Check'+name)['nodes'][-1]['name'] == name
        controller.stages = [[np.zeros_like(frame)]]
        for name in names:
            assert native.run(tasker,'Check'+name)['nodes'][-1]['name'] == 'HeaderMissing'
        assert not controller.clicks and not controller.swipes
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']
