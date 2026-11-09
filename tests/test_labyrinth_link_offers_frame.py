"""Link-imprint offer frames: badge identity, OCR quantity binding and the guard.

The two 1280x720 frames are the user-provided 2026-11-09 card pages
(light/fire/wind and light/water/dark). They cover all five attributes and are
the reason the pack uses the 31xxxxxx badge series: its crop matches these live
cards at NCC 0.983-0.987, while the 11xxxxxx series only reached 0.886-0.903.
This is an offline SavedImages replay, not live-game acceptance.
"""
from hashlib import sha256
from dataclasses import replace
import json
import socket
from pathlib import Path

import pytest

from pcr_maa import native
from pcr_maa.labyrinth import links
from pcr_maa.labyrinth.batch import BatchStopped
from pcr_maa.labyrinth.link_imprint import (LinkImprintIconCapture, load_catalog,
                                             BADGE_ROIS)
from pcr_maa.replay import SavedImages

DATA = Path(__file__).with_name('data')
MANIFEST = json.loads((DATA / 'labyrinth-link-choice-20261109.json').read_text(encoding='utf-8'))
FRAMES = ('labyrinth-link-choice-20261109.png', 'labyrinth-link-choice-20261109-b.png')


def image(name):
    raw = (DATA / name).read_bytes()
    meta = MANIFEST['frames'][name]
    assert sha256(raw).hexdigest() == meta['sha256']
    frame = native.read_image(DATA / name)
    assert frame.shape[:2] == (meta['height'], meta['width'])
    return frame


