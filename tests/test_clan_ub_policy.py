"""A same-frame read of the UB auto-clear setting supplies no setting inputs."""
from copy import deepcopy

import pytest

from pcr_maa import native
from pcr_maa.clan_battle_calibration import load_calibration
from pcr_maa.clan_battle_inheritance import InheritancePage, inheritance_pipeline
from pcr_maa.clan_battle_native import ClanBattlePage, _detail
from pcr_maa.replay import SavedImages
from test_clan_opening import sampled_plan


def replay_policy(image, *, wrong_row=False):
    plan = sampled_plan()
    profile = load_calibration(plan)
    spec = profile['auto_battle']['inheritance']
    resource = native.load_resource()
    callback = InheritancePage(spec, ClanBattlePage(plan, profile))
    assert resource.register_custom_recognition('ClanBattleInheritance', callback)
    nodes = inheritance_pipeline(spec)
    if wrong_row:
        nodes['CBSetResetOnLabel']['roi'] = [660, 365, 100, 55]
        nodes['CBSetResetOffLabel']['roi'] = [960, 365, 100, 55]
    assert resource.override_pipeline(nodes)
    controller = SavedImages([[image]])
    tasker = native.bind(resource, controller)
    tasker._resource_holder = resource
    try:
        receipt = native.run(tasker, 'CBSetResetRead')
        assert not controller.clicks and not controller.swipes
        return _detail(receipt) if receipt['native_ok'] else None
    finally:
        stopped = native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


def frame():
    return native.read_image(native.ROOT / 'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples/inherit-settings-all-top.png')


@pytest.mark.parametrize('variant,expected', [
    ('off', False), ('on', True), ('both_off', None), ('both_on', None),
    ('missing_description', None), ('wrong_row', None), ('unknown_radio', None)])
def test_saved_settings_page_reads_the_exact_ub_option_without_any_input(variant, expected):
    image = frame()
    left = image[542:577, 625:660].copy()
    right = image[542:577, 925:960].copy()
    if variant == 'on':
        image[542:577, 625:660], image[542:577, 925:960] = right, left
    elif variant == 'both_off':
        image[542:577, 925:960] = left
    elif variant == 'both_on':
        image[542:577, 625:660] = right
    elif variant == 'missing_description':
        image[440:485, 40:1140] = 0
    elif variant == 'unknown_radio':
        image[542:577, 925:960] = 0
    reading = replay_policy(image, wrong_row=variant == 'wrong_row')
    if expected is None:
        assert reading is None
    else:
        assert reading['auto_release_on_ub'] is expected and reading['same_frame'] is True
        assert len(reading['frame_pixel_sha256']) == 64


def test_older_calibration_without_the_optional_readback_cannot_provide_policy():
    profile = load_calibration(sampled_plan())
    spec = deepcopy(profile['auto_battle']['inheritance'])
    spec.pop('ub_auto_clear')
    assert 'CBSetResetRead' not in inheritance_pipeline(spec)
    callback = InheritancePage(spec, ClanBattlePage(sampled_plan(), profile))
    assert callback.ub_auto_clear is False
