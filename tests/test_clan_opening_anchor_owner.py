"""Owner/callback anchor wiring with stub inputs; no live device or UB acceptance."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from pcr_maa import clan_battle_daily_native as daily
from pcr_maa import clan_battle_native as backend
from pcr_maa import clan_battle_opening_observation as observation
from pcr_maa import native, supported_daily
from pcr_maa.clan_battle_characters import ClanBattleCharacters


TOKEN = 'a' * 32
UNITS = [1, 2, 3, 4, 5]
PHASES = ('before_start', 'battle', 'paused')


def node(name, detail):
    return dict(name=name, completed=True, action=dict(name=name, success=True, raw={}),
                recognition=dict(best=dict(detail=deepcopy(detail))))


@pytest.fixture(params=[False, True], ids=['simulation', 'formal'])
def owner(tmp_path, monkeypatch, request):
    formal, source_owner = request.param, object()
    state = dict(events=[], runs=0, failure=None)
    expected = (source_owner, 'fixture-adb-not-executed', 'fixture-serial')

    class Capture:
        def __init__(self, adb, serial, display, directory):
            assert (adb, serial, display) == (*expected[1:], '0')
            self.directory, self.released, self.starts = Path(directory), False, 0
        def start(self):
            self.directory.mkdir(parents=True)
            self.starts += 1
            return self
        def stop_and_join(self):
            self.released = True
            state['events'].append('joined')
        def mark(self, phase, name, battle_start):
            state['events'].append((phase, name, battle_start))
        def finish(self, *, source_valid, battle_start):
            assert self.released
            state['events'].append('capture_finished')
            result = dict(status='captured' if source_valid else 'unknown',
                source_bound=source_valid, first_ub_verified=False, battle_start=battle_start)
            (self.directory/'capture.json').write_text(json.dumps(result), encoding='utf-8')
            return result

    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_ONLY', '1')
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_FORMAT', 'png')
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_DIR', str(tmp_path/'materials'))
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_DISPLAY_ID', '0')
    monkeypatch.setattr(observation, 'OpeningCaptureMaterials', Capture)
    cls = daily.NativeClanDailyAdapter if formal else backend.NativeClanBattle
    port = object.__new__(cls)
    backend.NativeClanBattle.__init__(port, NS(resource=NS(override_pipeline=lambda _: True)), tmp_path/'run')
    port.directory.mkdir()
    port.plan, port._account_binding = {'switches': []}, source_owner
    port.pending = dict(intent_id='fixture-intent', action={'start_seconds': 90})
    port.action = {'kind': 'regular'}
    reader_cls = daily._DailyTeamReader if formal else ClanBattleCharacters
    reader = object.__new__(reader_cls)
    reader.team, reader.plan = UNITS, port.plan
    reader.calibration = dict(auto_battle={'paused_guards': [{}, {}, {'roi': [1, 1, 1, 1]}]},
        daily={'execution': {'opening': {'clock': [1, 1, 1, 1]}}})
    reader.page = NS(port=port, modern=False, current='formation', expected=None,
        current_page='formation', expected_page=None, require_team=lambda _: None,
        action=lambda *_: [10, 10, 1, 1], word=lambda *_: '1:28',
        hit=lambda context, image, name: name != 'LabyDisconnect6002Observed')
    reader.formation = lambda *_: dict(unit_ids=UNITS, stars=[5]*5, member_count=5, support_unit_id=None)
    reader.read_opening_clock = lambda *_: (88, {})
    reader.read_paused_controls = lambda context, image, **kwargs: dict(unit_ids=UNITS,
        sets=[True]*5, auto=True, battle_speed=2, remaining_seconds=kwargs['remaining_seconds'],
        same_frame=True, paused=True, frame_pixel_sha256=hashlib.sha256(image.tobytes()).hexdigest())
    port.page, port.characters = reader.page, reader
    monkeypatch.setattr(port, 'opening_binding', lambda **kwargs: port.pending, raising=False)
    monkeypatch.setattr(port, 'freeze_opening_control_policy', lambda _: None)
    def source():
        state['events'].append('source_checked')
        return expected
    monkeypatch.setattr(port, '_opening_capture_source', source)

    def native_start(self, name, *, click=False, reset_hits=()):
        assert self is port and name == 'CBStart' and click
        if formal:
            assert reset_hits == ('CBDailyPauseOpening', 'CBDailyPauseRetry')
        state['runs'] += 1
        if state['failure'] is not None:
            raise state['failure']
        results, pixels = [], {}
        modes = [('start', 'before_start'), ('formal_pause' if formal else 'opening_observe', 'battle'),
                 ('opening_read', 'paused')]
        for index, (mode, phase) in enumerate(modes):
            image = np.full((720, 1280, 3), index+1, np.uint8)
            params = dict(mode=mode, **({'opening_start_token': TOKEN} if mode == 'start' else {}))
            result = reader.analyze(NS(tasker=NS(stopping=False)),
                NS(image=image, custom_recognition_param=json.dumps(params)))
            assert result is not None and result.detail['opening_anchor']['phase'] == phase
            results.append(result.detail)
            pixels[phase] = image.copy()
            image[:] = 255
        pause, callback = ('CBDailyPauseOpening', 'CBDailyOpeningBinding') if formal else ('CBPauseOpening', 'CBOpeningObserve')
        receipt = dict(native_ok=True, tasker_inited=True, task_id=123, nodes=[
            node('CBStart', results[0]), node(pause, {}), node('CBOpeningRead', results[2])])
        receipt['nodes'][1]['recognition'] = [dict(name=callback, detail=dict(best=dict(detail=results[1])))]
        self.sequence += 1
        path = self.directory/f'{self.sequence:03d}-CBStart.json'
        path.write_text(json.dumps(receipt), encoding='utf-8')
        state.update(pixels=pixels, receipt=deepcopy(receipt), receipt_path=path)
        return receipt

    monkeypatch.setattr(backend.NativeGuideTrial, 'run', native_start)
    port.arm_opening_start(TOKEN)
    return NS(port=port, reader=reader, observer=reader.opening_observer, capture=port._opening_capture,
              expected=expected, state=state, formal=formal)


def test_start_callbacks_bind_exact_three_images_and_receipt_to_original_owner(owner):
    port, state = owner.port, owner.state
    assert port._opening_capture_observer is owner.observer
    returned = port.run('CBStart', click=True)
    assert port._opening_anchor_start_receipt == state['receipt']
    assert port._opening_anchor_start_receipt_path == state['receipt_path']
    returned['nodes'][0]['recognition']['best']['detail']['opening_anchor']['phase'] = 'caller-mutated'
    assert port._opening_anchor_start_receipt == state['receipt']
    for phase in PHASES:
        assert np.array_equal(owner.observer.anchor_frames[phase], state['pixels'][phase])
    # A later callback wrapper replacement must not redirect the capture's original observer.
    owner.reader.opening_observer = observation.OpeningObservation.begin('other', UNITS, owner_source=object())
    state['events'].clear()
    result = port.finish_opening_capture()
    assert state['events'][:3] == ['joined', 'source_checked', 'capture_finished']
    assert result['anchors']['status'] == 'captured'
    manifest = json.loads((owner.capture.directory/'anchors.json').read_text(encoding='utf-8'))
    assert all(row['receipt_confirmed'] for row in manifest['anchors'])
    assert manifest['start_receipt']['file_sha256'] == hashlib.sha256(state['receipt_path'].read_bytes()).hexdigest()
    assert manifest['coverage'] == 'unknown' and manifest['not_fired_unit_ids'] == []
    assert manifest['first_ub_verified'] is False
    link = json.loads((port.directory/'opening-capture.json').read_text(encoding='utf-8'))
    assert link['anchors']['sha256'] == hashlib.sha256((owner.capture.directory/'anchors.json').read_bytes()).hexdigest()
    assert owner.capture.released and port._opening_capture is None and port._opening_capture_observer is None
    assert port.finish_opening_capture() is None and state['runs'] == 1


@pytest.mark.parametrize('failure_stage', ['after_anchors', 'summary_link'])
def test_finish_failure_keeps_released_owner_for_idempotent_material_retry(owner, monkeypatch, failure_stage):
    port, state = owner.port, owner.state
    port.run('CBStart', click=True)
    if failure_stage == 'after_anchors':
        original = owner.observer.write_anchors
        def fail_once(*args, **kwargs):
            result = original(*args, **kwargs)
            monkeypatch.setattr(owner.observer, 'write_anchors', original)
            raise OSError('fixture material finish interrupted')
        monkeypatch.setattr(owner.observer, 'write_anchors', fail_once)
    else:
        original = supported_daily._atomic_json
        def fail_once(*args, **kwargs):
            monkeypatch.setattr(supported_daily, '_atomic_json', original)
            raise OSError('fixture summary link interrupted')
        monkeypatch.setattr(supported_daily, '_atomic_json', fail_once)
    with pytest.raises(OSError, match='fixture'):
        port.finish_opening_capture()
    assert owner.capture.released and port._opening_capture is owner.capture
    assert port._opening_capture_observer is owner.observer
    files = [*owner.capture.directory.glob('anchor-*.png'), owner.capture.directory/'anchors.json']
    before = {p.name: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in files}
    result = port.finish_opening_capture()
    assert result['anchors']['status'] == 'captured'
    assert before == {p.name: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in files}
    assert state['runs'] == 1 and owner.capture.starts == 1
    assert port._opening_capture is None and port._opening_capture_observer is None


@pytest.mark.parametrize('source_failure', ['changed', 'unreadable'])
def test_final_owner_source_change_keeps_materials_unknown(owner, monkeypatch, source_failure):
    port = owner.port
    port.run('CBStart', click=True)
    def source():
        assert owner.capture.released
        if source_failure == 'unreadable':
            raise RuntimeError('fixture source unavailable')
        return (object(), *owner.expected[1:])
    monkeypatch.setattr(port, '_opening_capture_source', source)
    result = port.finish_opening_capture()
    manifest = json.loads((owner.capture.directory/'anchors.json').read_text(encoding='utf-8'))
    assert result['source_bound'] is False and result['anchors']['status'] == 'unknown'
    assert manifest['source_bound'] is False and manifest['status'] == 'unknown'
    assert manifest['coverage'] == 'unknown' and manifest['not_fired_unit_ids'] == []
    assert port._opening_capture is None and owner.capture.released


@pytest.mark.parametrize('failure', ['not_sent', 'unknown'])
def test_failed_start_has_no_successful_receipt_binding_and_cannot_restart(owner, failure):
    port, state = owner.port, owner.state
    state['failure'] = (native.NativeInputNotSent({'input_execution': 'not_sent'}) if failure == 'not_sent'
                        else RuntimeError('fixture unknown start'))
    with pytest.raises(RuntimeError):
        port.run('CBStart', click=True)
    assert port._opening_anchor_start_receipt is None and port._opening_anchor_start_receipt_path is None
    with pytest.raises(RuntimeError):
        port.run('CBStart', click=True)
    result = port.finish_opening_capture()
    assert state['runs'] == 1 and owner.capture.released
    assert result['anchors']['status'] == 'unknown'
    assert port.battle_start == ('not_sent' if failure == 'not_sent' else 'unknown')
