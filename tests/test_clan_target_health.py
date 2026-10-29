import cv2
import numpy as np

from pcr_maa import native
from pcr_maa.clan_battle_calibration import load_calibration
from pcr_maa.clan_battle_daily_native import daily_pipeline
from pcr_maa.replay import SavedImages
from test_clan_opening import sampled_plan


def test_formal_health_line_reads_complete_ratio_and_rejects_blank():
    plan = sampled_plan()
    profile = load_calibration(plan, capability='formal')
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[402:441, 645:925] = cv2.imread(str(native.ROOT/'tests/data/clan-target-health.png'))
    nodes = daily_pipeline(profile, plan)
    name = 'CBDaily_target_health'
    assert nodes[name]['threshold'] == .9
    nodes['HealthMissing'] = dict(recognition='DirectHit', action='DoNothing', next=[], pre_delay=0, post_delay=0)
    nodes['CheckHealth'] = dict(recognition='DirectHit', action='DoNothing', next=[name, 'HealthMissing'], pre_delay=0, post_delay=0)
    resource = native.load_resource()
    assert resource.override_pipeline(nodes)
    controller = SavedImages([[frame]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    try:
        result = native.run(tasker, 'CheckHealth')['nodes'][-1]
        assert result['name'] == name
        assert result['recognition']['best']['text'] == '1162822255/1480000000'
        controller.stages = [[np.zeros_like(frame)]]
        assert native.run(tasker, 'CheckHealth')['nodes'][-1]['name'] == 'HealthMissing'
        assert not controller.clicks and not controller.swipes
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']
