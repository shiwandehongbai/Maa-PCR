"""Fourth-stage simulation uses saved real menu and target frames."""
import pytest

from pcr_maa.clan_battle_calibration import load_calibration
from pcr_maa.clan_battle_native import ClanBattlePage, _detail
from test_clan_difficulty import read_saved
from test_clan_opening import sampled_plan


@pytest.mark.parametrize("name,phase,selected", [
    ("difficulty-d-selected.png", "D", True),
    ("difficulty-c-selected-d-available.png", "D", False),
    ("difficulty-d-selected.png", "C", False),
    ("difficulty-d-selected.png", "B", False),
])
def test_real_menu_distinguishes_d_radio(name, phase, selected):
    receipt, errors = read_saved(name, phase, "CBDifficultyChosen")
    assert receipt["native_ok"] is selected, errors


def test_real_d3_simulation_target_is_fourth_stage_and_full_simulation():
    receipt, errors = read_saved("target-d3.png", "D", "CBPage_target", page="target")
    assert receipt["native_ok"], errors
    detail = _detail(receipt)
    assert detail["phase"] == "D" and detail["boss"] == 3
    assert detail["simulation"] is True and detail["live_state"] == "off"
    assert detail["selected_difficulty"] == dict(phase="D", boss=3, simulation=True)


def test_real_d3_formal_page_cannot_be_a_simulation_target():
    receipt, errors = read_saved("boss-detail-d3.png", "D", "CBPage_target", page="target")
    assert not receipt["native_ok"] and errors


def test_d_radio_click_uses_the_existing_same_row_target():
    plan = dict(sampled_plan(), phase="D", boss=3)
    callback = ClanBattlePage(plan, load_calibration(plan))
    callback.read = lambda *args, **kwargs: {}
    callback.hit = lambda *args, **kwargs: True
    assert callback.action(None, None, "select_difficulty") == [665, 402, 1, 1]