@pytest.fixture
def link_offline(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail('offline saved frame prohibits network/device')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(native, 'RUNTIME', tmp_path / 'native')
    controller = SavedImages([[image(FRAMES[0])], [image(FRAMES[1])]])
    resource = native.load_resource()
    assert resource.override_pipeline({**links.link_pipeline(), **links.result_pipeline()})
    assert resource.register_custom_recognition(
        links.LINK_ICON, LinkImprintIconCapture(load_catalog()))
    tasker = native.bind(resource, controller)
    try:
        yield controller, tasker
    finally:
        native.stop_tasker(tasker)


def _observe(controller, tasker, stage):
    controller.stage = stage
    controller.frame = 0
    receipt = native.run(tasker, 'LabyLinkObserved')
    return receipt, links.link_offers(receipt)


def _identity_detail(receipt):
    rows = next(node['recognition'] for node in receipt['nodes'] if node['name'] == 'LabyLinkObserved')
    node = next(row for row in rows if row['name'] == 'LabyLinkIconIdentity')
    return node['detail']['filtered'][0]['detail']


@pytest.mark.parametrize('stage', [0, 1])
def test_offer_frame_identity_comes_from_the_badge(link_offline, stage):
    _, tasker = link_offline
    name = FRAMES[stage]
    receipt, offers = _observe(link_offline[0], tasker, stage)
    expected = MANIFEST['expected']

    assert [offer.element for offer in offers] == [
        item['element'] for item in expected['offers'][name]]
    assert [offer.quantity for offer in offers] == [expected['quantity']] * 3
    assert [offer.remaining for offer in offers] == [expected['remaining']] * 3
    assert all(not hasattr(offer, 'character_ids') for offer in offers)

    # The same OCR line is a cross-check, not the identity: all five readable
    # attributes agree with the badges on these frames.
    assert all(offer.element in '火暗光风水' for offer in offers)
    assert links.preferred_choice(offers).element == expected['priority_choice'][name]

    detail = _identity_detail(receipt)
    assert detail['mode'] == 'offers' and detail['trusted'] is True
    for slot, item in enumerate(detail['slots']):
        assert item['trusted'] is True
        assert item['element'] == offers[slot].element
        assert item['score'] >= .95
        assert item['rival'] <= .70
        assert item['margin'] >= .25
        assert item['art'] == list(BADGE_ROIS[slot])


def test_offer_frame_one_shot_guard_clicks_only_the_selected_card(link_offline):
    controller, tasker = link_offline
    _, offers = _observe(controller, tasker, 0)
    selected = links.preferred_choice(offers)
    assert selected.slot == 1 and selected.element == '火'
    guard = links.selection_override(offers, selected.slot)
    assert guard['LabyLinkIconGuard']['custom_recognition_param'] == dict(
        mode='guard', slot=1, element='火')
    with links.selection_guard(tasker, offers, selected.slot):
        value = native.run(tasker, 'LabyLinkSelectOnce')
    assert value['native_ok'] is True
    assert len(controller.clicks) == 1 and not controller.swipes
    x, y = controller.clicks[0]
    bx, by, bw, bh = links.BUTTON_ROIS[1]
    assert bx <= x <= bx + bw and by <= y <= by + bh


def test_offer_frame_blank_badge_falls_back_to_the_text_line(link_offline):
    controller, tasker = link_offline
    frame = image(FRAMES[0])
    x, y, w, h = BADGE_ROIS[0]
    frame[y:y + h, x:x + w] = 255
    controller.stages[0] = [frame]
    controller.stage, controller.frame = 0, 0
    offers = links.link_offers(native.run(tasker, 'LabyLinkObserved'))
    assert [(offer.element, offer.identity, offer.icon) for offer in offers] == [
        ('光', links.IDENTITY_TEXT, None),
        ('火', links.IDENTITY_BADGE, offers[1].icon),
        ('风', links.IDENTITY_BADGE, offers[2].icon)]
    assert links.preferred_choice(offers).element == '火'


def test_offer_frame_text_fallback_guard_clicks_the_selected_text_card(link_offline):
    controller, tasker = link_offline
    frame = image(FRAMES[0])
    x, y, w, h = BADGE_ROIS[0]
    frame[y:y + h, x:x + w] = 255
    controller.stages[0] = [frame]
    controller.stage, controller.frame = 0, 0
    offers = links.link_offers(native.run(tasker, 'LabyLinkObserved'))
    guard = links.selection_override(offers, 0)
    assert 'LabyLinkIconGuard' not in guard['LabyLinkSelectOnce']['all_of']
    assert guard['LabyLinkGuardQuantity0']['expected'] == r'^光属性的随机印记[×?？xX*]?3$'
    with links.selection_guard(tasker, offers, 0):
        value = native.run(tasker, 'LabyLinkSelectOnce')
    assert value['native_ok'] is True
    assert len(controller.clicks) == 1 and not controller.swipes
    bx, by, bw, bh = links.BUTTON_ROIS[0]
    assert bx <= controller.clicks[0][0] <= bx + bw and by <= controller.clicks[0][1] <= by + bh


def test_offer_frame_blank_badge_and_text_stops(link_offline):
    controller, tasker = link_offline
    frame = image(FRAMES[0])
    x, y, w, h = BADGE_ROIS[0]
    frame[y:y + h, x:x + w] = 255
    qx, qy, qw, qh = links.QUANTITY_ROIS[0]
    frame[qy:qy + qh, qx:qx + qw] = 255
    controller.stages[0] = [frame]
    controller.stage, controller.frame = 0, 0
    with pytest.raises(BatchStopped):
        links.link_offers(native.run(tasker, 'LabyLinkObserved'))


def test_offer_frame_duplicate_badge_is_refused(link_offline):
    controller, tasker = link_offline
    frame = image(FRAMES[0])
    source = frame[BADGE_ROIS[1][1]:BADGE_ROIS[1][1] + BADGE_ROIS[1][3],
                   BADGE_ROIS[1][0]:BADGE_ROIS[1][0] + BADGE_ROIS[1][2]].copy()
    x, y, w, h = BADGE_ROIS[2]
    frame[y:y + h, x:x + w] = source
    controller.stages[0] = [frame]
    controller.stage, controller.frame = 0, 0
    # The copied fire badge now contradicts slot 2's own 风 text; the same-frame
    # conflict stops before any ambiguous two-fire identity could be used.
    with pytest.raises(BatchStopped, match='link_identity_conflict'):
        links.link_offers(native.run(tasker, 'LabyLinkObserved'))


def test_offer_frame_selected_badge_mismatch_sends_zero_input(link_offline):
    controller, tasker = link_offline
    _, offers = _observe(controller, tasker, 0)
    # Ask the guard for the wrong element on the selected card: the click frame
    # still shows 火, so the badge guard must miss and no input is sent.
    wrong = list(offers)
    wrong[1] = replace(offers[1], element='水', icon=links.LinkIcon('水', .98, .4, .58),
        quantity_words=(replace(offers[1].quantity_words[0], text='水属性的随机印记×3'),))
    with links.selection_guard(tasker, wrong, 1):
        value = native.run(tasker, 'LabyLinkSelectOnce')
    assert value['native_ok'] is False
    assert not controller.clicks and not controller.swipes
