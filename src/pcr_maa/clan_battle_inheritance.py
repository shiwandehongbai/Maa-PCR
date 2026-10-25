"""Temporarily disable battle inheritance through the same Owner, then restore it."""
from copy import deepcopy
import hashlib
import json
import re
from uuid import uuid4

import cv2
import numpy as np
from maa.custom_recognition import CustomRecognition

from . import native
from .preset_execution import Journal


OPERATION = 'clan_battle_inheritance'


def inheritance_pipeline(spec):
    from .clan_battle_native import _node
    if (type(spec) is not dict or set(spec) != {'battle_selected', 'radio_on', 'radio_off', 'samples'}
            or any(type(spec[key]) is not dict or spec[key].get('recognition') != 'TemplateMatch'
                   for key in ('battle_selected', 'radio_on', 'radio_off'))):
        raise ValueError('clan_battle_inheritance_calibration_invalid')
    nodes = {'CBInheritance'+name: _node(spec[key]) for name, key in (
        ('BattleSelected', 'battle_selected'), ('RadioOn', 'radio_on'), ('RadioOff', 'radio_off'))}
    for name, expected, roi in (
            ('SettingsTitle', '^设定$', [575, 28, 135, 60]),
            ('SettingsEntry', '^设定$', [180, 235, 115, 70]),
            ('BattleLabel', '^战斗$', [445, 90, 105, 65]),
            ('AllLabel', '^所有$', [130, 185, 95, 65]),
            ('AllExpanded', '^连结爆发立即发动模式的设定$', [40, 270, 550, 50]),
            ('Description', '^自动战斗[、，,]连结爆发立即发动模式和战斗速度[。.]?$', [40, 160, 1135, 370]),
            ('OnLabel', '^继承$', [620, 180, 140, 395]),
            ('OffLabel', '^无法继承$', [930, 180, 180, 395]),
            ('CloseLabel', '^关闭$', [565, 605, 160, 70])):
        nodes['CBInheritance'+name] = _node(dict(roi=roi, expected=expected))
    base = ['CBInheritanceSettingsTitle', 'CBInheritanceCloseLabel']
    nodes['CBInheritanceSettingsObserved'] = dict(recognition='And', all_of=base,
        action='DoNothing', next=[], timeout=1000, pre_delay=0, post_delay=0)
    for name, guards, target in (
            ('Open', ['CBInheritanceSettingsEntry', 'LabyClientMainMenuTitle', 'LabyClientMainMenuProfileLabel'], True),
            ('Battle', ['CBInheritanceBattleLabel', *base], True),
            ('Expand', ['CBInheritanceAllLabel', *base, 'CBInheritanceBattleSelected'], True),
            ('Close', ['CBInheritanceCloseLabel', 'CBInheritanceSettingsTitle'], True)):
        nodes['CBInheritance'+name] = dict(recognition='And', all_of=guards, action='Click', target=target,
            next=[], max_hit=1, timeout=3000, pre_delay=0, post_delay=500)
    nodes['CBInheritanceDown'] = dict(recognition='And', all_of=[*base, 'CBInheritanceBattleSelected'],
        action='Swipe', begin=[1180, 555], end=[1180, 335], duration=500,
        next=[], max_hit=1, timeout=3000, pre_delay=0, post_delay=700)
    for name, mode in (('Read', 'read'), ('Enable', 'enable'), ('Disable', 'disable')):
        nodes['CBInheritance'+name] = dict(recognition='Custom', custom_recognition='ClanBattleInheritance',
            custom_recognition_param=mode, action='DoNothing' if mode == 'read' else 'Click',
            target=True, next=[], timeout=3000, pre_delay=0, post_delay=500 if mode != 'read' else 0)
        if mode != 'read':
            nodes['CBInheritance'+name]['max_hit'] = 1
    return nodes


