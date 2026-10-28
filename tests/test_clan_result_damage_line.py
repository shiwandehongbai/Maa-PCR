"""The shipped modern result layout reads the complete damage and multiplier twice."""
from copy import deepcopy
import numpy as np
import pytest

from pcr_maa import clan_battle_native as module
from test_clan_battle_native import calibration, calibration_v2, plan, v2_context


def read_result(calibration_v2, plan, first, second):
    _, texts, _, context = v2_context(calibration_v2, plan)
    texts["CBField_result_damage"], texts["CBField_result_damage_verify"] = first, second
    page = module.ClanBattlePage(plan, calibration_v2)
    page.bound = dict(period=plan["period"], phase="B", boss=plan["boss"], round=None,
        round_source="selected_difficulty",
        selected_difficulty=dict(phase="B", boss=plan["boss"], simulation=True))
    page.current_page = "battle"
    return page.read(context, np.zeros((720, 1280, 3), np.uint8), "result")


@pytest.mark.parametrize("text,expected", [
    ("185851253×5.0", 185851253), ("90000000×3.1", 90000000),
    ("50000000×2.8", 50000000), ("1,480,000,000×5.0", 1480000000),
])
def test_same_frame_complete_lines_agree(calibration_v2, plan, text, expected):
    assert read_result(calibration_v2, plan, text, text)["damage"] == expected


@pytest.mark.parametrize("text", [
    "185851253", "185851253+5.0", "185851253×", "185851253×5.",
    "185851253×5", "185851253×5.0garbage",
])
def test_incomplete_or_wrong_multiplier_is_rejected(calibration_v2, plan, text):
    with pytest.raises(RuntimeError, match="page_field_unknown"):
        read_result(calibration_v2, plan, text, text)


@pytest.mark.parametrize("second", ["185851257×5.0", "185851253×3.1", "18585125×5.0"])
def test_disagreeing_same_frame_reads_are_rejected(calibration_v2, plan, second):
    with pytest.raises(RuntimeError, match="result_damage_mismatch"):
        read_result(calibration_v2, plan, "185851253×5.0", second)


def test_derived_rois_are_bounded_to_the_known_modern_layout(calibration_v2, plan):
    before = deepcopy(calibration_v2)
    nodes = module.calibration_pipeline(calibration_v2, plan)
    assert nodes["CBField_result_damage"]["roi"] == [80, 45, 332, 42]
    assert nodes["CBField_result_damage_verify"]["roi"] == [88, 47, 324, 38]
    assert nodes["CBField_result_damage_verify"]["only_rec"] is True
    assert calibration_v2 == before
    calibration_v2["pages"]["result"]["fields"]["damage"] = [88, 47, 163, 38]
    with pytest.raises(ValueError, match="result_damage_uncalibrated"):
        module.calibration_pipeline(calibration_v2, plan)


def test_unreviewed_device_layout_is_not_inferred_from_matching_roi(calibration_v2, plan):
    calibration_v2.update(evidence_kind="device_saved_frame", calibration_id="another-profile")
    with pytest.raises(ValueError, match="result_damage_uncalibrated"):
        module.calibration_pipeline(calibration_v2, plan)


def test_v1_result_still_accepts_the_original_digits_only(calibration, plan):
    nodes = module.calibration_pipeline(calibration, plan)
    assert nodes["CBField_result_damage"]["expected"] == module.FIELDS["damage"]
    assert "CBField_result_damage_verify" not in nodes


@pytest.mark.parametrize("text,verify,expected", [
    ("265113492×4.5", "265113492×4.5", 265113492),
    ("1,480,000,000x5.0", "1，480，000，000×5．0", 1480000000),
])
def test_shared_complete_line_parser_preserves_formal_damage(text, verify, expected):
    assert module.result_damage_line(text, verify) == expected


@pytest.mark.parametrize("text,verify,error", [
    ("265113492×4.5", "26511349×4.5", "result_damage_mismatch"),
    ("265113492×4.5", "265113492×4.8", "result_damage_mismatch"),
    ("265113492×4.5", "265113492", "result_damage_unknown"),
    (None, "265113492×4.5", "result_damage_unknown"),
])
def test_shared_complete_line_parser_rejects_missing_or_conflicting_read(text, verify, error):
    with pytest.raises(RuntimeError, match=error):
        module.result_damage_line(text, verify)


def test_shared_nodes_require_the_known_anchor_and_do_not_mutate_it():
    roi = [88, 47, 162, 38]
    nodes = module.result_damage_nodes(roi, "CBDailyField_result_damage")
    assert roi == [88, 47, 162, 38]
    assert nodes["CBDailyField_result_damage"]["roi"] == [80, 45, 332, 42]
    assert nodes["CBDailyField_result_damage_verify"]["roi"] == [88, 47, 324, 38]
    assert nodes["CBDailyField_result_damage_verify"]["only_rec"] is True
    with pytest.raises(ValueError, match="result_damage_uncalibrated"):
        module.result_damage_nodes([88, 47, 163, 38], "CBDailyField_result_damage")
