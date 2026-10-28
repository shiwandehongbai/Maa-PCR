"""Opening-only UB coverage. Unknown capture/visibility never proves no UB."""
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import struct
import subprocess
import time


def _digest(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


class OpeningObservation:
    @classmethod
    def begin(cls, start_token, unit_ids, *, owner_source=None, scope_sha256=None, calibration=None):
        if (type(start_token) is not str or not start_token or type(unit_ids) is not list
                or len(unit_ids) != 5 or len(set(unit_ids)) != 5
                or any(type(uid) is not int or uid <= 0 for uid in unit_ids)):
            raise ValueError('clan_battle_opening_observation_binding_invalid')
        self = cls()
        self.start_token, self.units, self.owner_source = start_token, list(unit_ids), owner_source
        self.scope_sha256, self.calibration = scope_sha256, deepcopy(calibration)
        self.first, self.last, self.previous_capture = None, None, None
        self.samples, self.max_gap_ns = 0, 0
        self.fired, self.unclear, self.reasons, self.capture_ids = set(), set(), set(), set()
        self.sealed = None
        self.anchor_frames, self.anchor_refs, self.anchor_sequence = {}, {}, 0
        self.anchor_manifest, self.anchors_invalidated = None, False
        if owner_source is None:
            self.reasons.add('owner_source_unknown')
        return self

    def invalidate(self, reason):
        self.anchors_invalidated = True
        self.reasons.add(reason)
        self.sealed = None

    def retain_anchor(self, image, phase, *, owner_source):
        """Keep at most three callback images; callback time is not capture time."""
        if (phase not in ('before_start', 'battle', 'paused')
                or owner_source is None or owner_source != self.owner_source
                or getattr(image, 'shape', None) != (720, 1280, 3)
                or str(getattr(image, 'dtype', None)) != 'uint8'):
            return None
        digest = hashlib.sha256(image.tobytes()).hexdigest()
        if 'paused' in self.anchor_refs:
            ref = self.anchor_refs.get(phase)
            return deepcopy(ref) if phase == 'paused' and ref['frame_pixel_sha256'] == digest else None
        self.anchor_sequence += 1
        ref = dict(phase=phase, start_token=self.start_token, observation_index=self.anchor_sequence,
            frame_pixel_sha256=digest, callback_observed_monotonic_ns=time.monotonic_ns(),
            source='owner_callback_image', capture_interval='unknown', shape=[720, 1280, 3],
            dtype='uint8', color_order='BGR')
        self.anchor_frames[phase] = image.copy(order='C')
        self.anchor_refs[phase] = ref
        return deepcopy(ref)

    def write_anchors(self, directory, *, start_token, owner_source, source_valid,
            start_receipt, start_receipt_path=None):
        """Persist matching materials, never event coverage or a no-UB conclusion."""
        import cv2
        import json
        directory = Path(directory)
        if (start_token != self.start_token or directory.name != start_token
                or owner_source is None or owner_source != self.owner_source or not directory.is_dir()):
            raise RuntimeError('clan_battle_opening_anchor_binding_changed')
        bound = source_valid is True and not self.anchors_invalidated
        if self.anchor_manifest is not None:
            if self.anchor_manifest['source_bound'] != bound:
                raise RuntimeError('clan_battle_opening_anchor_source_changed_after_save')
            receipt = start_receipt if isinstance(start_receipt, dict) else {}
            receipt_hash = hashlib.sha256(json.dumps(receipt, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if receipt_hash != self.anchor_manifest['start_receipt']['canonical_json_sha256']:
                raise RuntimeError('clan_battle_opening_anchor_receipt_changed_after_save')
            saved_receipt = self.anchor_manifest['start_receipt']
            if ('path' in saved_receipt and hashlib.sha256(Path(saved_receipt['path']).read_bytes()).hexdigest() != saved_receipt['file_sha256']):
                raise RuntimeError('clan_battle_opening_anchor_receipt_file_changed')
            for row in self.anchor_manifest['anchors']:
                if hashlib.sha256((directory/row['path']).read_bytes()).hexdigest() != row['png_sha256']:
                    raise RuntimeError('clan_battle_opening_anchor_file_changed')
            expected = (json.dumps(self.anchor_manifest, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf-8')
            if (directory/'anchors.json').read_bytes() != expected:
                raise RuntimeError('clan_battle_opening_anchor_manifest_changed')
            return deepcopy(self.anchor_manifest)

        def detail(value):
            if isinstance(value, str):
                try: value = json.loads(value)
                except (TypeError, ValueError): return {}
            return value if isinstance(value, dict) else {}

        def reference(node, callback=None):
            action = detail(node.get('action'))
            if (node.get('completed') is not True or action.get('success') is not True
                    or action.get('name') != node.get('name')):
                return None
            recognition = node.get('recognition')
            if callback:
                rows = [row for row in recognition if isinstance(row, dict) and row.get('name') == callback] if isinstance(recognition, list) else []
                if len(rows) != 1: return None
                recognition = detail(rows[0].get('detail'))
            return detail(detail(detail(recognition).get('best')).get('detail')).get('opening_anchor')

        receipt = start_receipt if isinstance(start_receipt, dict) else {}
        nodes = receipt.get('nodes', [])
        nodes = nodes if isinstance(nodes, list) else []
        selected = {}
        for phase, names in [('before_start', ('CBStart',)), ('battle', ('CBPauseOpening', 'CBDailyPauseOpening')),
                ('paused', ('CBOpeningRead',))]:
            hits = [(index, row) for index, row in enumerate(nodes) if isinstance(row, dict) and row.get('name') in names]
            if len(hits) == 1: selected[phase] = hits[0]
        receipt_ok = (receipt.get('native_ok') is True and type(receipt.get('task_id')) is int
            and receipt['task_id'] > 0 and len(selected) == 3
            and selected['before_start'][0] < selected['battle'][0] < selected['paused'][0])
        receipt_evidence = dict(task_id=receipt.get('task_id'), native_ok=receipt.get('native_ok'),
            canonical_json_sha256=hashlib.sha256(json.dumps(receipt, sort_keys=True, allow_nan=False).encode()).hexdigest())
        if start_receipt_path is not None:
            raw = Path(start_receipt_path).read_bytes()
            if json.loads(raw) != receipt: receipt_ok = False
            receipt_evidence.update(path=str(start_receipt_path), file_sha256=hashlib.sha256(raw).hexdigest())

        def exclusive(path, data):
            try:
                with path.open('xb') as stream: stream.write(data)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise RuntimeError('clan_battle_opening_anchor_file_conflict') from None

        rows = []
        for phase in ('before_start', 'battle', 'paused'):
            if phase not in self.anchor_frames: continue
            image, ref = self.anchor_frames[phase], self.anchor_refs[phase]
            if hashlib.sha256(image.tobytes()).hexdigest() != ref['frame_pixel_sha256']:
                raise RuntimeError('clan_battle_opening_anchor_pixels_changed')
            ok, encoded = cv2.imencode('.png', image)
            if not ok: raise RuntimeError('clan_battle_opening_anchor_encode_failed')
            data, name = encoded.tobytes(), 'anchor-'+phase+'.png'
            exclusive(directory/name, data)
            node = selected.get(phase, (None, {}))[1]
            callback = ('CBOpeningObserve' if node.get('name') == 'CBPauseOpening' else 'CBDailyOpeningBinding') if phase == 'battle' else None
            confirmed = receipt_ok and reference(node, callback) == ref
            rows.append(dict(ref, path=name, png_sha256=hashlib.sha256(data).hexdigest(), receipt_confirmed=confirmed))
        missing = [phase for phase in ('before_start', 'battle', 'paused') if phase not in self.anchor_frames]
        manifest = dict(purpose='matching_material_only', start_token=self.start_token, source_bound=bound,
            coverage='unknown', first_frame_verified=False, first_ub_verified=False, not_fired_unit_ids=[],
            anchors=rows, missing_phases=missing, start_receipt=receipt_evidence,
            status='captured' if bound and not missing and all(row['receipt_confirmed'] for row in rows) else 'unknown')
        exclusive(directory/'anchors.json', (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf-8'))
        self.anchor_manifest = deepcopy(manifest)
        self.anchor_frames.clear()
        return manifest

    def feed(self, frame_sha256, phase, *, owner_source, capture=None, events=None):
        if self.sealed is not None:
            self.invalidate('observation_after_seal')
            return
        if not _digest(frame_sha256) or phase not in ('before_start', 'battle', 'paused'):
            self.reasons.add('frame_binding_invalid')
            return
        self.samples += 1
        if owner_source is None or owner_source != self.owner_source:
            self.reasons.add('owner_source_changed')
        if self.first is None:
            self.first = dict(phase=phase, frame_sha256=frame_sha256)
            if phase != 'before_start':
                self.reasons.add('start_boundary_missing')
        elif self.last['phase'] == 'paused' or phase == 'before_start':
            self.reasons.add('phase_order_unknown')
        self.last = dict(phase=phase, frame_sha256=frame_sha256)
        if (type(capture) is not dict or capture.get('source') != 'adb_command_screencap'
                or capture.get('frame_pixel_sha256') != frame_sha256
                or type(capture.get('capture_id')) is not str or not capture['capture_id']
                or type(capture.get('lower_ns')) is not int or type(capture.get('upper_ns')) is not int
                or not 0 <= capture['lower_ns'] <= capture['upper_ns']):
            self.reasons.add('capture_interval_unknown')
        else:
            key = capture['capture_id']
            if key in self.capture_ids:
                self.reasons.add('capture_reused')
            if self.samples > 4096:
                self.reasons.add('opening_observation_limit')
            else:
                self.capture_ids.add(key)
            if self.previous_capture is not None:
                if capture['lower_ns'] < self.previous_capture['upper_ns']:
                    self.reasons.add('capture_order_unknown')
                self.max_gap_ns = max(self.max_gap_ns, capture['upper_ns'] - self.previous_capture['lower_ns'])
            self.previous_capture = dict(capture)
        if phase != 'before_start':
            if type(events) is not dict or set(events) != {str(uid) for uid in self.units}:
                self.unclear.update(self.units)
            else:
                for uid in self.units:
                    status = events[str(uid)]
                    if status == 'ub_started':
                        self.fired.add(uid)
                    elif status != 'clear':
                        self.unclear.add(uid)

    def capture_qualified(self, battle_speed):
        """Check the internal capture prerequisites; this alone grants no proof."""
        calibration = self.calibration
        return (type(calibration) is dict and calibration.get('source') == 'adb_command_screencap'
            and _digest(calibration.get('capture_calibration_sha256'))
            and _digest(calibration.get('marker_calibration_sha256'))
            and _digest(self.scope_sha256) and calibration.get('scope_sha256') == self.scope_sha256
            and type(calibration.get('min_visible_ns')) is int and calibration['min_visible_ns'] > 0
            and type(calibration.get('unit_ids')) is list
            and all(type(uid) is int for uid in calibration['unit_ids'])
            and set(calibration['unit_ids']) == set(self.units)
            and type(calibration.get('speeds')) is list
            and all(type(speed) is int and speed in (1, 2, 4) for speed in calibration['speeds'])
            and type(battle_speed) is int and battle_speed in calibration['speeds']
            and calibration.get('pause_keeps_event_visible') is True)

    def pending(self, start_token, frame_sha256, *, owner_source, battle_speed):
        """Report the pause anchor without sealing or claiming timed coverage."""
        reasons = set(self.reasons) | {'capture_pending', 'capture_interval_unknown'}
        if start_token != self.start_token or owner_source is None or owner_source != self.owner_source:
            reasons.add('seal_binding_changed')
        return dict(source='continuous_opening_observation', start_token=self.start_token,
            initial_frame_pixel_sha256=frame_sha256, owner_bound=self.owner_source is not None,
            coverage='unknown', reasons=sorted(reasons), frame_count=self.samples,
            max_gap_ns=self.max_gap_ns, calibration=deepcopy(self.calibration), scope_sha256=self.scope_sha256,
            battle_speed=battle_speed, not_fired_unit_ids=[], fired_unit_ids=[], unknown_unit_ids=list(self.units),
            before_start_frame_sha256=self.first['frame_sha256'] if self.first else None,
            paused_frame_sha256=frame_sha256)

    def seal(self, start_token, frame_sha256, *, owner_source, battle_speed):
        if start_token != self.start_token or owner_source is None or owner_source != self.owner_source:
            self.invalidate('seal_binding_changed')
        if self.sealed is not None:
            return deepcopy(self.sealed)
        if self.last is None or self.last != dict(phase='paused', frame_sha256=frame_sha256):
            self.reasons.add('pause_boundary_missing')
        calibration = self.calibration
        if (type(calibration) is not dict or calibration.get('source') != 'adb_command_screencap'
                or not _digest(calibration.get('capture_calibration_sha256'))
                or not _digest(calibration.get('marker_calibration_sha256'))
                or not _digest(self.scope_sha256) or calibration.get('scope_sha256') != self.scope_sha256
                or type(calibration.get('min_visible_ns')) is not int or calibration['min_visible_ns'] <= 0
                or type(calibration.get('unit_ids')) is not list
                or any(type(uid) is not int for uid in calibration['unit_ids'])
                or set(calibration['unit_ids']) != set(self.units)
                or type(calibration.get('speeds')) is not list
                or any(type(speed) is not int or speed not in (1, 2, 4) for speed in calibration['speeds'])
                or type(battle_speed) is not int
                or battle_speed not in calibration['speeds']):
            self.reasons.add('visibility_or_capture_calibration_missing')
        else:
            if self.samples < 2 or self.max_gap_ns >= calibration['min_visible_ns']:
                self.reasons.add('event_can_fit_between_captures')
            # A pause may hide a newly started UB before the next frame can expose it.
            if calibration.get('pause_keeps_event_visible') is not True:
                self.reasons.add('pause_tail_unknown')
        complete = not self.reasons
        not_fired = [uid for uid in self.units if complete and uid not in self.fired | self.unclear]
        self.sealed = dict(source='continuous_opening_observation', start_token=self.start_token,
            initial_frame_pixel_sha256=frame_sha256, owner_bound=self.owner_source is not None,
            coverage='complete' if complete else 'unknown', reasons=sorted(self.reasons),
            frame_count=self.samples, max_gap_ns=self.max_gap_ns,
            calibration=deepcopy(calibration), scope_sha256=self.scope_sha256, battle_speed=battle_speed,
            not_fired_unit_ids=not_fired, fired_unit_ids=sorted(self.fired),
            unknown_unit_ids=[uid for uid in self.units if uid not in set(not_fired) | self.fired],
            before_start_frame_sha256=self.first['frame_sha256'] if self.first else None,
            paused_frame_sha256=frame_sha256)
        return deepcopy(self.sealed)


def capture_adb_png(adb_path, serial, display_id, *, timeout):
    """New process stdout plus host call bounds; no SDK object or claimed renderer sequence."""
    import zlib
    if (not isinstance(adb_path, (str, Path)) or not Path(adb_path).is_file()
            or type(serial) is not str or not serial or type(display_id) is not str
            or re.fullmatch('[0-9]+', display_id) is None
            or type(timeout) not in (int, float) or not 0 < timeout <= 30):
        raise ValueError('clan_battle_opening_capture_binding_invalid')
    command = [str(adb_path), '-s', serial, 'exec-out', 'screencap', '-p', '-d', display_id]
    lower = time.monotonic_ns()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        data, _ = process.communicate(timeout=timeout)
    except BaseException:
        process.kill()
        process.communicate(timeout=2)
        raise
    upper = time.monotonic_ns()
    if (process.returncode != 0 or not 33 <= len(data) <= 32*1024*1024
            or not data.startswith(b'\x89PNG\r\n\x1a\n') or data[12:16] != b'IHDR'):
        raise RuntimeError('clan_battle_opening_capture_invalid')
    width, height = struct.unpack('>II', data[16:24])
    if not width or not height or width*9 != height*16 or width*height > 16777216:
        raise RuntimeError('clan_battle_opening_capture_layout_unknown')
    offset, idat, ended = 8, False, False
    while offset + 12 <= len(data):
        size = struct.unpack('>I', data[offset:offset+4])[0]
        end = offset + size + 12
        if end > len(data) or zlib.crc32(data[offset+4:end-4]) & 0xffffffff != struct.unpack('>I',data[end-4:end])[0]:
            raise RuntimeError('clan_battle_opening_capture_png_corrupt')
        kind = data[offset+4:offset+8]
        idat |= kind == b'IDAT'
        offset = end
        if kind == b'IEND':
            ended = size == 0 and end == len(data)
            break
    if not idat or not ended:
        raise RuntimeError('clan_battle_opening_capture_png_corrupt')
    packet = dict(source='adb_command_screencap', capture_id=f'{process.pid}:{lower}',
        lower_ns=lower, upper_ns=upper, png_sha256=hashlib.sha256(data).hexdigest(),
        physical_size=[width,height], display_id=display_id)
    return data, packet


def decode_capture(data, packet):
    import cv2
    import numpy as np
    if hashlib.sha256(data).hexdigest() != packet.get('png_sha256'):
        raise RuntimeError('clan_battle_opening_capture_png_changed')
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    width, height = packet['physical_size']
    if image is None or image.shape != (height, width, 3):
        raise RuntimeError('clan_battle_opening_capture_invalid')
    raw_hash = hashlib.sha256(image.tobytes()).hexdigest()
    image = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
    return image, dict(packet, raw_frame_pixel_sha256=raw_hash,
        frame_pixel_sha256=hashlib.sha256(image.tobytes()).hexdigest())


def bind_screencap_materials(directory, capture, *, display_id, owner_bound):
    """Bind stored PNG pixels to their own command intervals, never to UB coverage."""
    import cv2
    unknown = dict(status='unknown', coverage='unknown', first_ub_verified=False,
        not_fired_unit_ids=[], frames=[], time_semantics='host_command_interval_only')
    if (type(capture) is not dict or capture.get('format') not in (None, 'png') or owner_bound is not True
            or type(display_id) is not str or re.fullmatch('[0-9]+', display_id) is None
            or capture.get('status') != 'captured' or capture.get('source_bound') is not True):
        return dict(unknown, reason='capture_source_or_format_unverified')
    rows, markers = capture.get('frames'), capture.get('markers')
    if type(rows) is not list or not 1 <= len(rows) <= 600 or type(markers) is not list:
        return dict(unknown, reason='capture_rows_or_markers_missing')
    starts = [row for row in markers if type(row) is dict and row.get('phase') == 'dispatch' and row.get('node') == 'CBStart']
    ends = [row for row in markers if type(row) is dict and row.get('phase') == 'returned' and row.get('node') == 'CBStart']
    if (len(starts) != 1 or len(ends) != 1 or type(starts[0].get('host_monotonic_ns')) is not int
            or type(ends[0].get('host_monotonic_ns')) is not int
            or starts[0].get('meaning') != 'business_call_boundary_not_capture_pts'
            or ends[0].get('meaning') != 'business_call_boundary_not_capture_pts'
            or ends[0].get('battle_start') != 'sent'
            or not 0 <= starts[0]['host_monotonic_ns'] < ends[0]['host_monotonic_ns']):
        return dict(unknown, reason='start_boundaries_unknown')
    directory = Path(directory).resolve()
    frames, ids, previous_upper, max_gap = [], set(), None, 0
    try:
        for index, packet in enumerate(rows):
            if (type(packet) is not dict or packet.get('source') != 'adb_command_screencap'
                    or packet.get('display_id') != display_id
                    or packet.get('path') != f'capture-{index:04d}.png'
                    or type(packet.get('capture_id')) is not str or not packet['capture_id']
                    or packet['capture_id'] in ids or type(packet.get('lower_ns')) is not int
                    or type(packet.get('upper_ns')) is not int
                    or not 0 <= packet['lower_ns'] <= packet['upper_ns']
                    or previous_upper is not None and packet['lower_ns'] < previous_upper):
                raise ValueError('capture_frame_order_or_identity_unknown')
            size = packet.get('physical_size')
            if (type(size) is not list or len(size) != 2 or any(type(value) is not int or value <= 0 for value in size)
                    or size[0]*9 != size[1]*16 or size[0]*size[1] > 16777216):
                raise ValueError('capture_frame_layout_unknown')
            path = directory / packet['path']
            if (path.is_symlink() or not path.is_file() or path.resolve().parent != directory
                    or not 0 < path.stat().st_size <= 32*1024*1024):
                raise ValueError('capture_frame_path_unknown')
            data = path.read_bytes()
            _, decoded = decode_capture(data, packet)
            if frames:
                max_gap = max(max_gap, packet['upper_ns'] - frames[-1]['lower_ns'])
            frames.append(dict(capture_id=packet['capture_id'], lower_ns=packet['lower_ns'],
                upper_ns=packet['upper_ns'], png_sha256=packet['png_sha256'],
                frame_pixel_sha256=decoded['frame_pixel_sha256']))
            ids.add(packet['capture_id'])
            previous_upper = packet['upper_ns']
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, cv2.error):
        return dict(unknown, reason='capture_frame_unverified')
    pre_start = any(row['upper_ns'] <= starts[0]['host_monotonic_ns'] for row in frames)
    post_return = any(row['lower_ns'] >= ends[0]['host_monotonic_ns'] for row in frames)
    if not pre_start or not post_return:
        return dict(unknown, reason='capture_boundaries_unverified')
    return dict(unknown, status='bound_material_only', frames=frames, max_gap_ns=max_gap,
        start_dispatch_ns=starts[0]['host_monotonic_ns'], start_return_ns=ends[0]['host_monotonic_ns'])


class OpeningCaptureMaterials:
    """Capture-only worker; Owner/source checks remain on its creating thread."""
    def __init__(self, adb_path, serial, display_id, directory, *, max_frames=600, max_seconds=60):
        import threading
        self.adb_path, self.serial, self.display_id = adb_path, serial, display_id
        self.directory, self.max_frames, self.max_seconds = Path(directory), max_frames, max_seconds
        self.stop, self.ready, self.rows, self.error, self.bytes = threading.Event(), threading.Event(), [], None, 0
        self.markers = []
        self.thread = threading.Thread(target=self._run, name='clan-opening-capture', daemon=False)

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        self.thread.start()
        if not self.ready.wait(timeout=8) or not self.rows:
            self.stop_and_join()
            raise RuntimeError('clan_battle_opening_capture_first_frame_missing')
        return self

    def _run(self):
        deadline = time.monotonic() + self.max_seconds
        try:
            for index in range(self.max_frames):
                if self.stop.is_set() or time.monotonic() >= deadline:
                    break
                tick = time.monotonic()
                data, packet = capture_adb_png(self.adb_path, self.serial, self.display_id,
                    timeout=min(5, max(.01, deadline-time.monotonic())))
                self.bytes += len(data)
                if self.bytes > 256*1024*1024:
                    raise RuntimeError('clan_battle_opening_capture_material_limit')
                name = f'capture-{index:04d}.png'
                (self.directory/name).write_bytes(data)
                self.rows.append(dict(packet, path=name))
                self.ready.set()
                # Capture-only resource bound; actual intervals, not this cadence, enter coverage checks.
                self.stop.wait(max(0, .1-(time.monotonic()-tick)))
        except Exception as exc:
            self.error = f'{type(exc).__name__}:{exc}'
        finally:
            self.ready.set()

    def mark(self, phase, node, battle_start):
        try:
            self.markers.append(dict(phase=phase, node=node, battle_start=battle_start,
                host_monotonic_ns=time.monotonic_ns(), meaning='business_call_boundary_not_capture_pts'))
        except Exception as exc:
            self.error = 'capture_marker_failed:' + type(exc).__name__

    def stop_and_join(self):
        self.stop.set()
        self.thread.join(timeout=8)
        if self.thread.is_alive():
            raise RuntimeError('clan_battle_opening_capture_worker_not_released')

    def finish(self, *, source_valid, battle_start):
        import json
        self.stop_and_join()
        result = dict(purpose='capture_only_not_ub_acceptance', source_bound=source_valid is True,
            battle_start=battle_start, first_ub_verified=False, frames=self.rows, markers=self.markers, error=self.error,
            status='captured' if source_valid is True and self.rows and self.error is None else 'unknown')
        (self.directory/'capture.json').write_bytes((json.dumps(result,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
        return result


def _mp4_boxes(data, start, end):
    while start < end:
        if start + 8 > end:
            raise ValueError('screenrecord_box_truncated')
        size, kind = struct.unpack_from('>I4s', data, start)
        header = 8
        if size == 1:
            if start + 16 > end:
                raise ValueError('screenrecord_large_box_truncated')
            size, header = struct.unpack_from('>Q', data, start+8)[0], 16
        elif size == 0:
            size = end-start
        if size < header or start+size > end:
            raise ValueError('screenrecord_box_size_invalid')
        yield kind, start+header, start+size
        start += size


def _mp4_one(data, parent, kind):
    matches = [box for box in _mp4_boxes(data, parent[1], parent[2]) if box[0] == kind]
    if len(matches) != 1:
        raise ValueError('screenrecord_box_not_unique')
    return matches[0]


def read_screenrecord_metadata(path):
    """Read the single mett sample through its MP4 tables, without ffmpeg or timestamp repair."""
    path = Path(path)
    if not 0 < path.stat().st_size <= 256*1024*1024:
        raise ValueError('screenrecord_media_size_invalid')
    data = path.read_bytes()
    top = list(_mp4_boxes(data, 0, len(data)))
    movies = [box for box in top if box[0] == b'moov']
    media_ranges = [(box[1], box[2]) for box in top if box[0] == b'mdat']
    if len(movies) != 1 or not media_ranges:
        raise ValueError('screenrecord_finalized_mp4_required')
    results = []
    for track in (box for box in _mp4_boxes(data, movies[0][1], movies[0][2]) if box[0] == b'trak'):
        mdia = _mp4_one(data, track, b'mdia')
        stbl = _mp4_one(data, _mp4_one(data, mdia, b'minf'), b'stbl')
        stsd = _mp4_one(data, stbl, b'stsd')
        if stsd[2]-stsd[1] < 8 or struct.unpack_from('>I', data, stsd[1]+4)[0] != 1:
            continue
        entries = list(_mp4_boxes(data, stsd[1]+8, stsd[2]))
        if len(entries) != 1 or entries[0][0] != b'mett':
            continue
        stsz = _mp4_one(data, stbl, b'stsz')
        if stsz[2]-stsz[1] < 12:
            raise ValueError('screenrecord_stsz_truncated')
        size, count = struct.unpack_from('>II', data, stsz[1]+4)
        if count != 1:
            raise ValueError('screenrecord_mett_single_sample_required')
        if size == 0 and stsz[2]-stsz[1] == 16:
            size = struct.unpack_from('>I', data, stsz[1]+12)[0]
        elif size == 0 or stsz[2]-stsz[1] != 12:
            raise ValueError('screenrecord_mett_sample_size_invalid')
        stsc = _mp4_one(data, stbl, b'stsc')
        if stsc[2]-stsc[1] != 20 or struct.unpack_from('>4I', data, stsc[1]+4) != (1,1,1,1):
            raise ValueError('screenrecord_mett_chunk_mapping_unknown')
        tables = [box for box in _mp4_boxes(data, stbl[1], stbl[2]) if box[0] in (b'stco', b'co64')]
        if len(tables) != 1:
            raise ValueError('screenrecord_mett_offset_unknown')
        table = tables[0]
        width = 4 if table[0] == b'stco' else 8
        if table[2]-table[1] != 8+width or struct.unpack_from('>I', data, table[1]+4)[0] != 1:
            raise ValueError('screenrecord_mett_single_chunk_required')
        offset = struct.unpack_from('>I' if width == 4 else '>Q', data, table[1]+8)[0]
        if not any(begin <= offset and offset+size <= end for begin,end in media_ranges):
            raise ValueError('screenrecord_mett_outside_mdat')
        sample = data[offset:offset+size]
        if sample.startswith(b'#VV1NSC0PET1ME!#') and len(sample) >= 20:
            count = struct.unpack_from('<I', sample, 16)[0]
            begin, multiplier, version = 20, 1000, 1
        elif sample.startswith(b'#VV1NSC0PET1ME2#') and len(sample) >= 36:
            version, _, count = struct.unpack_from('<IqQ', sample, 16)
            if version != 2:
                raise ValueError('screenrecord_winscope_version_unknown')
            begin, multiplier = 36, 1
        else:
            raise ValueError('screenrecord_winscope_header_unknown')
        if not 0 < count <= 6001 or len(sample) != begin+8*count:
            raise ValueError('screenrecord_winscope_count_invalid')
        timestamps = [value*multiplier for value in struct.unpack_from('<'+'Q'*count, sample, begin)]
        if not timestamps[0] or any(b <= a for a,b in zip(timestamps,timestamps[1:])):
            raise ValueError('screenrecord_winscope_time_order_invalid')
        results.append(dict(source='mp4_mett_tables', version=version, frame_count=count,
            elapsed_ns=timestamps, sample_offset=offset, sample_bytes=size,
            sample_sha256=hashlib.sha256(sample).hexdigest(), timestamp_semantics_verified=False))
    if len(results) != 1:
        raise ValueError('screenrecord_unique_winscope_track_required')
    return results[0]


class OpeningMp4Materials:
    """One Owner's capture-only recorder. The worker never owns a Maa object."""
    def __init__(self, adb_path, serial, display_id, directory, *, max_seconds=60, pre_roll_seconds=1.0):
        import threading
        self.directory = Path(directory)
        if (not Path(adb_path).is_file() or type(serial) is not str or not serial
                or type(display_id) is not str or re.fullmatch('[0-9]+', display_id) is None
                or re.fullmatch('[0-9a-f]{32}', self.directory.name) is None
                or type(max_seconds) is not int or not 1 <= max_seconds <= 60
                or type(pre_roll_seconds) not in (int,float) or not 0 <= pre_roll_seconds <= 3):
            raise ValueError('clan_battle_mp4_binding_invalid')
        self.adb, self.serial, self.display_id = str(adb_path), serial, display_id
        self.max_seconds, self.token = max_seconds, self.directory.name
        self.pre_roll_seconds = pre_roll_seconds
        self.process_verified_ns = self.pre_roll_completed_ns = None
        self.remote = '/sdcard/maa-clan-opening-'+self.token+'.mp4'
        self.media = self.directory/'original.mp4'
        self.stop, self.ready = threading.Event(), threading.Event()
        self.rows, self.markers, self.error = [], [], None
        self.process, self.remote_pid, self.identity = None, None, None
        self.commands, self.clocks, self.media_evidence, self.metadata = [], [], None, None
        self.thread = threading.Thread(target=self._run, name='clan-opening-mp4', daemon=False)

    def _command(self, name, arguments, *, timeout=2, required=True):
        remaining = self.deadline-time.monotonic()-1
        if remaining <= 0:
            raise TimeoutError('clan_battle_mp4_total_deadline')
        timeout = min(timeout,remaining)
        index = len(self.commands)
        row = dict(name=name, lower_ns=time.monotonic_ns())
        self.commands.append(row)
        out, err = self.directory/f'{index:02d}-{name}.stdout', self.directory/f'{index:02d}-{name}.stderr'
        with out.open('wb') as stdout, err.open('wb') as stderr:
            process = subprocess.Popen([self.adb, '-s', self.serial, *arguments],
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            row['local_adb_pid'] = process.pid
            try:
                code = process.wait(timeout=timeout)
            except BaseException:
                process.kill()
                process.wait(timeout=1)
                row.update(upper_ns=time.monotonic_ns(), status='local_wait_failed')
                raise
        data = out.read_bytes()
        row.update(upper_ns=time.monotonic_ns(), returncode=code, stdout_path=out.name,
            stderr_path=err.name, stdout_sha256=hashlib.sha256(data).hexdigest())
        if required and code != 0:
            raise RuntimeError('clan_battle_mp4_command_failed:'+name)
        return data, row

    def _clock(self, phase):
        data, row = self._command('uptime-'+phase, ['shell','cat','/proc/uptime'])
        if re.fullmatch(rb'[0-9]+\.[0-9]+(?:\s+[0-9]+\.[0-9]+)?\s*',data) is None:
            raise RuntimeError('clan_battle_mp4_clock_unreadable')
        self.clocks.append(dict(phase=phase, **row, device_clock_semantics_verified=False))

    def _identity(self, pid):
        stat, _ = self._command('record-stat', ['exec-out','cat',f'/proc/{pid}/stat'])
        cmdline, _ = self._command('record-cmdline', ['exec-out','cat',f'/proc/{pid}/cmdline'])
        fields = stat.decode('ascii').rsplit(') ',1)[-1].split()
        arguments = cmdline.rstrip(b'\0').split(b'\0')
        expected = [b'/system/bin/screenrecord',b'--bugreport',b'--display-id',self.display_id.encode('ascii'),
            b'--size',b'1920x1080',b'--bit-rate',b'12000000',b'--time-limit',str(self.max_seconds).encode('ascii'),
            self.remote.encode('ascii')]
        if len(fields) <= 19 or not fields[19].isdigit() or arguments != expected:
            raise RuntimeError('clan_battle_mp4_owned_process_unverified')
        return int(fields[19]), hashlib.sha256(cmdline).hexdigest()

    def _run(self):
        self.deadline = time.monotonic()+self.max_seconds+20
        try:
            self._command('path-unused', ['shell','test','!','-e',self.remote])
            self._clock('before')
            binary_before, _ = self._command('binary-before', ['shell','sha256sum','/system/bin/screenrecord'])
            expected_binary = re.match(rb'^([0-9a-fA-F]{64})\s',binary_before)
            if not expected_binary:
                raise RuntimeError('clan_battle_mp4_binary_hash_unknown')
            payload=(f'test ! -e {self.remote} || exit 67; echo MAA_SCREENRECORD_PID:$$; '
                f'exec /system/bin/screenrecord --bugreport --display-id {self.display_id} --size 1920x1080 '
                f'--bit-rate 12000000 --time-limit {self.max_seconds} {self.remote}')
            self.record_stdout = self.directory/'record.stdout'
            with self.record_stdout.open('wb') as stdout, (self.directory/'record.stderr').open('wb') as stderr:
                self.record_lower_ns = time.monotonic_ns()
                self.process = subprocess.Popen([self.adb,'-s',self.serial,'shell',"sh -c '"+payload+"'"],
                    stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                deadline = min(self.deadline-1,time.monotonic()+self.max_seconds+5)
                while self.process.poll() is None:
                    if self.remote_pid is None:
                        match = re.search(rb'^MAA_SCREENRECORD_PID:([1-9][0-9]*)\r?$',self.record_stdout.read_bytes()[:1024],re.MULTILINE)
                        if match:
                            self.remote_pid = int(match.group(1))
                            self.identity = self._identity(self.remote_pid)
                            self.process_verified_ns = time.monotonic_ns()
                    if (self.identity is not None and self.pre_roll_completed_ns is None
                            and time.monotonic_ns()-self.process_verified_ns >= self.pre_roll_seconds*1000000000):
                        self.pre_roll_completed_ns = time.monotonic_ns()
                        self.ready.set()  # Pre-Start warm-up, never a first-frame claim.
                    if time.monotonic() >= deadline:
                        raise TimeoutError('clan_battle_mp4_recording_deadline')
                    time.sleep(.05)
                if self.process.wait(timeout=1) != 0:
                    raise RuntimeError('clan_battle_mp4_recording_failed')
                self.record_upper_ns = time.monotonic_ns()
            if self.identity is None:
                raise RuntimeError('clan_battle_mp4_recording_process_not_observed')
            self._clock('after')
            binary_after, _ = self._command('binary-after', ['shell','sha256sum','/system/bin/screenrecord'])
            if binary_after != binary_before:
                raise RuntimeError('clan_battle_mp4_binary_changed')
            size, _ = self._command('media-size', ['shell','stat','-c','%s',self.remote])
            size = int(size.strip())
            digest, _ = self._command('media-sha', ['shell','sha256sum',self.remote])
            match = re.match(rb'^([0-9a-fA-F]{64})\s',digest)
            if not match or not 0 < size <= 256*1024*1024:
                raise RuntimeError('clan_battle_mp4_media_unknown')
            if self.media.exists():
                raise RuntimeError('clan_battle_mp4_local_media_exists')
            self._command('pull', ['pull',self.remote,str(self.media)], timeout=10)
            local = self.media.read_bytes()
            actual = hashlib.sha256(local).hexdigest()
            if len(local) != size or actual != match.group(1).decode('ascii').lower():
                raise RuntimeError('clan_battle_mp4_media_changed')
            self.metadata = read_screenrecord_metadata(self.media)
            self.media_evidence = dict(path=self.media.name, bytes=size, sha256=actual,
                remote_path=self.remote, remote_original_retained=True)
        except Exception as exc:
            import traceback
            self.error = f'{type(exc).__name__}:{exc}'
            self.traceback = traceback.format_exc()
        finally:
            try:
                if self.process is not None and self.process.poll() is None:
                    self.process.kill()  # Only this worker's LOCAL adb child; remote has its natural time limit.
                    self.process.wait(timeout=1)
            except Exception as exc:
                self.error = self.error or 'local_recorder_not_released:'+str(exc)
            finally:
                self.ready.set()

    def start(self):
        self.directory.mkdir(parents=True,exist_ok=False)
        self.thread.start()
        if (not self.ready.wait(timeout=8) or self.identity is None or self.pre_roll_completed_ns is None
                or self.error is not None):
            self.stop_and_join()
            raise RuntimeError('clan_battle_mp4_process_not_ready')
        return self

    def mark(self, phase, node, battle_start):
        try:
            self.markers.append(dict(phase=phase,node=node,battle_start=battle_start,
                host_monotonic_ns=time.monotonic_ns(),meaning='business_call_boundary_not_capture_pts'))
        except Exception as exc:
            self.error = self.error or 'capture_marker_failed:'+type(exc).__name__

    def stop_and_join(self):
        # No remote signal: PID reuse cannot be ruled out by separate adb readbacks.
        if not self.stop.is_set():
            self.stop_requested_ns = time.monotonic_ns()
        self.stop.set()
        self.thread.join(timeout=self.max_seconds+25)
        if self.thread.is_alive():
            raise RuntimeError('clan_battle_mp4_worker_not_released')

    def finish(self, *, source_valid, battle_start):
        import json
        self.stop_and_join()
        result = dict(purpose='capture_only_not_ub_acceptance',format='mp4',start_token=self.token,
            source_bound=source_valid is True,battle_start=battle_start,first_ub_verified=False,
            first_frame_verified=False,coverage='unknown',not_fired_unit_ids=[],overlay_requested=True,
            info_frame_exclusion_verified=False,recorder_process_identity=self.identity,
            natural_limit_seconds=self.max_seconds,remote_signals_sent=False,
            total_worker_deadline_seconds=self.max_seconds+20,join_wait_bound_seconds=self.max_seconds+25,
            stop_requested_ns=getattr(self,'stop_requested_ns',None),
            pre_roll=dict(requested_seconds=self.pre_roll_seconds,process_verified_ns=self.process_verified_ns,
                completed_ns=self.pre_roll_completed_ns,first_frame_proof=False),
            remote_recording_pid=self.remote_pid,local_worker_released=not self.thread.is_alive(),
            media=self.media_evidence,metadata=self.metadata,clock_samples=self.clocks,
            commands=self.commands,markers=self.markers,error=self.error,
            recording_command=dict(local_adb_pid=self.process.pid if self.process is not None else None,
                lower_ns=getattr(self,'record_lower_ns',None),upper_ns=getattr(self,'record_upper_ns',None),
                returncode=self.process.poll() if self.process is not None else None),
            status='captured' if source_valid is True and self.media_evidence and self.error is None else 'unknown')
        if self.error:
            result['traceback'] = getattr(self,'traceback',None)
        (self.directory/'capture.json').write_bytes((json.dumps(result,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
        return result
