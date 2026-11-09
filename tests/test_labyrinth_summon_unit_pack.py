"""Summon-unit icon pack and catalog: series semantics, evidence and TODO.

The labyrinth client draws imprint items with ``icon_labyrinth_icon_summon_unit_<id>``.
The 2026-11-09 shop frames show the gold series (110*/111*) is the choice
imprint and the gray series (310*/311*) is the random imprint; the 310* role
class and the 311* attribute mappings also come from ``labyrinth_summon_unit``
(master 202611021739). The 111* attribute-choice series has no live frame yet,
so it is migrated as data but marked unverified.
"""
from hashlib import sha256
import json
import re
from pathlib import Path

import cv2
import numpy as np

from pcr_maa.labyrinth.link_imprint import ELEMENTS

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets/labyrinth'
PACK = ASSETS / 'summon-unit-pack'
CATALOG = ASSETS / 'summon-units.json'
DATA = Path(__file__).with_name('data')
FRAME = DATA / 'labyrinth-shop-choice-imprint-20261109.png'
MANIFEST = DATA / 'labyrinth-summon-units-20261109.json'
ROLE_CLASSES = ['攻击', '破防', '增益', '减益', '强化', '治疗', '坦克', '干扰']
ELEMENT_ORDER = ['火', '水', '风', '光', '暗']
SERIES = {
    '110': ('gold', 1, 'choice_imprint', 'role_class', True),
    '111': ('gold', 1, 'choice_imprint', 'attribute', False),
    '310': ('gray', 3, 'random_imprint', 'role_class', True),
    '311': ('gray', 3, 'random_imprint', 'attribute', True),
}


def provenance():
    return json.loads((PACK / 'PROVENANCE.json').read_text(encoding='utf-8'))


def catalog():
    return json.loads(CATALOG.read_text(encoding='utf-8'))


def units():
    return {unit['summonId']: unit for unit in catalog()['units']}


def _image(path):
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def test_provenance_pins_every_icon():
    prov = provenance()
    assert prov['schemaVersion'] == 1
    assert prov['pack'] == 'labyrinth-summon-unit-pack'
    assert prov['clientVersion'] == '11.7.2'
    assert prov['distribution_permission_not_inferred'] is True
    assert prov['private_captures_included'] is False and prov['private_uid_included'] is False
    assert prov['sourceIndex']['file'] == '索引.csv'
    assert re.fullmatch(r'[0-9a-f]{64}', prov['sourceIndex']['sha256'])
    assert prov['sourceMaster'] == {
        'table': 'labyrinth_summon_unit', 'version': '202611021739',
        'sha256': 'ac6c17ab2fdd9ad4d93955d42b35496d56c80aa2ca28cc5b4f064f6f1e5529c8',
        'rows': 33}
    records = {row['path']: row for row in prov['icons']}
    files = sorted(path.relative_to(PACK).as_posix() for path in (PACK / 'icons').glob('*.png'))
    assert len(records) == len(files) == 1 + 8 + 5 + 1 + 8 + 5 == 28
    assert files == sorted(records)
    for name, record in records.items():
        raw = (PACK / name).read_bytes()
        assert raw[:8] == b'\x89PNG\r\n\x1a\n'
        width, height = int.from_bytes(raw[16:20], 'big'), int.from_bytes(raw[20:24], 'big')
        assert (width, height) == (record['width'], record['height']) == (128, 128)
        assert sha256(raw).hexdigest() == record['sha256']
        assert len(raw) == record['bytes'] == (PACK / name).stat().st_size
        assert record['pixelFormat'] == '32bppArgb'
        assert record['sourceId'] == int(Path(name).stem)
        assert record['sourceBundle'] == f"icon_labyrinth_icon_summon_unit_{record['sourceId']}.unity3d"


def test_catalog_series_carry_frame_kind_and_verification():
    data = catalog()
    series = {row['id']: row for row in data['series']}
    assert set(series) == set(SERIES)
    for series_id, (frame, summon_type, kind, scope, verified) in SERIES.items():
        row = series[series_id]
        assert (row['frame'], row['summonType'], row['kind'], row['scope'], row['verified']) == (
            frame, summon_type, kind, scope, verified)
        assert row['summonIds'] == sorted(row['summonIds'])
        assert len(row['summonIds']) == (8 if scope == 'role_class' else 5)
    assert series['111']['evidence'] == []
    assert '无实机帧' in series['111']['todo']
    assert series['110']['evidence'] and series['310']['evidence'] and series['311']['evidence']


