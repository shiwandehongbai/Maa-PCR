"""Synthetic anchor material contracts; these tests do not qualify live UB coverage."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from pcr_maa.clan_battle_opening_observation import OpeningObservation


TOKEN = 'a' * 32
UNITS = [1, 2, 3, 4, 5]
OWNER = object()
PHASES = ('before_start', 'battle', 'paused')


def retained(*, token=TOKEN, phases=PHASES, value_offset=0):
    observer = OpeningObservation.begin(token, UNITS, owner_source=OWNER)
    refs, pixels = {}, {}
    for index, phase in enumerate(phases):
        image = np.full((720, 1280, 3), 20 + index + value_offset, np.uint8)
        refs[phase] = observer.retain_anchor(image, phase, owner_source=OWNER)
        pixels[phase] = image.copy()
    return observer, refs, pixels


def receipt(refs, *, formal=False):
    def node(name, ref):
        return dict(name=name, completed=True, action=dict(name=name, success=True, raw={}),
                    recognition=dict(best=dict(detail=dict(opening_anchor=deepcopy(ref)))))
    pause = 'CBDailyPauseOpening' if formal else 'CBPauseOpening'
    callback = 'CBDailyOpeningBinding' if formal else 'CBOpeningObserve'
    result = dict(native_ok=True, tasker_inited=True, task_id=123, nodes=[
        node('CBStart', refs.get('before_start')),
        node(pause, refs.get('battle')),
        node('CBOpeningRead', refs.get('paused')),
    ])
    result['nodes'][1]['recognition'] = [dict(name=callback,
        detail=dict(best=dict(detail=dict(opening_anchor=deepcopy(refs.get('battle'))))))]
    return result


def save(observer, directory, start_receipt, **changes):
    options = dict(start_token=TOKEN, owner_source=OWNER, source_valid=True,
                   start_receipt=start_receipt)
    options.update(changes)
    return observer.write_anchors(directory, **options)


def no_ub_claim(manifest):
    assert manifest['purpose'] == 'matching_material_only'
    assert manifest['coverage'] == 'unknown'
    assert manifest['first_frame_verified'] is False
    assert manifest['first_ub_verified'] is False
    assert manifest['not_fired_unit_ids'] == []


def file_state(directory):
    return {path.relative_to(directory).as_posix(): (path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest())
            for path in directory.rglob('*') if path.is_file()}


def test_retained_pixels_are_copies_and_only_last_battle_candidate_survives():
    observer = OpeningObservation.begin(TOKEN, UNITS, owner_source=OWNER)
    image = np.zeros((720, 1280, 3), np.uint8)
    before = observer.retain_anchor(image, 'before_start', owner_source=OWNER)
    image[:] = 1
    first = observer.retain_anchor(image, 'battle', owner_source=OWNER)
    image[:] = 2
    latest = observer.retain_anchor(image, 'battle', owner_source=OWNER)
    image[:] = 3
    paused = observer.retain_anchor(image, 'paused', owner_source=OWNER)
    image[:] = 4
    assert set(observer.anchor_frames) == set(PHASES)
    assert all(np.all(observer.anchor_frames[phase] == value)
               for phase, value in [('before_start', 0), ('battle', 2), ('paused', 3)])
    assert first['observation_index'] < latest['observation_index'] < paused['observation_index']
    assert latest['frame_pixel_sha256'] == hashlib.sha256(observer.anchor_frames['battle'].tobytes()).hexdigest()
    assert before['source'] == 'owner_callback_image' and before['capture_interval'] == 'unknown'
    assert type(before['callback_observed_monotonic_ns']) is int
    assert before['shape'] == [720, 1280, 3] and before['dtype'] == 'uint8' and before['color_order'] == 'BGR'
    assert 'capture_utc' not in before and 'lower_ns' not in before and 'upper_ns' not in before
    before['phase'] = 'caller-mutated'
    assert observer.anchor_refs['before_start']['phase'] == 'before_start'


def test_first_paused_anchor_freezes_all_three_slots():
    observer, refs, pixels = retained()
    for phase in PHASES:
        observer.retain_anchor(np.full((720, 1280, 3), 255, np.uint8), phase, owner_source=OWNER)
    assert observer.anchor_refs == refs
    for phase in PHASES:
        assert np.array_equal(observer.anchor_frames[phase], pixels[phase])


@pytest.mark.parametrize('formal', [False, True])
def test_successful_receipt_confirms_exact_three_materials_without_ub_claim(tmp_path, formal):
    observer, refs, pixels = retained()
    directory = tmp_path / TOKEN
    directory.mkdir()
    manifest = save(observer, directory, receipt(refs, formal=formal))
    no_ub_claim(manifest)
    assert manifest['status'] == 'captured' and manifest['source_bound'] is True
    assert manifest['missing_phases'] == []
    assert {row['phase'] for row in manifest['anchors']} == set(PHASES)
    for row in manifest['anchors']:
        assert row['receipt_confirmed'] is True
        assert row['start_token'] == TOKEN
        saved = directory / row['path']
        assert saved.is_relative_to(directory)
        data = saved.read_bytes()
        assert hashlib.sha256(data).hexdigest() == row['png_sha256']
        decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        assert np.array_equal(decoded, pixels[row['phase']])
        assert hashlib.sha256(decoded.tobytes()).hexdigest() == row['frame_pixel_sha256']
    assert observer.anchor_frames == {}
    assert observer.anchor_refs == refs
    again = observer.seal(TOKEN, refs['paused']['frame_pixel_sha256'], owner_source=OWNER, battle_speed=2)
    assert again['coverage'] == 'unknown' and again['not_fired_unit_ids'] == []


@pytest.mark.parametrize('failure', ['native_failed', 'task_missing', 'task_bool', 'start_incomplete',
    'start_action_wrong', 'pause_not_sent', 'battle_hash_wrong', 'battle_sequence_wrong',
    'battle_token_wrong', 'read_missing', 'pause_duplicated', 'out_of_order'])
def test_unproven_receipt_keeps_materials_unconfirmed(tmp_path, failure):
    observer, refs, _ = retained()
    proof = receipt(refs)
    if failure == 'native_failed':
        proof['native_ok'] = False
    elif failure == 'task_missing':
        proof.pop('task_id')
    elif failure == 'task_bool':
        proof['task_id'] = True
    elif failure == 'start_incomplete':
        proof['nodes'][0]['completed'] = False
    elif failure == 'start_action_wrong':
        proof['nodes'][0]['action']['name'] = 'some-other-action'
    elif failure == 'pause_not_sent':
        proof['nodes'][1]['action']['success'] = False
    elif failure.startswith('battle_'):
        ref = proof['nodes'][1]['recognition'][0]['detail']['best']['detail']['opening_anchor']
        field = {'battle_hash_wrong': 'frame_pixel_sha256', 'battle_sequence_wrong': 'observation_index',
                 'battle_token_wrong': 'start_token'}[failure]
        ref[field] = 999 if field == 'observation_index' else 'b' * len(ref[field])
    elif failure == 'read_missing':
        proof['nodes'].pop()
    elif failure == 'pause_duplicated':
        proof['nodes'].insert(2, deepcopy(proof['nodes'][1]))
    else:
        proof['nodes'][0], proof['nodes'][1] = proof['nodes'][1], proof['nodes'][0]
    directory = tmp_path / TOKEN
    directory.mkdir()
    manifest = save(observer, directory, proof)
    no_ub_claim(manifest)
    assert manifest['status'] != 'captured'
    assert any(not row['receipt_confirmed'] for row in manifest['anchors'])


@pytest.mark.parametrize('change', ['token', 'owner', 'directory'])
def test_binding_mismatch_never_writes_old_pixels_to_another_capture(tmp_path, change):
    observer, refs, _ = retained()
    directory = tmp_path / ('b' * 32 if change == 'directory' else TOKEN)
    directory.mkdir()
    changes = {'start_token': 'b' * 32} if change == 'token' else {'owner_source': object()} if change == 'owner' else {}
    with pytest.raises((ValueError, RuntimeError)):
        save(observer, directory, receipt(refs), **changes)
    assert not list(directory.iterdir())


@pytest.mark.parametrize('invalidate', [False, True])
def test_invalid_source_retains_diagnostic_pixels_without_promoting_source_binding(tmp_path, invalidate):
    observer, refs, _ = retained()
    if invalidate:
        observer.invalidate('owner_binding_invalidated')
    directory = tmp_path / TOKEN
    directory.mkdir()
    manifest = save(observer, directory, receipt(refs), source_valid=invalidate)
    no_ub_claim(manifest)
    assert manifest['source_bound'] is False and manifest['status'] != 'captured'
    assert len(manifest['anchors']) == 3


def test_missing_paused_image_is_reported_not_filled_from_battle(tmp_path):
    observer, refs, _ = retained(phases=PHASES[:2])
    directory = tmp_path / TOKEN
    directory.mkdir()
    manifest = save(observer, directory, receipt(refs))
    no_ub_claim(manifest)
    assert manifest['missing_phases'] == ['paused']
    assert manifest['status'] != 'captured'
    assert {row['phase'] for row in manifest['anchors']} == {'before_start', 'battle'}


def test_same_material_finish_retry_reuses_files_without_rewriting(tmp_path):
    observer, refs, _ = retained()
    directory = tmp_path / TOKEN
    directory.mkdir()
    proof = receipt(refs)
    first = save(observer, directory, proof)
    before = file_state(directory)
    again = save(observer, directory, proof)
    assert again == first and file_state(directory) == before
    assert observer.anchor_frames == {}


def test_existing_different_pixels_are_never_overwritten(tmp_path):
    observer, refs, _ = retained()
    directory = tmp_path / TOKEN
    directory.mkdir()
    save(observer, directory, receipt(refs))
    before = file_state(directory)
    other, other_refs, _ = retained(value_offset=50)
    with pytest.raises((ValueError, RuntimeError, FileExistsError)):
        save(other, directory, receipt(other_refs))
    assert file_state(directory) == before


def test_tampered_persisted_pixels_are_rejected_on_cleanup_retry(tmp_path):
    observer, refs, _ = retained()
    directory = tmp_path / TOKEN
    directory.mkdir()
    manifest = save(observer, directory, receipt(refs))
    damaged = directory / manifest['anchors'][0]['path']
    damaged.write_bytes(b'external-change')
    with pytest.raises((ValueError, RuntimeError)):
        save(observer, directory, receipt(refs))
    assert damaged.read_bytes() == b'external-change'


def test_new_observer_never_inherits_previous_start_materials():
    old, _, _ = retained()
    new = OpeningObservation.begin('b' * 32, UNITS, owner_source=OWNER)
    assert not new.anchor_frames and not new.anchor_refs
    assert set(old.anchor_frames) == set(PHASES)


@pytest.mark.parametrize('failure', ['wrong_shape', 'wrong_dtype', 'wrong_owner', 'missing_owner', 'wrong_phase'])
def test_unbound_or_noncanonical_callback_pixels_are_not_retained(failure):
    observer = OpeningObservation.begin(TOKEN, UNITS, owner_source=OWNER)
    image = np.zeros((720, 1280, 3), np.uint8)
    owner, phase = OWNER, 'before_start'
    if failure == 'wrong_shape':
        image = image[:100]
    elif failure == 'wrong_dtype':
        image = image.astype(np.float32)
    elif failure == 'wrong_owner':
        owner = object()
    elif failure == 'missing_owner':
        owner = None
    else:
        phase = 'after_result'
    assert observer.retain_anchor(image, phase, owner_source=owner) is None
    assert not observer.anchor_frames and not observer.anchor_refs



def test_changed_start_receipt_is_rejected_without_rewriting_saved_materials(tmp_path):
    observer, refs, _ = retained()
    directory = tmp_path / TOKEN
    directory.mkdir()
    proof = receipt(refs)
    assert save(observer, directory, proof)['status'] == 'captured'
    before = file_state(directory)
    changed = deepcopy(proof)
    changed['task_id'] += 1
    with pytest.raises(RuntimeError, match='anchor_receipt_changed_after_save'):
        save(observer, directory, changed)
    assert file_state(directory) == before



def test_changed_saved_start_receipt_file_is_rejected_without_rewriting_materials(tmp_path):
    observer, refs, _ = retained()
    directory = tmp_path / TOKEN
    directory.mkdir()
    proof = receipt(refs)
    proof_path = tmp_path / '350-CBStart.json'
    proof_path.write_text(json.dumps(proof), encoding='utf-8')
    assert save(observer, directory, proof, start_receipt_path=proof_path)['status'] == 'captured'
    before = file_state(directory)
    changed = deepcopy(proof)
    changed['native_ok'] = False
    proof_path.write_text(json.dumps(changed), encoding='utf-8')
    changed_bytes = proof_path.read_bytes()
    with pytest.raises(RuntimeError, match='anchor_receipt_file_changed'):
        save(observer, directory, proof, start_receipt_path=proof_path)
    assert file_state(directory) == before
    assert proof_path.read_bytes() == changed_bytes