class InheritancePage(CustomRecognition):
    def __init__(self, spec, reader):
        super().__init__()
        self.reader = reader
        self.radios = {state: cv2.imdecode(np.frombuffer(
            (native.RESOURCE/'image'/spec['radio_'+state]['template']).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                       for state in ('on', 'off')}
        if any(image is None or image.shape != (18, 18, 3) for image in self.radios.values()):
            raise ValueError('clan_battle_inheritance_radio_invalid')

    def read(self, context, image):
        if context.tasker.stopping or image is None or image.shape != (720, 1280, 3):
            raise RuntimeError('clan_battle_inheritance_unknown')
        for name in ('SettingsTitle', 'BattleSelected'):
            if not self.reader.hit(context, image, 'CBInheritance'+name):
                raise RuntimeError('clan_battle_inheritance_unknown')
        self.reader.word(context, image, 'CBInheritanceDescription')
        boxes = []
        for name in ('OnLabel', 'OffLabel'):
            self.reader.word(context, image, 'CBInheritance'+name)
            boxes.append(list(context.run_recognition('CBInheritance'+name, image).box))
        left, right = boxes
        if abs(left[1]+left[3]/2-right[1]-right[3]/2) > 5 or not 250 < right[0]-left[0] < 360:
            raise RuntimeError('clan_battle_inheritance_unknown')
        centers = [(box[0]-39, int(box[1]+box[3]/2)) for box in boxes]
        states = []
        for x, y in centers:
            patch = image[y-17:y+18, x-17:x+18]
            scores = {key: float(cv2.minMaxLoc(cv2.matchTemplate(patch, template, cv2.TM_SQDIFF_NORMED))[0])
                      for key, template in self.radios.items()}
            matches = [key for key, score in scores.items() if score < .025]
            if len(matches) != 1:
                raise RuntimeError('clan_battle_inheritance_unknown')
            states.append(matches[0])
        if states not in (['on', 'off'], ['off', 'on']):
            raise RuntimeError('clan_battle_inheritance_unknown')
        return dict(inherit=states[0] == 'on', same_frame=True,
            frame_pixel_sha256=hashlib.sha256(image.tobytes()).hexdigest()), centers

    def analyze(self, context, argv):
        try:
            reading, centers = self.read(context, argv.image)
            mode = json.loads(argv.custom_recognition_param)
            if mode not in ('read', 'enable', 'disable') or mode != 'read' and reading['inherit'] is (mode == 'enable'):
                return None
            x, y = centers[0 if mode == 'enable' else 1]
            return self.AnalyzeResult(box=[x-3, y-3, 6, 6], detail=reading)
        except Exception:
            return None


class BattleInheritance:
    def __init__(self, port, journal):
        self.port, self.journal = port, journal
        self.intent = None
        self.evidence = None

    def _pending(self):
        events = self.journal.events()
        for index, intent in enumerate(events[::2]):
            if (intent.get('operation') != OPERATION or type(intent.get('account_id')) is not str
                    or re.fullmatch('[0-9]{1,19}', intent['account_id']) is None
                    or intent.get('original') is not True):
                raise RuntimeError('clan_battle_inheritance_ledger_invalid')
            if 2*index+1 == len(events):
                if intent['account_id'] != self.port.account:
                    raise RuntimeError('clan_battle_inheritance_pending_other_account')
                return intent
            result = events[2*index+1]
            if (any(result.get(key) != intent.get(key) for key in ('intent_id', 'operation', 'account_id', 'original'))
                    or result.get('restored') is not True):
                raise RuntimeError('clan_battle_inheritance_ledger_invalid')
        return None

    def _restore(self, intent):
        state = self.port.read_inheritance()
        if state['inherit'] is False:
            state = self.port.set_inheritance(False, True)
        if state['inherit'] is not True:
            raise RuntimeError('clan_battle_inheritance_restore_unverified')
        self.journal.append(dict(intent, state='settled', restored=True, restore_evidence=state))
        return state

    def prepare(self):
        with self.journal.locked():
            pending = self._pending()
            if pending is not None:
                self._restore(pending)
            state = self.port.read_inheritance()
            self.evidence = dict(original=state['inherit'], original_evidence=deepcopy(state), restored=not state['inherit'])
            if state['inherit']:
                self.intent = dict(state='in_flight', operation=OPERATION, intent_id=uuid4().hex,
                    account_id=self.port.account, original=True, original_evidence=deepcopy(state))
                self.journal.append(self.intent)
                state = self.port.set_inheritance(True, False)
            if state['inherit'] is not False:
                raise RuntimeError('clan_battle_inheritance_prepare_unverified')
            self.evidence['prepared_evidence'] = deepcopy(state)

    def restore(self):
        if self.intent is None:
            state = self.port.read_inheritance()
            if self.evidence is None or state['inherit'] is not self.evidence['original']:
                raise RuntimeError('clan_battle_inheritance_restore_unverified')
            self.evidence.update(restored=True, restore_evidence=deepcopy(state))
            return deepcopy(self.evidence)
        with self.journal.locked():
            if self._pending() != self.intent:
                raise RuntimeError('clan_battle_inheritance_ledger_changed')
            state = self._restore(self.intent)
            self.evidence.update(restored=True, restore_evidence=deepcopy(state))
            self.intent = None
        return deepcopy(self.evidence)

    @classmethod
    def for_port(cls, port):
        return cls(port, Journal(native.ROOT/'.runtime/clan-battle-simulation-work/inheritance'))