def test_role_class_and_element_mapping_matches_the_master_rows():
    data, by_id = catalog(), units()
    assert [row['name'] for row in data['roleClasses']] == ROLE_CLASSES
    assert [row['element'] for row in data['elements']] == ELEMENT_ORDER
    # The link-imprint policy uses the same five attributes in its own priority
    # order (火>暗>光>风>水); the pack id order is 火水风光暗.
    assert set(ELEMENT_ORDER) == set(ELEMENTS) == {'火', '水', '风', '光', '暗'}
    for index, row in enumerate(data['roleClasses'], start=1):
        choice = 11001000 + (index - 1) * 1000
        random = 31001000 + (index - 1) * 1000
        assert (row['choiceSummonId'], row['randomSummonId']) == (choice, random)
        assert by_id[choice]['name'] == row['name'] + '型职能的选择印记'
        assert by_id[random]['name'] == row['name'] + '型职能的随机印记'
        assert by_id[choice]['price'] == 2800 and by_id[random]['price'] == 1200
        assert by_id[choice]['frame'] == 'gold' and by_id[random]['frame'] == 'gray'
    for index, row in enumerate(data['elements'], start=1):
        random = 31100000 + (index - 1) * 100000
        choice = 11100000 + (index - 1) * 100000
        assert (row['randomSummonId'], row['choiceSummonId']) == (random, choice)
        assert by_id[random]['name'] == row['randomName'] == row['element'] + '属性的随机印记'
        assert by_id[random]['verified'] is True and by_id[random]['price'] == 1200
        assert row['choiceVerified'] is False


def test_units_cover_the_pack_and_mark_the_inferred_series():
    data, by_id = catalog(), units()
    assert len(by_id) == 28
    assert set(by_id) == {row['sourceId'] for row in provenance()['icons']}
    for summon_id, unit in by_id.items():
        assert unit['icon'] == f'icons/{summon_id}.png' and (PACK / unit['icon']).is_file()
        frame, summon_type, kind, scope, verified = SERIES[unit['series']]
        assert (unit['frame'], unit['summonType'], unit['kind']) == (frame, summon_type, kind)
        if unit['series'] == '111':
            assert unit['verified'] is verified is False
            assert unit['icon'].startswith('icons/') and unit['source'] == 'inferred'
            assert unit['name'] is None and unit['price'] is None
            assert unit['scope'] == 'attribute' and unit['element'] in ELEMENT_ORDER
            assert '无实机帧' in unit['note']
        else:
            assert unit['verified'] is verified is True
            assert unit['source'] == 'master' and type(unit['name']) is str and unit['name']
            assert type(unit['price']) is int and unit['summonType'] == (1 if unit['series'] == '110' else 3)
    assert {summon_id for summon_id, unit in by_id.items() if unit['scope'] == 'all'} == {11000000, 31000000}
    assert by_id[11000000]['name'] == '所有角色的选择印记'
    assert by_id[31000000]['name'] == '所有角色的随机印记'


def test_todo_lists_unverified_and_unmigrated_series():
    todo = ' '.join(catalog()['todo'])
    for token in ('11100000-11500000', '11000999/31000999', '13000001-13000013', '20000000',
                  '商店/事件尚未接入图标身份'):
        assert token in todo


def _match_score(image, box, icon):
    x, y, width, height = box
    region = image[max(0, y - 4):y + height + 4, max(0, x - 4):x + width + 4]
    best = -1.
    for size in range(width - 12, width + 8, 2):
        template = cv2.resize(icon, (size, size), interpolation=cv2.INTER_AREA)
        if template.shape[0] >= region.shape[0] or template.shape[1] >= region.shape[1]:
            continue
        result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        best = max(best, float(result.max()))
    return best


def test_evidence_frame_pins_the_shop_choice_and_random_series():
    meta = json.loads(MANIFEST.read_text(encoding='utf-8'))['frames'][FRAME.name]
    image = _image(FRAME)
    assert image is not None and image.shape[:2] == (meta['height'], meta['width']) == (720, 1280)
    assert sha256(FRAME.read_bytes()).hexdigest() == meta['sha256']
    icons = {row['summonId']: _image(PACK / row['path']) for row in provenance()['icons']}
    # Row 2 of this shop page: gold 11003000 choice imprint, gray 31004000 random.
    assert _match_score(image, (67, 464, 103, 103), icons[11003000]) >= .80
    assert _match_score(image, (453, 464, 103, 103), icons[31004000]) >= .80
    for other in (31003000, 31002000, 31004000, 31006000):
        assert _match_score(image, (67, 464, 103, 103), icons[other]) <= .65
    for other in (11003000, 11002000, 11004000, 11006000):
        assert _match_score(image, (453, 464, 103, 103), icons[other]) <= .65
