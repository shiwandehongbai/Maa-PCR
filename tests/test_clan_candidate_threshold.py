"""Only v2 list candidates accept the measured badge-animation contrast drop."""
from types import SimpleNamespace as NS
import cv2
import numpy as np
import pytest
from pcr_maa.clan_battle_characters import ClanBattleCharacters
from pcr_maa.guide_trial_characters import GuideTrialCharacters


@pytest.mark.parametrize('kind,version,roi,score,accepted', [
    ('clan', 2, [80,153,1120,342], .863416, True),
    ('clan', 2, [80,153,1120,342], .8499, False),
    ('clan', 1, [80,153,1120,342], .863416, False),
    ('clan', 2, [65,542,126,126], .863416, False),
    ('clan', 2, [255,522,135,135], .863416, False),
    ('clan', 2, [350,275,100,100], .863416, False),
    ('guide', 2, [80,153,1120,342], .863416, False),
    ('guide', 2, [80,153,1120,342], .9, True),
])
def test_candidate_threshold_scope_preserves_default_slot_and_hud_gate(monkeypatch, kind, version, roi, score, accepted):
    reader = object.__new__(ClanBattleCharacters if kind == 'clan' else GuideTrialCharacters)
    reader.calibration = dict(version=version, formation=dict(candidates=[[80,153,1120,342]]))
    reader.templates = {133601: [(np.zeros((2,2,3), np.uint8), 0, 0, 126)]}
    monkeypatch.setattr(cv2, 'matchTemplate', lambda *args: np.array([[score]], dtype=np.float32))
    result = reader.match(np.zeros((720,1280,3), np.uint8), 133601, roi)
    assert (result is not None) is accepted
    if accepted: assert result['unit_id'] == 133601
