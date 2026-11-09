"""Link-imprint badge pack must stay byte-identical to its provenance ledger.

Attribute identity now comes from these five badges, so a silent icon or hash
drift would choose a different attribute. This locks the pack to
``PROVENANCE.json`` and to the five-attribute vocabulary the selection policy
uses.
"""
import hashlib
import json
import re
from pathlib import Path

import pytest

from pcr_maa.labyrinth import links
from pcr_maa.labyrinth.batch import BatchStopped
from pcr_maa.labyrinth.link_imprint import ELEMENTS, SIZE, ImprintCatalog, load_catalog

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets/labyrinth'
PACK = ASSETS / 'link-imprint-pack'
SHA = re.compile(r'^[0-9a-f]{64}$')


def provenance():
    return json.loads((PACK / 'PROVENANCE.json').read_text(encoding='utf-8'))


def test_manifest_names_the_five_attributes_and_pins_each_icon():
    prov = provenance()
    assert prov['schemaVersion'] == 1
    assert prov['pack'] == 'labyrinth-link-imprint-pack'
    assert prov['clientVersion'] == '11.7.2'
    assert prov['distribution_permission_not_inferred'] is True
    assert prov['private_captures_included'] is False and prov['private_uid_included'] is False
    assert prov['sourceIndex']['file'] == '索引.csv'
    assert SHA.fullmatch(prov['sourceIndex']['sha256'])
    assert type(prov['sourceIndex']['bytes']) is int and prov['sourceIndex']['bytes'] > 0
    assert prov['author'] == 'MAA PCR contributors'
    assert set(ELEMENTS) == set(links.ELEMENT_PRIORITY) == {'火', '暗', '光', '风', '水'}
    records = prov['icons']
    assert len(records) == 5
    assert {row['element'] for row in records} == set(ELEMENTS)
    assert len({row['path'] for row in records}) == 5
    expected_sources = {'火': '31100000', '水': '31200000', '风': '31300000',
                        '光': '31400000', '暗': '31500000'}
    for row in records:
        assert re.fullmatch(r'icons/(dark|fire|light|water|wind)\.png', row['path'])
        assert row['path'].isascii()
        assert row['crop'] == [84, 84, 44, 44]
        assert row['sourceId'] == expected_sources[row['element']]
        assert row['sourceBundle'] == f"icon_labyrinth_icon_summon_unit_{row['sourceId']}.unity3d"
        assert SHA.fullmatch(row['sourceSha256']) and type(row['sourceBytes']) is int
        assert row['width'] == row['height'] == SIZE == 44
        assert row['pixelFormat'] == '32bppArgb'
        assert SHA.fullmatch(row['sha256'])


def test_pack_files_match_provenance_both_ways():
    records = {row['path']: row for row in provenance()['icons']}
    files = sorted(path.relative_to(PACK).as_posix() for path in (PACK / 'icons').glob('*.png'))
    assert files == sorted(records)
    for name, record in records.items():
        path = PACK / name
        raw = path.read_bytes()
        assert raw[:8] == b'\x89PNG\r\n\x1a\n'
        width, height = int.from_bytes(raw[16:20], 'big'), int.from_bytes(raw[20:24], 'big')
        assert (width, height) == (record['width'], record['height']) == (SIZE, SIZE)
        assert hashlib.sha256(raw).hexdigest() == record['sha256']
        assert len(raw) == record['bytes'] == path.stat().st_size


def test_loader_reads_the_validated_pack_and_rejects_a_tampered_copy(tmp_path):
    assert load_catalog() is load_catalog()
    copies = tmp_path / 'pack'
    (copies / 'icons').mkdir(parents=True)
    for path in [PACK / 'PROVENANCE.json', *(PACK / 'icons').glob('*.png')]:
        (copies / path.relative_to(PACK)).write_bytes(path.read_bytes())
    assert ImprintCatalog(copies) is not None
    icon = copies / 'icons/fire.png'
    icon.write_bytes(icon.read_bytes() + b'\0')
    with pytest.raises(BatchStopped, match='link_imprint_resource_changed'):
        ImprintCatalog(copies)
    # A manifest that drops one attribute is refused, never degraded to four.
    icon.write_bytes((PACK / 'icons/fire.png').read_bytes())
    data = json.loads((copies / 'PROVENANCE.json').read_text(encoding='utf-8'))
    data['icons'] = data['icons'][:-1]
    (copies / 'PROVENANCE.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(BatchStopped, match='link_imprint_resource_changed'):
        ImprintCatalog(copies)


def test_origin_records_the_card_evidence_and_the_rejected_series():
    origin = provenance()['origin']
    assert '11.7.2' in origin and 'com.bilibili.priconne' in origin
    assert 'icon_labyrinth_icon_summon_unit' in origin
    assert '31xxxxxx' in origin and '11xxxxxx' in origin
    assert '1100x000' in origin
